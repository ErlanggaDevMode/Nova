"""Phase 18: Platform System Event Triggers (F18.1–F18.5).

Provides cross-platform, testable system telemetry listeners and safe action
dispatchers for the Nova Desktop Agent:

- **F18.1**: ``DiskUsageListener`` — monitors per-mount disk usage (% and free GB)
  with hysteresis and cooldown protection.
- **F18.2**: ``ResourceUtilizationTrigger`` — monitors CPU % and RAM % with
  sustained consecutive-sample confirmation to suppress transient spikes.
- **F18.3**: ``NetworkConnectivityHook`` — detects ``online`` <-> ``offline``
  transitions with optional debounce and structured hook callbacks.
- **F18.4**: ``SessionLockMonitor`` — cross-platform session lock/unlock adapter
  (Linux/Windows/macOS) + injectable provider for ``locked`` <-> ``unlocked``.
- **F18.5**: ``LowBatteryTrigger`` & ``SafeActionDispatcher`` — monitors battery
  level and ``power_plugged`` state, validates and dispatches safe actions or
  forwards structured trigger events to ``nova_core``.
"""

from __future__ import annotations

import logging
import platform
import socket
import subprocess
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Callable, Literal, Optional, Protocol, runtime_checkable

from pydantic import BaseModel, Field, model_validator

from .models import ActionRequest

logger = logging.getLogger("nova.system_triggers")


# ---------------------------------------------------------------------------
# Shared Enums, Clock Helpers & Structured Event Model
# ---------------------------------------------------------------------------


class TriggerType(str, Enum):
    DISK_USAGE = "disk_usage"
    CPU_UTILIZATION = "cpu_utilization"
    RAM_UTILIZATION = "ram_utilization"
    NETWORK_CONNECTIVITY = "network_connectivity"
    SESSION_LOCK = "session_lock"
    LOW_BATTERY = "low_battery"


class TriggerSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


_SEVERITY_RANK: dict[TriggerSeverity, int] = {
    TriggerSeverity.INFO: 0,
    TriggerSeverity.WARNING: 1,
    TriggerSeverity.CRITICAL: 2,
}


def severity_at_least(current: TriggerSeverity, minimum: TriggerSeverity) -> bool:
    """Returns True when ``current`` severity is >= ``minimum`` severity."""
    return _SEVERITY_RANK[current] >= _SEVERITY_RANK[minimum]


def _default_utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _invoke_callbacks(
    callbacks: list[Callable[["SystemTriggerEvent"], Any]],
    event: "SystemTriggerEvent",
) -> None:
    for cb in callbacks:
        try:
            cb(event)
        except Exception as exc:  # pragma: no cover - defensive guard
            logger.error("System trigger callback failed for %s: %s", event.event_name, exc)


class SystemTriggerEvent(BaseModel):
    """Structured event emitted by all Phase 18 platform system triggers."""

    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    trigger_type: TriggerType
    event_name: str = Field(..., min_length=1, max_length=128)
    severity: TriggerSeverity = Field(default=TriggerSeverity.INFO)
    source_device_id: str = Field(default="desktop_agent_client", max_length=128)
    timestamp: datetime = Field(default_factory=_default_utc_now)
    message: str = Field(default="")
    metrics: dict[str, Any] = Field(default_factory=dict)
    state_transition: Optional[dict[str, Any]] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)

    def to_automation_event(self) -> dict[str, Any]:
        """Converts to a flat event dict compatible with ``AutomationEngine.evaluate_and_fire``."""
        payload: dict[str, Any] = {
            "type": f"system.{self.trigger_type.value}",
            "event_id": self.event_id,
            "event_name": self.event_name,
            "trigger_type": self.trigger_type.value,
            "severity": self.severity.value,
            "source": self.source_device_id,
            "text": self.message,
            "timestamp": self.timestamp.isoformat(),
        }
        if self.state_transition:
            payload["previous_state"] = self.state_transition.get("from")
            payload["current_state"] = self.state_transition.get("to")
        for key, value in self.metrics.items():
            if key not in payload:
                payload[key] = value
        return payload


# ---------------------------------------------------------------------------
# F18.1 — Disk Usage Threshold Listener
# ---------------------------------------------------------------------------


class DiskUsageSample(BaseModel):
    """Point-in-time disk usage reading for a mount path."""

    mount_path: str = Field(..., min_length=1)
    total_bytes: int = Field(..., ge=0)
    used_bytes: int = Field(..., ge=0)
    free_bytes: int = Field(..., ge=0)
    percent: float = Field(..., ge=0.0, le=100.0)

    @property
    def total_gb(self) -> float:
        return round(self.total_bytes / (1024**3), 3)

    @property
    def used_gb(self) -> float:
        return round(self.used_bytes / (1024**3), 3)

    @property
    def free_gb(self) -> float:
        return round(self.free_bytes / (1024**3), 3)


class DiskThresholdConfig(BaseModel):
    """Configuration for F18.1 disk usage threshold monitoring."""

    enabled: bool = Field(default=True)
    mount_paths: list[str] = Field(default_factory=lambda: ["/"])
    warning_percent: float = Field(default=85.0, ge=0.0, le=100.0)
    critical_percent: float = Field(default=95.0, ge=0.0, le=100.0)
    min_free_gb: Optional[float] = Field(default=None, ge=0.0)
    critical_free_gb: Optional[float] = Field(default=None, ge=0.0)
    hysteresis_percent: float = Field(default=3.0, ge=0.0, le=50.0)
    hysteresis_free_gb: float = Field(default=1.0, ge=0.0)
    cooldown_seconds: float = Field(default=300.0, ge=0.0)
    emit_recovery: bool = Field(default=True)

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "DiskThresholdConfig":
        if self.critical_percent < self.warning_percent:
            raise ValueError(
                f"critical_percent ({self.critical_percent}) must be >= warning_percent ({self.warning_percent})."
            )
        if (
            self.min_free_gb is not None
            and self.critical_free_gb is not None
            and self.critical_free_gb > self.min_free_gb
        ):
            raise ValueError(
                f"critical_free_gb ({self.critical_free_gb}) must be <= min_free_gb ({self.min_free_gb})."
            )
        cleaned_mounts = [m.strip() for m in self.mount_paths if m and m.strip()]
        if not cleaned_mounts:
            raise ValueError("mount_paths must contain at least one non-empty path.")
        self.mount_paths = list(dict.fromkeys(cleaned_mounts))
        return self


def default_disk_probe(mount_path: str) -> DiskUsageSample:
    """Default disk usage probe backed by ``psutil.disk_usage``."""
    import psutil

    usage = psutil.disk_usage(mount_path)
    return DiskUsageSample(
        mount_path=mount_path,
        total_bytes=int(usage.total),
        used_bytes=int(usage.used),
        free_bytes=int(usage.free),
        percent=float(usage.percent),
    )


class _DiskMountState:
    __slots__ = ("level", "last_alert_at", "last_sample", "last_error")

    def __init__(self) -> None:
        self.level: Literal["ok", "warning", "critical"] = "ok"
        self.last_alert_at: Optional[float] = None
        self.last_sample: Optional[DiskUsageSample] = None
        self.last_error: Optional[str] = None


class DiskUsageListener:
    """F18.1: Monitors disk usage per mount path with hysteresis & cooldown."""

    def __init__(
        self,
        config: Optional[DiskThresholdConfig] = None,
        *,
        probe: Optional[Callable[[str], DiskUsageSample]] = None,
        device_id: str = "desktop_agent_client",
        clock: Optional[Callable[[], float]] = None,
        utc_now: Optional[Callable[[], datetime]] = None,
        callbacks: Optional[list[Callable[[SystemTriggerEvent], Any]]] = None,
    ) -> None:
        self.config = config or DiskThresholdConfig()
        self._probe = probe or default_disk_probe
        self.device_id = device_id
        self._clock = clock or time.monotonic
        self._utc_now = utc_now or _default_utc_now
        self._callbacks: list[Callable[[SystemTriggerEvent], Any]] = list(callbacks or [])
        self._states: dict[str, _DiskMountState] = {}

    def register_callback(self, callback: Callable[[SystemTriggerEvent], Any]) -> None:
        self._callbacks.append(callback)

    def _classify_sample(self, sample: DiskUsageSample) -> Literal["ok", "warning", "critical"]:
        cfg = self.config
        free_gb = sample.free_gb

        is_critical_pct = sample.percent >= cfg.critical_percent
        is_critical_free = (
            cfg.critical_free_gb is not None and free_gb <= cfg.critical_free_gb
        )
        if is_critical_pct or is_critical_free:
            return "critical"

        is_warn_pct = sample.percent >= cfg.warning_percent
        is_warn_free = cfg.min_free_gb is not None and free_gb <= cfg.min_free_gb
        if is_warn_pct or is_warn_free:
            return "warning"

        return "ok"

    def _can_recover(self, sample: DiskUsageSample) -> bool:
        cfg = self.config
        pct_recovered = sample.percent <= max(0.0, cfg.warning_percent - cfg.hysteresis_percent)
        if cfg.min_free_gb is not None:
            free_recovered = sample.free_gb >= (cfg.min_free_gb + cfg.hysteresis_free_gb)
            return pct_recovered and free_recovered
        return pct_recovered

    def check_once(self) -> list[SystemTriggerEvent]:
        if not self.config.enabled:
            return []

        now_mono = self._clock()
        now_utc = self._utc_now()
        events: list[SystemTriggerEvent] = []

        for mount in self.config.mount_paths:
            state = self._states.setdefault(mount, _DiskMountState())
            try:
                sample = self._probe(mount)
                state.last_sample = sample
                state.last_error = None
            except Exception as exc:
                state.last_error = str(exc)
                logger.warning("Disk probe failed for mount '%s': %s", mount, exc)
                continue

            raw_level = self._classify_sample(sample)
            prev_level = state.level

            if raw_level == "ok":
                if prev_level != "ok" and self._can_recover(sample):
                    state.level = "ok"
                    state.last_alert_at = None
                    if self.config.emit_recovery:
                        event = SystemTriggerEvent(
                            trigger_type=TriggerType.DISK_USAGE,
                            event_name="disk.recovered",
                            severity=TriggerSeverity.INFO,
                            source_device_id=self.device_id,
                            timestamp=now_utc,
                            message=(
                                f"Disk usage recovered on '{mount}': "
                                f"{sample.percent:.1f}% used ({sample.free_gb:.2f} GB free)."
                            ),
                            metrics={
                                "mount_path": mount,
                                "percent": sample.percent,
                                "free_gb": sample.free_gb,
                                "used_gb": sample.used_gb,
                                "total_gb": sample.total_gb,
                            },
                            state_transition={"from": prev_level, "to": "ok"},
                        )
                        events.append(event)
                        _invoke_callbacks(self._callbacks, event)
                # Otherwise still inside hysteresis deadband -> hold prev_level without firing.
                continue

            # Breached (warning or critical)
            is_new_breach = prev_level == "ok"
            is_escalation = prev_level == "warning" and raw_level == "critical"
            cooldown_elapsed = (
                state.last_alert_at is None
                or (now_mono - state.last_alert_at) >= self.config.cooldown_seconds
            )

            # While active, keep critical sticky until recovered or demoted below critical
            state.level = raw_level

            if is_new_breach or is_escalation or cooldown_elapsed:
                severity = (
                    TriggerSeverity.CRITICAL
                    if raw_level == "critical"
                    else TriggerSeverity.WARNING
                )
                state.last_alert_at = now_mono
                event = SystemTriggerEvent(
                    trigger_type=TriggerType.DISK_USAGE,
                    event_name="disk.threshold_exceeded",
                    severity=severity,
                    source_device_id=self.device_id,
                    timestamp=now_utc,
                    message=(
                        f"Disk usage {raw_level} on '{mount}': "
                        f"{sample.percent:.1f}% used ({sample.free_gb:.2f} GB free)."
                    ),
                    metrics={
                        "mount_path": mount,
                        "percent": sample.percent,
                        "free_gb": sample.free_gb,
                        "used_gb": sample.used_gb,
                        "total_gb": sample.total_gb,
                        "warning_percent": self.config.warning_percent,
                        "critical_percent": self.config.critical_percent,
                        "min_free_gb": self.config.min_free_gb,
                    },
                    state_transition=(
                        {"from": prev_level, "to": raw_level}
                        if prev_level != raw_level
                        else None
                    ),
                )
                events.append(event)
                _invoke_callbacks(self._callbacks, event)

        return events

    def get_status(self) -> dict[str, Any]:
        mounts: dict[str, Any] = {}
        for mount, st in self._states.items():
            mounts[mount] = {
                "level": st.level,
                "last_error": st.last_error,
                "last_sample": st.last_sample.model_dump() if st.last_sample else None,
            }
        return {"enabled": self.config.enabled, "mounts": mounts}


# ---------------------------------------------------------------------------
# F18.2 — CPU & RAM Utilization Trigger (Sustained Thresholds)
# ---------------------------------------------------------------------------


class ResourceSample(BaseModel):
    """Point-in-time CPU and RAM utilization reading."""

    cpu_percent: float = Field(..., ge=0.0, le=100.0)
    ram_percent: float = Field(..., ge=0.0, le=100.0)
    ram_used_gb: Optional[float] = Field(default=None, ge=0.0)
    ram_total_gb: Optional[float] = Field(default=None, ge=0.0)
    ram_available_gb: Optional[float] = Field(default=None, ge=0.0)


class ResourceThresholdConfig(BaseModel):
    """Configuration for F18.2 CPU & RAM utilization trigger."""

    enabled: bool = Field(default=True)
    cpu_warning_percent: float = Field(default=80.0, ge=0.0, le=100.0)
    cpu_critical_percent: float = Field(default=95.0, ge=0.0, le=100.0)
    ram_warning_percent: float = Field(default=80.0, ge=0.0, le=100.0)
    ram_critical_percent: float = Field(default=95.0, ge=0.0, le=100.0)
    consecutive_samples: int = Field(default=3, ge=1, le=100)
    hysteresis_percent: float = Field(default=5.0, ge=0.0, le=50.0)
    cooldown_seconds: float = Field(default=120.0, ge=0.0)
    emit_recovery: bool = Field(default=True)

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "ResourceThresholdConfig":
        if self.cpu_critical_percent < self.cpu_warning_percent:
            raise ValueError(
                f"cpu_critical_percent ({self.cpu_critical_percent}) must be >= "
                f"cpu_warning_percent ({self.cpu_warning_percent})."
            )
        if self.ram_critical_percent < self.ram_warning_percent:
            raise ValueError(
                f"ram_critical_percent ({self.ram_critical_percent}) must be >= "
                f"ram_warning_percent ({self.ram_warning_percent})."
            )
        return self


def default_resource_probe() -> ResourceSample:
    """Default CPU/RAM probe backed by ``psutil``."""
    import psutil

    cpu = float(psutil.cpu_percent(interval=None))
    mem = psutil.virtual_memory()
    return ResourceSample(
        cpu_percent=cpu,
        ram_percent=float(mem.percent),
        ram_used_gb=round(mem.used / (1024**3), 3),
        ram_total_gb=round(mem.total / (1024**3), 3),
        ram_available_gb=round(mem.available / (1024**3), 3),
    )


class _SustainedMetricState:
    __slots__ = ("level", "consecutive_breaches", "last_alert_at", "last_value")

    def __init__(self) -> None:
        self.level: Literal["ok", "warning", "critical"] = "ok"
        self.consecutive_breaches: int = 0
        self.last_alert_at: Optional[float] = None
        self.last_value: Optional[float] = None


class ResourceUtilizationTrigger:
    """F18.2: Sustained CPU and RAM threshold trigger with spike suppression."""

    def __init__(
        self,
        config: Optional[ResourceThresholdConfig] = None,
        *,
        probe: Optional[Callable[[], ResourceSample]] = None,
        device_id: str = "desktop_agent_client",
        clock: Optional[Callable[[], float]] = None,
        utc_now: Optional[Callable[[], datetime]] = None,
        callbacks: Optional[list[Callable[[SystemTriggerEvent], Any]]] = None,
    ) -> None:
        self.config = config or ResourceThresholdConfig()
        self._probe = probe or default_resource_probe
        self.device_id = device_id
        self._clock = clock or time.monotonic
        self._utc_now = utc_now or _default_utc_now
        self._callbacks: list[Callable[[SystemTriggerEvent], Any]] = list(callbacks or [])
        self._cpu_state = _SustainedMetricState()
        self._ram_state = _SustainedMetricState()
        self._last_sample: Optional[ResourceSample] = None

    def register_callback(self, callback: Callable[[SystemTriggerEvent], Any]) -> None:
        self._callbacks.append(callback)

    def _evaluate_metric(
        self,
        *,
        metric_name: Literal["cpu", "ram"],
        trigger_type: TriggerType,
        value: float,
        warn_threshold: float,
        crit_threshold: float,
        state: _SustainedMetricState,
        extra_metrics: dict[str, Any],
        now_mono: float,
        now_utc: datetime,
    ) -> Optional[SystemTriggerEvent]:
        cfg = self.config
        state.last_value = value
        prev_level = state.level

        if value >= warn_threshold:
            state.consecutive_breaches += 1
            if state.consecutive_breaches < cfg.consecutive_samples:
                # Spike not yet sustained
                return None

            new_level: Literal["warning", "critical"] = (
                "critical" if value >= crit_threshold else "warning"
            )
            is_new_breach = prev_level == "ok"
            is_escalation = prev_level == "warning" and new_level == "critical"
            cooldown_elapsed = (
                state.last_alert_at is None
                or (now_mono - state.last_alert_at) >= cfg.cooldown_seconds
            )
            state.level = new_level

            if is_new_breach or is_escalation or cooldown_elapsed:
                state.last_alert_at = now_mono
                severity = (
                    TriggerSeverity.CRITICAL
                    if new_level == "critical"
                    else TriggerSeverity.WARNING
                )
                label = metric_name.upper()
                return SystemTriggerEvent(
                    trigger_type=trigger_type,
                    event_name=f"{metric_name}.threshold_exceeded",
                    severity=severity,
                    source_device_id=self.device_id,
                    timestamp=now_utc,
                    message=(
                        f"{label} utilization {new_level}: {value:.1f}% "
                        f"(sustained for {state.consecutive_breaches} samples)."
                    ),
                    metrics={
                        f"{metric_name}_percent": value,
                        "consecutive_samples": state.consecutive_breaches,
                        "required_samples": cfg.consecutive_samples,
                        "warning_percent": warn_threshold,
                        "critical_percent": crit_threshold,
                        **extra_metrics,
                    },
                    state_transition=(
                        {"from": prev_level, "to": new_level}
                        if prev_level != new_level
                        else None
                    ),
                )
            return None

        # Value dipped below warning threshold -> reset sustained counter immediately
        state.consecutive_breaches = 0

        if prev_level != "ok" and value <= max(0.0, warn_threshold - cfg.hysteresis_percent):
            state.level = "ok"
            state.last_alert_at = None
            if cfg.emit_recovery:
                label = metric_name.upper()
                return SystemTriggerEvent(
                    trigger_type=trigger_type,
                    event_name=f"{metric_name}.recovered",
                    severity=TriggerSeverity.INFO,
                    source_device_id=self.device_id,
                    timestamp=now_utc,
                    message=f"{label} utilization recovered to {value:.1f}%.",
                    metrics={
                        f"{metric_name}_percent": value,
                        **extra_metrics,
                    },
                    state_transition={"from": prev_level, "to": "ok"},
                )
        return None

    def check_once(self) -> list[SystemTriggerEvent]:
        if not self.config.enabled:
            return []

        sample = self._probe()
        self._last_sample = sample
        now_mono = self._clock()
        now_utc = self._utc_now()
        events: list[SystemTriggerEvent] = []

        cpu_event = self._evaluate_metric(
            metric_name="cpu",
            trigger_type=TriggerType.CPU_UTILIZATION,
            value=sample.cpu_percent,
            warn_threshold=self.config.cpu_warning_percent,
            crit_threshold=self.config.cpu_critical_percent,
            state=self._cpu_state,
            extra_metrics={},
            now_mono=now_mono,
            now_utc=now_utc,
        )
        if cpu_event is not None:
            events.append(cpu_event)
            _invoke_callbacks(self._callbacks, cpu_event)

        ram_extra: dict[str, Any] = {}
        if sample.ram_used_gb is not None:
            ram_extra["ram_used_gb"] = sample.ram_used_gb
        if sample.ram_total_gb is not None:
            ram_extra["ram_total_gb"] = sample.ram_total_gb
        if sample.ram_available_gb is not None:
            ram_extra["ram_available_gb"] = sample.ram_available_gb

        ram_event = self._evaluate_metric(
            metric_name="ram",
            trigger_type=TriggerType.RAM_UTILIZATION,
            value=sample.ram_percent,
            warn_threshold=self.config.ram_warning_percent,
            crit_threshold=self.config.ram_critical_percent,
            state=self._ram_state,
            extra_metrics=ram_extra,
            now_mono=now_mono,
            now_utc=now_utc,
        )
        if ram_event is not None:
            events.append(ram_event)
            _invoke_callbacks(self._callbacks, ram_event)

        return events

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "cpu": {
                "level": self._cpu_state.level,
                "consecutive_breaches": self._cpu_state.consecutive_breaches,
                "last_value": self._cpu_state.last_value,
            },
            "ram": {
                "level": self._ram_state.level,
                "consecutive_breaches": self._ram_state.consecutive_breaches,
                "last_value": self._ram_state.last_value,
            },
        }


# ---------------------------------------------------------------------------
# F18.3 — Network Connectivity Online/Offline State Change Hook
# ---------------------------------------------------------------------------


class NetworkState(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


class NetworkProbeResult(BaseModel):
    """Result from a connectivity probe."""

    online: bool
    interface: Optional[str] = None
    latency_ms: Optional[float] = None
    details: dict[str, Any] = Field(default_factory=dict)


class NetworkConnectivityConfig(BaseModel):
    """Configuration for F18.3 network connectivity state hook."""

    enabled: bool = Field(default=True)
    check_hosts: list[tuple[str, int]] = Field(
        default_factory=lambda: [("1.1.1.1", 53), ("8.8.8.8", 53)]
    )
    timeout_seconds: float = Field(default=2.0, gt=0.0, le=30.0)
    consecutive_failures_for_offline: int = Field(default=1, ge=1, le=20)
    consecutive_successes_for_online: int = Field(default=1, ge=1, le=20)
    emit_initial_state: bool = Field(default=False)


def default_network_probe(
    check_hosts: Optional[list[tuple[str, int]]] = None,
    timeout_seconds: float = 2.0,
) -> NetworkProbeResult:
    """Checks active non-loopback interfaces and TCP reachability."""
    import psutil

    active_ifaces: list[str] = []
    try:
        for name, stats in psutil.net_if_stats().items():
            if stats.isup and not name.lower().startswith(("lo", "loopback")):
                active_ifaces.append(name)
    except Exception:
        pass

    hosts = check_hosts or [("1.1.1.1", 53), ("8.8.8.8", 53)]
    for host, port in hosts:
        start = time.perf_counter()
        try:
            with socket.create_connection((host, port), timeout=timeout_seconds):
                latency_ms = round((time.perf_counter() - start) * 1000.0, 2)
                return NetworkProbeResult(
                    online=True,
                    interface=active_ifaces[0] if active_ifaces else None,
                    latency_ms=latency_ms,
                    details={"host": host, "port": port, "active_interfaces": active_ifaces},
                )
        except OSError:
            continue

    return NetworkProbeResult(
        online=False,
        interface=active_ifaces[0] if active_ifaces else None,
        details={"active_interfaces": active_ifaces},
    )


class NetworkConnectivityHook:
    """F18.3: Detects ``online`` <-> ``offline`` transitions and runs hooks."""

    def __init__(
        self,
        config: Optional[NetworkConnectivityConfig] = None,
        *,
        probe: Optional[Callable[[], NetworkProbeResult | bool]] = None,
        device_id: str = "desktop_agent_client",
        utc_now: Optional[Callable[[], datetime]] = None,
        on_online: Optional[Callable[[SystemTriggerEvent], Any]] = None,
        on_offline: Optional[Callable[[SystemTriggerEvent], Any]] = None,
        callbacks: Optional[list[Callable[[SystemTriggerEvent], Any]]] = None,
    ) -> None:
        self.config = config or NetworkConnectivityConfig()
        self._probe = probe or (
            lambda: default_network_probe(
                check_hosts=self.config.check_hosts,
                timeout_seconds=self.config.timeout_seconds,
            )
        )
        self.device_id = device_id
        self._utc_now = utc_now or _default_utc_now
        self._callbacks: list[Callable[[SystemTriggerEvent], Any]] = list(callbacks or [])
        self._state_hooks: dict[NetworkState, list[Callable[[SystemTriggerEvent], Any]]] = {
            NetworkState.ONLINE: [],
            NetworkState.OFFLINE: [],
        }
        if on_online is not None:
            self._state_hooks[NetworkState.ONLINE].append(on_online)
        if on_offline is not None:
            self._state_hooks[NetworkState.OFFLINE].append(on_offline)

        self.current_state: NetworkState = NetworkState.UNKNOWN
        self._pending_state: Optional[NetworkState] = None
        self._pending_count: int = 0
        self._last_probe: Optional[NetworkProbeResult] = None

    def register_hook(
        self,
        callback: Callable[[SystemTriggerEvent], Any],
        *,
        on_state: Optional[NetworkState] = None,
    ) -> None:
        """Registers a callback for all transitions or a specific target state."""
        if on_state is None:
            self._callbacks.append(callback)
        elif on_state in self._state_hooks:
            self._state_hooks[on_state].append(callback)
        else:
            raise ValueError(f"Unsupported hook target state: {on_state}")

    def _run_probe(self) -> NetworkProbeResult:
        raw = self._probe()
        if isinstance(raw, NetworkProbeResult):
            return raw
        return NetworkProbeResult(online=bool(raw))

    def _emit_transition(
        self,
        previous_state: NetworkState,
        new_state: NetworkState,
        probe_res: NetworkProbeResult,
    ) -> SystemTriggerEvent:
        is_online = new_state == NetworkState.ONLINE
        event = SystemTriggerEvent(
            trigger_type=TriggerType.NETWORK_CONNECTIVITY,
            event_name="network.online" if is_online else "network.offline",
            severity=TriggerSeverity.INFO if is_online else TriggerSeverity.WARNING,
            source_device_id=self.device_id,
            timestamp=self._utc_now(),
            message=(
                f"Network connectivity transitioned from {previous_state.value} to {new_state.value}."
            ),
            metrics={
                "online": is_online,
                "interface": probe_res.interface,
                "latency_ms": probe_res.latency_ms,
                **probe_res.details,
            },
            state_transition={"from": previous_state.value, "to": new_state.value},
        )
        _invoke_callbacks(self._callbacks, event)
        _invoke_callbacks(self._state_hooks[new_state], event)
        return event

    def check_once(self) -> list[SystemTriggerEvent]:
        if not self.config.enabled:
            return []

        probe_res = self._run_probe()
        self._last_probe = probe_res
        observed = NetworkState.ONLINE if probe_res.online else NetworkState.OFFLINE

        if self.current_state == NetworkState.UNKNOWN:
            self.current_state = observed
            self._pending_state = None
            self._pending_count = 0
            if self.config.emit_initial_state:
                return [self._emit_transition(NetworkState.UNKNOWN, observed, probe_res)]
            return []

        if observed == self.current_state:
            self._pending_state = None
            self._pending_count = 0
            return []

        # Potential transition: apply debounce threshold
        if self._pending_state == observed:
            self._pending_count += 1
        else:
            self._pending_state = observed
            self._pending_count = 1

        required = (
            self.config.consecutive_successes_for_online
            if observed == NetworkState.ONLINE
            else self.config.consecutive_failures_for_offline
        )
        if self._pending_count < required:
            return []

        prev = self.current_state
        self.current_state = observed
        self._pending_state = None
        self._pending_count = 0
        return [self._emit_transition(prev, observed, probe_res)]

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "state": self.current_state.value,
            "pending_state": self._pending_state.value if self._pending_state else None,
            "pending_count": self._pending_count,
            "last_probe": self._last_probe.model_dump() if self._last_probe else None,
        }


# ---------------------------------------------------------------------------
# F18.4 — Session Screen Lock/Unlock Monitor
# ---------------------------------------------------------------------------


class SessionLockState(str, Enum):
    LOCKED = "locked"
    UNLOCKED = "unlocked"
    UNKNOWN = "unknown"


class SessionLockConfig(BaseModel):
    """Configuration for F18.4 session lock/unlock monitor."""

    enabled: bool = Field(default=True)
    emit_initial_state: bool = Field(default=False)
    ignore_unknown: bool = Field(default=True)


@runtime_checkable
class SessionLockProvider(Protocol):
    """Cross-platform contract for querying current screen lock state."""

    def get_lock_state(self) -> SessionLockState:
        ...


class CallableSessionLockProvider:
    """Wraps a callable returning ``SessionLockState``, ``bool``, or ``None``."""

    def __init__(self, fn: Callable[[], SessionLockState | bool | None]) -> None:
        self._fn = fn

    def get_lock_state(self) -> SessionLockState:
        val = self._fn()
        if isinstance(val, SessionLockState):
            return val
        if val is True:
            return SessionLockState.LOCKED
        if val is False:
            return SessionLockState.UNLOCKED
        return SessionLockState.UNKNOWN


class LinuxSessionLockProvider:
    """Queries systemd ``loginctl`` or ``gnome-screensaver-command`` safely."""

    def __init__(
        self,
        runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> None:
        self._runner = runner or subprocess.run

    @staticmethod
    def parse_loginctl_output(output: str) -> SessionLockState:
        for line in output.splitlines():
            clean = line.strip().lower()
            if clean.startswith("lockedhint="):
                val = clean.split("=", 1)[1].strip()
                if val in ("yes", "true", "1"):
                    return SessionLockState.LOCKED
                if val in ("no", "false", "0"):
                    return SessionLockState.UNLOCKED
        return SessionLockState.UNKNOWN

    def get_lock_state(self) -> SessionLockState:
        # 1. Try loginctl show-session
        try:
            proc = self._runner(
                ["loginctl", "show-session", "self", "-p", "LockedHint"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                parsed = self.parse_loginctl_output(proc.stdout)
                if parsed != SessionLockState.UNKNOWN:
                    return parsed
        except Exception:
            pass

        # 2. Fallback: gnome-screensaver-command -q
        try:
            proc = self._runner(
                ["gnome-screensaver-command", "-q"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                out = proc.stdout.lower()
                if "is active" in out:
                    return SessionLockState.LOCKED
                if "is inactive" in out:
                    return SessionLockState.UNLOCKED
        except Exception:
            pass

        return SessionLockState.UNKNOWN


class MacOSSessionLockProvider:
    """Queries macOS ``ioreg`` for ``CGSSessionScreenIsLocked`` safely."""

    def __init__(
        self,
        runner: Optional[Callable[..., subprocess.CompletedProcess[str]]] = None,
    ) -> None:
        self._runner = runner or subprocess.run

    @staticmethod
    def parse_ioreg_output(output: str) -> SessionLockState:
        if "CGSSessionScreenIsLocked" not in output:
            return SessionLockState.UNLOCKED if output.strip() else SessionLockState.UNKNOWN
        for line in output.splitlines():
            if "CGSSessionScreenIsLocked" in line:
                lower = line.lower()
                if "yes" in lower or "true" in lower or "= 1" in lower:
                    return SessionLockState.LOCKED
                if "no" in lower or "false" in lower or "= 0" in lower:
                    return SessionLockState.UNLOCKED
        return SessionLockState.UNKNOWN

    def get_lock_state(self) -> SessionLockState:
        try:
            proc = self._runner(
                ["ioreg", "-n", "Root", "-d1"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout:
                return self.parse_ioreg_output(proc.stdout)
        except Exception:
            pass
        return SessionLockState.UNKNOWN


class WindowsSessionLockProvider:
    """Queries Windows desktop lock state via ``LogonUI.exe`` or Win32 API."""

    def __init__(
        self,
        process_names_provider: Optional[Callable[[], list[str]]] = None,
    ) -> None:
        self._process_names_provider = process_names_provider

    def _list_process_names(self) -> list[str]:
        if self._process_names_provider is not None:
            return self._process_names_provider()
        import psutil

        names: list[str] = []
        for proc in psutil.process_iter(["name"]):
            try:
                name = proc.info.get("name")
                if name:
                    names.append(str(name))
            except Exception:
                continue
        return names

    def get_lock_state(self) -> SessionLockState:
        try:
            names = [n.lower() for n in self._list_process_names()]
            if not names:
                return SessionLockState.UNKNOWN
            return (
                SessionLockState.LOCKED
                if "logonui.exe" in names
                else SessionLockState.UNLOCKED
            )
        except Exception:
            return SessionLockState.UNKNOWN


def get_default_session_lock_provider(system: Optional[str] = None) -> SessionLockProvider:
    """Factory returning the platform-specific ``SessionLockProvider``."""
    os_name = (system or platform.system()).strip().lower()
    if os_name == "windows":
        return WindowsSessionLockProvider()
    if os_name == "darwin":
        return MacOSSessionLockProvider()
    return LinuxSessionLockProvider()


class SessionLockMonitor:
    """F18.4: Monitors session screen ``locked`` <-> ``unlocked`` transitions."""

    def __init__(
        self,
        config: Optional[SessionLockConfig] = None,
        *,
        provider: Optional[SessionLockProvider | Callable[[], SessionLockState | bool | None]] = None,
        device_id: str = "desktop_agent_client",
        utc_now: Optional[Callable[[], datetime]] = None,
        on_lock: Optional[Callable[[SystemTriggerEvent], Any]] = None,
        on_unlock: Optional[Callable[[SystemTriggerEvent], Any]] = None,
        callbacks: Optional[list[Callable[[SystemTriggerEvent], Any]]] = None,
    ) -> None:
        self.config = config or SessionLockConfig()
        if provider is None:
            self._provider: SessionLockProvider = get_default_session_lock_provider()
        elif isinstance(provider, SessionLockProvider):
            self._provider = provider
        else:
            self._provider = CallableSessionLockProvider(provider)

        self.device_id = device_id
        self._utc_now = utc_now or _default_utc_now
        self._callbacks: list[Callable[[SystemTriggerEvent], Any]] = list(callbacks or [])
        self._on_lock: list[Callable[[SystemTriggerEvent], Any]] = [on_lock] if on_lock else []
        self._on_unlock: list[Callable[[SystemTriggerEvent], Any]] = [on_unlock] if on_unlock else []
        self.current_state: SessionLockState = SessionLockState.UNKNOWN

    def register_hook(
        self,
        callback: Callable[[SystemTriggerEvent], Any],
        *,
        on_state: Optional[SessionLockState] = None,
    ) -> None:
        if on_state is None:
            self._callbacks.append(callback)
        elif on_state == SessionLockState.LOCKED:
            self._on_lock.append(callback)
        elif on_state == SessionLockState.UNLOCKED:
            self._on_unlock.append(callback)
        else:
            raise ValueError(f"Unsupported session lock hook state: {on_state}")

    def _emit_transition(
        self,
        previous: SessionLockState,
        current: SessionLockState,
    ) -> SystemTriggerEvent:
        locked = current == SessionLockState.LOCKED
        event = SystemTriggerEvent(
            trigger_type=TriggerType.SESSION_LOCK,
            event_name="session.locked" if locked else "session.unlocked",
            severity=TriggerSeverity.INFO,
            source_device_id=self.device_id,
            timestamp=self._utc_now(),
            message=f"Desktop session transitioned from {previous.value} to {current.value}.",
            metrics={"locked": locked, "state": current.value},
            state_transition={"from": previous.value, "to": current.value},
        )
        _invoke_callbacks(self._callbacks, event)
        if locked:
            _invoke_callbacks(self._on_lock, event)
        elif current == SessionLockState.UNLOCKED:
            _invoke_callbacks(self._on_unlock, event)
        return event

    def check_once(self) -> list[SystemTriggerEvent]:
        if not self.config.enabled:
            return []

        try:
            observed = self._provider.get_lock_state()
        except Exception as exc:
            logger.warning("Session lock probe error: %s", exc)
            observed = SessionLockState.UNKNOWN

        if observed == SessionLockState.UNKNOWN and self.config.ignore_unknown:
            return []

        if self.current_state == SessionLockState.UNKNOWN:
            self.current_state = observed
            if self.config.emit_initial_state and observed != SessionLockState.UNKNOWN:
                return [self._emit_transition(SessionLockState.UNKNOWN, observed)]
            return []

        if observed == self.current_state:
            return []

        prev = self.current_state
        self.current_state = observed
        return [self._emit_transition(prev, observed)]

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "state": self.current_state.value,
        }


# ---------------------------------------------------------------------------
# F18.5 — Low Battery Alert Action Trigger Dispatcher
# ---------------------------------------------------------------------------


SAFE_BUILTIN_TRIGGER_ACTIONS: frozenset[str] = frozenset(
    {
        "notify",
        "emit_core_event",
        "log_alert",
        "power_saver_hint",
    }
)

BLOCKED_AUTONOMOUS_CATEGORIES: frozenset[str] = frozenset({"shell_command"})


class BatterySample(BaseModel):
    """Point-in-time battery reading."""

    available: bool = Field(default=True)
    percent: float = Field(default=100.0, ge=0.0, le=100.0)
    power_plugged: bool = Field(default=False)
    secs_left: Optional[int] = Field(default=None)


class BatteryTriggerAction(BaseModel):
    """Configured action to dispatch when a low-battery alert fires."""

    action_type: str = Field(..., min_length=1, max_length=128)
    category: str = Field(default="system_trigger", min_length=1, max_length=64)
    params: dict[str, Any] = Field(default_factory=dict)
    min_severity: TriggerSeverity = Field(default=TriggerSeverity.WARNING)
    enabled: bool = Field(default=True)


class LowBatteryConfig(BaseModel):
    """Configuration for F18.5 low-battery monitoring & action dispatch."""

    enabled: bool = Field(default=True)
    warning_percent: float = Field(default=20.0, ge=1.0, le=100.0)
    critical_percent: float = Field(default=10.0, ge=1.0, le=100.0)
    hysteresis_percent: float = Field(default=5.0, ge=0.0, le=50.0)
    ignore_when_plugged: bool = Field(default=True)
    cooldown_seconds: float = Field(default=300.0, ge=0.0)
    emit_recovery: bool = Field(default=True)
    actions: list[BatteryTriggerAction] = Field(
        default_factory=lambda: [
            BatteryTriggerAction(
                action_type="notify",
                category="system_trigger",
                params={"title": "Low Battery Alert"},
                min_severity=TriggerSeverity.WARNING,
            )
        ]
    )

    @model_validator(mode="after")
    def _validate_thresholds(self) -> "LowBatteryConfig":
        if self.critical_percent > self.warning_percent:
            raise ValueError(
                f"critical_percent ({self.critical_percent}) must be <= "
                f"warning_percent ({self.warning_percent})."
            )
        return self


class ActionDispatchResult(BaseModel):
    """Outcome of dispatching a single trigger action."""

    action_type: str
    category: str
    dispatched: bool
    status: Literal["executed", "forwarded_to_core", "blocked", "skipped", "failed"]
    reason: Optional[str] = None
    result: dict[str, Any] = Field(default_factory=dict)


def default_battery_probe() -> BatterySample:
    """Default battery probe backed by ``psutil.sensors_battery``."""
    import psutil

    bat = psutil.sensors_battery()
    if bat is None:
        return BatterySample(available=False, percent=100.0, power_plugged=True, secs_left=None)
    secs = bat.secsleft if isinstance(bat.secsleft, int) and bat.secsleft >= 0 else None
    return BatterySample(
        available=True,
        percent=float(bat.percent),
        power_plugged=bool(bat.power_plugged),
        secs_left=secs,
    )


class SafeActionDispatcher:
    """Validates and safely dispatches actions triggered by system events (F18.5).

    Security guarantees:
    1. ``shell_command`` category is unconditionally blocked from autonomous
       background trigger dispatch (Nova security floor: shell commands always
       require explicit interactive confirmation).
    2. Built-in safe trigger actions (``notify``, ``emit_core_event``,
       ``log_alert``, ``power_saver_hint``) execute via safe handlers or
       forward structured events to ``nova_core``.
    3. Local desktop agent actions (e.g. ``read_only_info``, ``app_control``)
       are validated against ``PermissionRegistry``; any action that is denied
       or requires interactive confirmation is blocked from autonomous execution.
    """

    def __init__(
        self,
        *,
        permission_registry: Any = None,
        action_executor: Any = None,
        core_event_sender: Optional[Callable[[SystemTriggerEvent], Any]] = None,
        builtin_handlers: Optional[
            dict[str, Callable[[BatteryTriggerAction, SystemTriggerEvent], dict[str, Any]]]
        ] = None,
    ) -> None:
        self.permission_registry = permission_registry
        self.action_executor = action_executor
        self.core_event_sender = core_event_sender
        self._builtin_handlers: dict[
            str, Callable[[BatteryTriggerAction, SystemTriggerEvent], dict[str, Any]]
        ] = {
            "notify": self._handle_notify,
            "log_alert": self._handle_log_alert,
            "power_saver_hint": self._handle_power_saver_hint,
        }
        if builtin_handlers:
            self._builtin_handlers.update(builtin_handlers)

    @staticmethod
    def _handle_notify(
        action: BatteryTriggerAction, event: SystemTriggerEvent
    ) -> dict[str, Any]:
        title = action.params.get("title", f"Nova System Alert ({event.severity.value.upper()})")
        body = action.params.get("message") or event.message
        logger.warning("[SYSTEM TRIGGER NOTIFY] %s: %s", title, body)
        return {"title": title, "message": body, "severity": event.severity.value}

    @staticmethod
    def _handle_log_alert(
        action: BatteryTriggerAction, event: SystemTriggerEvent
    ) -> dict[str, Any]:
        logger.warning("[SYSTEM TRIGGER LOG] %s (%s)", event.event_name, event.message)
        return {"logged": True, "event_id": event.event_id}

    @staticmethod
    def _handle_power_saver_hint(
        action: BatteryTriggerAction, event: SystemTriggerEvent
    ) -> dict[str, Any]:
        profile = action.params.get("profile", "low_power")
        return {
            "profile": profile,
            "recommended": True,
            "battery_percent": event.metrics.get("percent"),
        }

    def dispatch(
        self,
        action: BatteryTriggerAction,
        event: SystemTriggerEvent,
    ) -> ActionDispatchResult:
        if not action.enabled:
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="skipped",
                reason="Action is disabled.",
            )

        if not severity_at_least(event.severity, action.min_severity):
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="skipped",
                reason=(
                    f"Event severity '{event.severity.value}' is below action "
                    f"min_severity '{action.min_severity.value}'."
                ),
            )

        # 1. Security floor: never run raw shell commands autonomously
        if action.category.strip().lower() in BLOCKED_AUTONOMOUS_CATEGORIES:
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="blocked",
                reason=(
                    "Autonomous execution of 'shell_command' actions is blocked; "
                    "shell commands unconditionally require interactive user confirmation."
                ),
            )

        # 2. Forward to nova_core event endpoint
        if action.action_type == "emit_core_event":
            if self.core_event_sender is None:
                return ActionDispatchResult(
                    action_type=action.action_type,
                    category=action.category,
                    dispatched=False,
                    status="skipped",
                    reason="No core_event_sender configured.",
                )
            try:
                res = self.core_event_sender(event)
                return ActionDispatchResult(
                    action_type=action.action_type,
                    category=action.category,
                    dispatched=True,
                    status="forwarded_to_core",
                    result=res if isinstance(res, dict) else {"response": res},
                )
            except Exception as exc:
                return ActionDispatchResult(
                    action_type=action.action_type,
                    category=action.category,
                    dispatched=False,
                    status="failed",
                    reason=f"Failed forwarding event to nova_core: {exc}",
                )

        # 3. Safe built-in system trigger actions
        if action.category == "system_trigger" and action.action_type in self._builtin_handlers:
            try:
                out = self._builtin_handlers[action.action_type](action, event)
                return ActionDispatchResult(
                    action_type=action.action_type,
                    category=action.category,
                    dispatched=True,
                    status="executed",
                    result=out,
                )
            except Exception as exc:
                return ActionDispatchResult(
                    action_type=action.action_type,
                    category=action.category,
                    dispatched=False,
                    status="failed",
                    reason=f"Built-in handler failed: {exc}",
                )

        # 4. Desktop agent action via PermissionRegistry + ActionExecutor
        registry = self.permission_registry or (
            getattr(self.action_executor, "permission_registry", None)
        )
        if registry is None:
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="blocked",
                reason=(
                    f"Action '{action.category}.{action.action_type}' cannot be dispatched "
                    "without a PermissionRegistry for safety validation."
                ),
            )

        req = ActionRequest(
            action_type=action.action_type,
            category=action.category,
            params=dict(action.params),
            source_device_id=event.source_device_id,
            origin="local_match",
        )
        decision = registry.check(req)
        if not decision.allowed:
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="blocked",
                reason=f"Permission denied: {decision.reason}",
            )
        if decision.requires_confirmation:
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="blocked",
                reason=(
                    "Action requires interactive confirmation and cannot run autonomously: "
                    f"{decision.reason}"
                ),
            )

        if self.action_executor is None:
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="skipped",
                reason="Permission verified, but no ActionExecutor is attached.",
            )

        try:
            exec_res = self.action_executor.execute(req)
            ok = bool(exec_res.get("success", False))
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=ok,
                status="executed" if ok else "failed",
                reason=None if ok else str(exec_res.get("error", "Execution failed")),
                result=exec_res,
            )
        except Exception as exc:
            return ActionDispatchResult(
                action_type=action.action_type,
                category=action.category,
                dispatched=False,
                status="failed",
                reason=f"ActionExecutor raised error: {exc}",
            )


class LowBatteryTrigger:
    """F18.5: Low-battery monitor with ``power_plugged`` check & safe action dispatch."""

    def __init__(
        self,
        config: Optional[LowBatteryConfig] = None,
        *,
        probe: Optional[Callable[[], BatterySample | dict[str, Any]]] = None,
        dispatcher: Optional[SafeActionDispatcher] = None,
        device_id: str = "desktop_agent_client",
        clock: Optional[Callable[[], float]] = None,
        utc_now: Optional[Callable[[], datetime]] = None,
        callbacks: Optional[list[Callable[[SystemTriggerEvent], Any]]] = None,
    ) -> None:
        self.config = config or LowBatteryConfig()
        self._probe = probe or default_battery_probe
        self.dispatcher = dispatcher or SafeActionDispatcher()
        self.device_id = device_id
        self._clock = clock or time.monotonic
        self._utc_now = utc_now or _default_utc_now
        self._callbacks: list[Callable[[SystemTriggerEvent], Any]] = list(callbacks or [])

        self._level: Literal["ok", "warning", "critical"] = "ok"
        self._last_alert_at: Optional[float] = None
        self._last_sample: Optional[BatterySample] = None

    def register_callback(self, callback: Callable[[SystemTriggerEvent], Any]) -> None:
        self._callbacks.append(callback)

    def _run_probe(self) -> BatterySample:
        raw = self._probe()
        if isinstance(raw, BatterySample):
            return raw
        return BatterySample(**raw)

    def check_once(self) -> list[SystemTriggerEvent]:
        if not self.config.enabled:
            return []

        sample = self._run_probe()
        self._last_sample = sample
        if not sample.available:
            return []

        now_mono = self._clock()
        now_utc = self._utc_now()
        prev_level = self._level

        # 1. Plugged-in recovery / suppression
        if sample.power_plugged and self.config.ignore_when_plugged:
            if prev_level != "ok":
                self._level = "ok"
                self._last_alert_at = None
                if self.config.emit_recovery:
                    event = SystemTriggerEvent(
                        trigger_type=TriggerType.LOW_BATTERY,
                        event_name="battery.recovered",
                        severity=TriggerSeverity.INFO,
                        source_device_id=self.device_id,
                        timestamp=now_utc,
                        message=(
                            f"Battery recovered (charger plugged in at {sample.percent:.1f}%)."
                        ),
                        metrics={
                            "percent": sample.percent,
                            "power_plugged": True,
                            "secs_left": sample.secs_left,
                            "recovery_reason": "power_plugged",
                        },
                        state_transition={"from": prev_level, "to": "ok"},
                    )
                    _invoke_callbacks(self._callbacks, event)
                    return [event]
            return []

        # 2. Evaluate battery percentage thresholds
        if sample.percent <= self.config.critical_percent:
            raw_level: Literal["ok", "warning", "critical"] = "critical"
        elif sample.percent <= self.config.warning_percent:
            raw_level = "warning"
        else:
            raw_level = "ok"

        if raw_level == "ok":
            recover_at = min(100.0, self.config.warning_percent + self.config.hysteresis_percent)
            if prev_level != "ok" and sample.percent >= recover_at:
                self._level = "ok"
                self._last_alert_at = None
                if self.config.emit_recovery:
                    event = SystemTriggerEvent(
                        trigger_type=TriggerType.LOW_BATTERY,
                        event_name="battery.recovered",
                        severity=TriggerSeverity.INFO,
                        source_device_id=self.device_id,
                        timestamp=now_utc,
                        message=f"Battery level recovered to {sample.percent:.1f}%.",
                        metrics={
                            "percent": sample.percent,
                            "power_plugged": sample.power_plugged,
                            "secs_left": sample.secs_left,
                            "recovery_reason": "threshold_recovered",
                        },
                        state_transition={"from": prev_level, "to": "ok"},
                    )
                    _invoke_callbacks(self._callbacks, event)
                    return [event]
            return []

        is_new_breach = prev_level == "ok"
        is_escalation = prev_level == "warning" and raw_level == "critical"
        cooldown_elapsed = (
            self._last_alert_at is None
            or (now_mono - self._last_alert_at) >= self.config.cooldown_seconds
        )
        self._level = raw_level

        if not (is_new_breach or is_escalation or cooldown_elapsed):
            return []

        self._last_alert_at = now_mono
        severity = (
            TriggerSeverity.CRITICAL if raw_level == "critical" else TriggerSeverity.WARNING
        )
        event_name = "battery.critical" if raw_level == "critical" else "battery.low"
        event = SystemTriggerEvent(
            trigger_type=TriggerType.LOW_BATTERY,
            event_name=event_name,
            severity=severity,
            source_device_id=self.device_id,
            timestamp=now_utc,
            message=(
                f"Battery {raw_level}: {sample.percent:.1f}% remaining "
                f"({'plugged in' if sample.power_plugged else 'on battery'})."
            ),
            metrics={
                "percent": sample.percent,
                "power_plugged": sample.power_plugged,
                "secs_left": sample.secs_left,
                "warning_percent": self.config.warning_percent,
                "critical_percent": self.config.critical_percent,
            },
            state_transition=(
                {"from": prev_level, "to": raw_level} if prev_level != raw_level else None
            ),
        )

        # Dispatch configured actions through SafeActionDispatcher
        dispatch_results = [
            self.dispatcher.dispatch(action, event).model_dump()
            for action in self.config.actions
        ]
        event.metadata["dispatched_actions"] = dispatch_results
        _invoke_callbacks(self._callbacks, event)
        return [event]

    def get_status(self) -> dict[str, Any]:
        return {
            "enabled": self.config.enabled,
            "level": self._level,
            "last_sample": self._last_sample.model_dump() if self._last_sample else None,
        }


# ---------------------------------------------------------------------------
# Unified Configuration, Core HTTP Forwarder & Coordinator Manager
# ---------------------------------------------------------------------------


class SystemTriggersConfig(BaseModel):
    """Unified configuration for all Phase 18 platform system triggers."""

    enabled: bool = Field(default=True)
    poll_interval_seconds: float = Field(default=15.0, ge=0.1, le=3600.0)
    disk: DiskThresholdConfig = Field(default_factory=DiskThresholdConfig)
    resources: ResourceThresholdConfig = Field(default_factory=ResourceThresholdConfig)
    network: NetworkConnectivityConfig = Field(default_factory=NetworkConnectivityConfig)
    session_lock: SessionLockConfig = Field(default_factory=SessionLockConfig)
    battery: LowBatteryConfig = Field(default_factory=LowBatteryConfig)


class CoreTriggerClient:
    """HTTP client helper for forwarding ``SystemTriggerEvent`` payloads to ``nova_core``."""

    def __init__(
        self,
        server_url: str = "http://127.0.0.1:8000",
        *,
        auth_token: Optional[str] = None,
        timeout_seconds: float = 5.0,
        http_post: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.auth_token = auth_token
        self.timeout_seconds = timeout_seconds
        self._http_post = http_post

    def send_event(self, event: SystemTriggerEvent) -> dict[str, Any]:
        url = f"{self.server_url}/api/v1/triggers/system/events"
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"

        payload = event.model_dump(mode="json")
        post_fn = self._http_post
        if post_fn is None:
            import requests

            post_fn = requests.post

        response = post_fn(url, json=payload, headers=headers, timeout=self.timeout_seconds)
        status_code = getattr(response, "status_code", 200)
        if 200 <= status_code < 300:
            return response.json() if hasattr(response, "json") else {"success": True}
        raise RuntimeError(f"nova_core returned HTTP {status_code} for {url}")


class SystemTriggerManager:
    """Coordinates F18.1–F18.5 listeners, history tracking, and core forwarding."""

    def __init__(
        self,
        config: Optional[SystemTriggersConfig] = None,
        *,
        device_id: str = "desktop_agent_client",
        clock: Optional[Callable[[], float]] = None,
        utc_now: Optional[Callable[[], datetime]] = None,
        disk_probe: Optional[Callable[[str], DiskUsageSample]] = None,
        resource_probe: Optional[Callable[[], ResourceSample]] = None,
        network_probe: Optional[Callable[[], NetworkProbeResult | bool]] = None,
        session_lock_provider: Optional[
            SessionLockProvider | Callable[[], SessionLockState | bool | None]
        ] = None,
        battery_probe: Optional[Callable[[], BatterySample | dict[str, Any]]] = None,
        dispatcher: Optional[SafeActionDispatcher] = None,
        core_event_sender: Optional[Callable[[SystemTriggerEvent], Any]] = None,
        history_limit: int = 200,
    ) -> None:
        self.config = config or SystemTriggersConfig()
        self.device_id = device_id
        self._clock = clock or time.monotonic
        self._utc_now = utc_now or _default_utc_now
        self.core_event_sender = core_event_sender
        self.dispatcher = dispatcher or SafeActionDispatcher(
            core_event_sender=core_event_sender
        )

        self.disk_listener = DiskUsageListener(
            config=self.config.disk,
            probe=disk_probe,
            device_id=device_id,
            clock=self._clock,
            utc_now=self._utc_now,
        )
        self.resource_trigger = ResourceUtilizationTrigger(
            config=self.config.resources,
            probe=resource_probe,
            device_id=device_id,
            clock=self._clock,
            utc_now=self._utc_now,
        )
        self.network_hook = NetworkConnectivityHook(
            config=self.config.network,
            probe=network_probe,
            device_id=device_id,
            utc_now=self._utc_now,
        )
        self.session_monitor = SessionLockMonitor(
            config=self.config.session_lock,
            provider=session_lock_provider,
            device_id=device_id,
            utc_now=self._utc_now,
        )
        self.battery_trigger = LowBatteryTrigger(
            config=self.config.battery,
            probe=battery_probe,
            dispatcher=self.dispatcher,
            device_id=device_id,
            clock=self._clock,
            utc_now=self._utc_now,
        )

        self._callbacks: list[Callable[[SystemTriggerEvent], Any]] = []
        self._history: deque[SystemTriggerEvent] = deque(maxlen=max(1, history_limit))
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def register_callback(self, callback: Callable[[SystemTriggerEvent], Any]) -> None:
        self._callbacks.append(callback)

    def update_config(self, new_config: SystemTriggersConfig) -> SystemTriggersConfig:
        with self._lock:
            self.config = new_config
            self.disk_listener.config = new_config.disk
            self.resource_trigger.config = new_config.resources
            self.network_hook.config = new_config.network
            self.session_monitor.config = new_config.session_lock
            self.battery_trigger.config = new_config.battery
            return self.config

    def poll_once(self) -> list[SystemTriggerEvent]:
        """Runs a single evaluation pass across all enabled Phase 18 listeners."""
        if not self.config.enabled:
            return []

        emitted: list[SystemTriggerEvent] = []
        with self._lock:
            for listener in (
                self.disk_listener,
                self.resource_trigger,
                self.network_hook,
                self.session_monitor,
                self.battery_trigger,
            ):
                try:
                    emitted.extend(listener.check_once())
                except Exception as exc:
                    logger.error(
                        "System trigger listener '%s' failed during poll_once: %s",
                        type(listener).__name__,
                        exc,
                    )

            for event in emitted:
                self._history.append(event)

        for event in emitted:
            _invoke_callbacks(self._callbacks, event)
            if self.core_event_sender is not None:
                try:
                    self.core_event_sender(event)
                except Exception as exc:
                    logger.error(
                        "Failed forwarding system trigger event '%s' to nova_core: %s",
                        event.event_name,
                        exc,
                    )

        return emitted

    def get_history(self, limit: int = 50) -> list[SystemTriggerEvent]:
        with self._lock:
            items = list(self._history)
        return items[-limit:] if limit > 0 else []

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": self.config.enabled,
                "device_id": self.device_id,
                "poll_interval_seconds": self.config.poll_interval_seconds,
                "history_count": len(self._history),
                "triggers": {
                    "disk": self.disk_listener.get_status(),
                    "resources": self.resource_trigger.get_status(),
                    "network": self.network_hook.get_status(),
                    "session_lock": self.session_monitor.get_status(),
                    "battery": self.battery_trigger.get_status(),
                },
            }

    def start(self) -> None:
        """Starts a background daemon polling thread."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()

        def _loop() -> None:
            while not self._stop_event.is_set():
                self.poll_once()
                self._stop_event.wait(timeout=self.config.poll_interval_seconds)

        self._thread = threading.Thread(
            target=_loop, name="nova-system-triggers", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        """Stops the background polling thread cleanly."""
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=timeout)
        self._thread = None
