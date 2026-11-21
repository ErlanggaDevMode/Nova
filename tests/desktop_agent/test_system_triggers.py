"""Comprehensive tests for Phase 18: Platform System Event Triggers (F18.1–F18.5).

All tests are 100% deterministic with injected probes and virtual clocks —
zero dependence on host hardware, real network sockets, or real OS lock state.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from desktop_agent.permission_registry import PermissionRegistry
from desktop_agent.system_triggers import (
    BatterySample,
    BatteryTriggerAction,
    CoreTriggerClient,
    DiskThresholdConfig,
    DiskUsageListener,
    DiskUsageSample,
    LinuxSessionLockProvider,
    LowBatteryConfig,
    LowBatteryTrigger,
    MacOSSessionLockProvider,
    NetworkConnectivityConfig,
    NetworkConnectivityHook,
    NetworkProbeResult,
    NetworkState,
    ResourceSample,
    ResourceThresholdConfig,
    ResourceUtilizationTrigger,
    SafeActionDispatcher,
    SessionLockConfig,
    SessionLockMonitor,
    SessionLockState,
    SystemTriggerEvent,
    SystemTriggerManager,
    SystemTriggersConfig,
    TriggerSeverity,
    TriggerType,
    WindowsSessionLockProvider,
    get_default_session_lock_provider,
)
from nova_core.api.routes_auth import router as auth_router
from nova_core.api.routes_system_triggers import router as system_triggers_router
from nova_core.auth import get_current_user
from nova_core.db.store import DatabaseStore


class FakeClock:
    """Deterministic monotonic + UTC clock for cooldown and timestamp testing."""

    def __init__(self, start_mono: float = 1000.0) -> None:
        self._mono = start_mono
        self._utc = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)

    def monotonic(self) -> float:
        return self._mono

    def utc_now(self) -> datetime:
        return self._utc

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._utc += timedelta(seconds=seconds)


def _disk_sample(mount: str, percent: float, free_gb: float, total_gb: float = 100.0) -> DiskUsageSample:
    gb = 1024**3
    total = int(total_gb * gb)
    free = int(free_gb * gb)
    used = max(0, total - free)
    return DiskUsageSample(
        mount_path=mount,
        total_bytes=total,
        used_bytes=used,
        free_bytes=free,
        percent=percent,
    )


# ---------------------------------------------------------------------------
# F18.1 — Disk Usage Threshold Listener
# ---------------------------------------------------------------------------


def test_f18_1_disk_threshold_hysteresis_cooldown_and_recovery():
    clock = FakeClock()
    readings: list[DiskUsageSample] = [
        _disk_sample("/", percent=70.0, free_gb=30.0),  # ok
        _disk_sample("/", percent=86.0, free_gb=14.0),  # warning -> fires immediately
        _disk_sample("/", percent=87.0, free_gb=13.0),  # still warning within cooldown -> suppressed
        _disk_sample("/", percent=83.0, free_gb=17.0),  # inside hysteresis deadband (85 - 3 = 82) -> holds warning, no recovery
        _disk_sample("/", percent=86.0, free_gb=14.0),  # after cooldown -> re-fires
        _disk_sample("/", percent=96.0, free_gb=4.0),   # escalation warning -> critical bypasses cooldown!
        _disk_sample("/", percent=80.0, free_gb=20.0),  # below 82% -> recovers!
    ]
    idx = {"i": 0}

    def probe(mount: str) -> DiskUsageSample:
        sample = readings[idx["i"]]
        idx["i"] += 1
        return sample

    callback_events: list[SystemTriggerEvent] = []
    listener = DiskUsageListener(
        config=DiskThresholdConfig(
            mount_paths=["/"],
            warning_percent=85.0,
            critical_percent=95.0,
            hysteresis_percent=3.0,
            cooldown_seconds=60.0,
        ),
        probe=probe,
        clock=clock.monotonic,
        utc_now=clock.utc_now,
        callbacks=[callback_events.append],
    )

    # 1. Below threshold -> no event
    assert listener.check_once() == []

    # 2. Breaches warning -> fires
    ev1 = listener.check_once()
    assert len(ev1) == 1
    assert ev1[0].trigger_type == TriggerType.DISK_USAGE
    assert ev1[0].event_name == "disk.threshold_exceeded"
    assert ev1[0].severity == TriggerSeverity.WARNING
    assert ev1[0].state_transition == {"from": "ok", "to": "warning"}

    # 3. Within cooldown (t + 20s) -> suppressed
    clock.advance(20.0)
    assert listener.check_once() == []

    # 4. Drops to 83% (inside hysteresis deadband > 82%) -> does NOT recover
    clock.advance(10.0)
    assert listener.check_once() == []
    assert listener.get_status()["mounts"]["/"]["level"] == "warning"

    # 5. Advance past cooldown (total +70s since alert) -> re-fires warning
    clock.advance(40.0)
    ev2 = listener.check_once()
    assert len(ev2) == 1
    assert ev2[0].severity == TriggerSeverity.WARNING

    # 6. Only +5s later, spikes to 96% -> escalates to CRITICAL immediately!
    clock.advance(5.0)
    ev3 = listener.check_once()
    assert len(ev3) == 1
    assert ev3[0].severity == TriggerSeverity.CRITICAL
    assert ev3[0].state_transition == {"from": "warning", "to": "critical"}

    # 7. Drops to 80% (<= 82%) -> emits recovery event
    clock.advance(5.0)
    ev4 = listener.check_once()
    assert len(ev4) == 1
    assert ev4[0].event_name == "disk.recovered"
    assert ev4[0].severity == TriggerSeverity.INFO
    assert ev4[0].state_transition == {"from": "critical", "to": "ok"}
    assert len(callback_events) == 4


def test_f18_1_disk_free_gb_multi_mount_and_probe_error_resilience():
    clock = FakeClock()

    def probe(mount: str) -> DiskUsageSample:
        if mount == "/broken":
            raise OSError("Mount point unavailable")
        if mount == "/data":
            return _disk_sample("/data", percent=60.0, free_gb=4.0, total_gb=10.0)
        return _disk_sample("/", percent=50.0, free_gb=50.0, total_gb=100.0)

    listener = DiskUsageListener(
        config=DiskThresholdConfig(
            mount_paths=["/", "/broken", "/data"],
            warning_percent=90.0,
            critical_percent=95.0,
            min_free_gb=5.0,
            critical_free_gb=2.0,
            hysteresis_free_gb=1.0,
        ),
        probe=probe,
        clock=clock.monotonic,
        utc_now=clock.utc_now,
    )

    events = listener.check_once()
    # /data triggers via min_free_gb even though percent (60%) is below warning_percent (90%)
    assert len(events) == 1
    assert events[0].metrics["mount_path"] == "/data"
    assert events[0].severity == TriggerSeverity.WARNING
    assert listener.get_status()["mounts"]["/broken"]["last_error"] == "Mount point unavailable"

    # Config validation checks
    with pytest.raises(ValueError, match="critical_percent"):
        DiskThresholdConfig(warning_percent=90.0, critical_percent=80.0)
    with pytest.raises(ValueError, match="critical_free_gb"):
        DiskThresholdConfig(min_free_gb=5.0, critical_free_gb=10.0)


# ---------------------------------------------------------------------------
# F18.2 — CPU & RAM Utilization Trigger (Sustained Samples)
# ---------------------------------------------------------------------------


def test_f18_2_cpu_and_ram_rejects_transient_spikes_and_fires_when_sustained():
    clock = FakeClock()
    samples = iter(
        [
            # Transient CPU spike (2 samples < 3 required), then dip -> must NOT trigger
            ResourceSample(cpu_percent=92.0, ram_percent=50.0),
            ResourceSample(cpu_percent=93.0, ram_percent=50.0),
            ResourceSample(cpu_percent=40.0, ram_percent=50.0),  # resets counter!
            # Sustained CPU warning (3 samples >= 80%) + RAM starts breaching
            ResourceSample(cpu_percent=85.0, ram_percent=88.0, ram_used_gb=14.1, ram_total_gb=16.0),
            ResourceSample(cpu_percent=86.0, ram_percent=90.0, ram_used_gb=14.4, ram_total_gb=16.0),
            ResourceSample(cpu_percent=87.0, ram_percent=96.0, ram_used_gb=15.4, ram_total_gb=16.0),
            # CPU escalates to critical on next sample
            ResourceSample(cpu_percent=97.0, ram_percent=96.0),
            # Both recover below hysteresis (80 - 5 = 75%)
            ResourceSample(cpu_percent=70.0, ram_percent=70.0),
        ]
    )

    trigger = ResourceUtilizationTrigger(
        config=ResourceThresholdConfig(
            cpu_warning_percent=80.0,
            cpu_critical_percent=95.0,
            ram_warning_percent=85.0,
            ram_critical_percent=95.0,
            consecutive_samples=3,
            hysteresis_percent=5.0,
            cooldown_seconds=120.0,
        ),
        probe=lambda: next(samples),
        clock=clock.monotonic,
        utc_now=clock.utc_now,
    )

    # First 3 samples: transient spike rejected
    assert trigger.check_once() == []
    assert trigger.check_once() == []
    assert trigger.check_once() == []
    assert trigger.get_status()["cpu"]["consecutive_breaches"] == 0

    # Samples 4 & 5: building up sustained count (1, 2)
    assert trigger.check_once() == []
    assert trigger.check_once() == []

    # Sample 6: 3rd consecutive sample -> BOTH CPU (warning) and RAM (critical) fire!
    ev_sustained = trigger.check_once()
    assert len(ev_sustained) == 2
    by_type = {e.trigger_type: e for e in ev_sustained}
    assert by_type[TriggerType.CPU_UTILIZATION].severity == TriggerSeverity.WARNING
    assert by_type[TriggerType.CPU_UTILIZATION].metrics["consecutive_samples"] == 3
    assert by_type[TriggerType.RAM_UTILIZATION].severity == TriggerSeverity.CRITICAL
    assert by_type[TriggerType.RAM_UTILIZATION].metrics["ram_used_gb"] == 15.4

    # Sample 7: CPU escalates warning -> critical (bypasses cooldown); RAM suppressed by cooldown
    clock.advance(10.0)
    ev_esc = trigger.check_once()
    assert len(ev_esc) == 1
    assert ev_esc[0].trigger_type == TriggerType.CPU_UTILIZATION
    assert ev_esc[0].severity == TriggerSeverity.CRITICAL

    # Sample 8: Both drop to 70% (<= 75% / 80%) -> both emit recovery events
    clock.advance(10.0)
    ev_rec = trigger.check_once()
    assert len(ev_rec) == 2
    assert {e.event_name for e in ev_rec} == {"cpu.recovered", "ram.recovered"}

    with pytest.raises(ValueError, match="cpu_critical_percent"):
        ResourceThresholdConfig(cpu_warning_percent=90.0, cpu_critical_percent=80.0)
    with pytest.raises(ValueError, match="ram_critical_percent"):
        ResourceThresholdConfig(ram_warning_percent=90.0, ram_critical_percent=80.0)


# ---------------------------------------------------------------------------
# F18.3 — Network Connectivity Online/Offline State Change Hook
# ---------------------------------------------------------------------------


def test_f18_3_network_online_offline_transitions_and_debounce():
    clock = FakeClock()
    states = iter(
        [
            NetworkProbeResult(online=True, interface="eth0", latency_ms=12.4),   # initial baseline
            NetworkProbeResult(online=False, interface="eth0"),                   # 1st failure (debounced)
            NetworkProbeResult(online=True, interface="eth0", latency_ms=11.0),   # blip recovered -> no offline event!
            NetworkProbeResult(online=False, interface="eth0"),                   # 1st failure
            NetworkProbeResult(online=False, interface="eth0"),                   # 2nd failure -> OFFLINE!
            NetworkProbeResult(online=False, interface="eth0"),                   # steady offline -> no duplicate
            NetworkProbeResult(online=True, interface="eth0", latency_ms=15.2),   # back ONLINE!
        ]
    )

    online_events: list[SystemTriggerEvent] = []
    offline_events: list[SystemTriggerEvent] = []
    all_events: list[SystemTriggerEvent] = []

    hook = NetworkConnectivityHook(
        config=NetworkConnectivityConfig(
            consecutive_failures_for_offline=2,
            consecutive_successes_for_online=1,
            emit_initial_state=False,
        ),
        probe=lambda: next(states),
        utc_now=clock.utc_now,
        on_online=online_events.append,
        on_offline=offline_events.append,
    )
    hook.register_hook(all_events.append)

    assert hook.check_once() == []  # learned online
    assert hook.current_state == NetworkState.ONLINE

    assert hook.check_once() == []  # 1st drop (needs 2)
    assert hook.check_once() == []  # recovered before debounce threshold

    assert hook.check_once() == []  # 1st drop
    ev_off = hook.check_once()      # 2nd consecutive drop -> fires!
    assert len(ev_off) == 1
    assert ev_off[0].event_name == "network.offline"
    assert ev_off[0].severity == TriggerSeverity.WARNING
    assert ev_off[0].state_transition == {"from": "online", "to": "offline"}
    assert len(offline_events) == 1

    assert hook.check_once() == []  # stays offline

    ev_on = hook.check_once()       # reconnects -> fires online!
    assert len(ev_on) == 1
    assert ev_on[0].event_name == "network.online"
    assert ev_on[0].severity == TriggerSeverity.INFO
    assert ev_on[0].state_transition == {"from": "offline", "to": "online"}
    assert ev_on[0].metrics["latency_ms"] == 15.2
    assert len(online_events) == 1
    assert len(all_events) == 2


def test_f18_3_network_bool_probe_and_emit_initial_state():
    hook = NetworkConnectivityHook(
        config=NetworkConnectivityConfig(emit_initial_state=True),
        probe=lambda: False,
    )
    events = hook.check_once()
    assert len(events) == 1
    assert events[0].event_name == "network.offline"
    assert events[0].state_transition == {"from": "unknown", "to": "offline"}


# ---------------------------------------------------------------------------
# F18.4 — Session Screen Lock/Unlock Monitor
# ---------------------------------------------------------------------------


def test_f18_4_session_lock_unlock_transitions_and_ignore_unknown():
    sequence = iter(
        [
            SessionLockState.UNLOCKED,  # baseline
            SessionLockState.UNKNOWN,   # transient probe failure -> ignored
            SessionLockState.LOCKED,    # unlocked -> locked
            SessionLockState.LOCKED,    # steady locked -> no duplicate
            SessionLockState.UNKNOWN,   # transient failure while locked -> ignored
            SessionLockState.UNLOCKED,  # locked -> unlocked
        ]
    )
    locked_cb: list[SystemTriggerEvent] = []
    unlocked_cb: list[SystemTriggerEvent] = []

    monitor = SessionLockMonitor(
        config=SessionLockConfig(emit_initial_state=False, ignore_unknown=True),
        provider=lambda: next(sequence),
        on_lock=locked_cb.append,
        on_unlock=unlocked_cb.append,
    )

    assert monitor.check_once() == []
    assert monitor.current_state == SessionLockState.UNLOCKED

    assert monitor.check_once() == []  # unknown ignored
    assert monitor.current_state == SessionLockState.UNLOCKED

    ev_lock = monitor.check_once()
    assert len(ev_lock) == 1
    assert ev_lock[0].event_name == "session.locked"
    assert ev_lock[0].state_transition == {"from": "unlocked", "to": "locked"}
    assert ev_lock[0].metrics["locked"] is True
    assert len(locked_cb) == 1

    assert monitor.check_once() == []  # steady
    assert monitor.check_once() == []  # unknown ignored

    ev_unlock = monitor.check_once()
    assert len(ev_unlock) == 1
    assert ev_unlock[0].event_name == "session.unlocked"
    assert ev_unlock[0].state_transition == {"from": "locked", "to": "unlocked"}
    assert ev_unlock[0].metrics["locked"] is False
    assert len(unlocked_cb) == 1


def test_f18_4_cross_platform_lock_providers():
    # Factory returns correct provider per OS name
    assert isinstance(get_default_session_lock_provider("Linux"), LinuxSessionLockProvider)
    assert isinstance(get_default_session_lock_provider("Darwin"), MacOSSessionLockProvider)
    assert isinstance(get_default_session_lock_provider("Windows"), WindowsSessionLockProvider)

    # Linux loginctl parser with mocked subprocess runner
    def fake_linux_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="LockedHint=yes\n", stderr="")

    linux_provider = LinuxSessionLockProvider(runner=fake_linux_runner)
    assert linux_provider.get_lock_state() == SessionLockState.LOCKED
    assert LinuxSessionLockProvider.parse_loginctl_output("LockedHint=no\n") == SessionLockState.UNLOCKED

    # macOS ioreg parser with mocked subprocess runner
    def fake_mac_runner(cmd, **kwargs):
        return subprocess.CompletedProcess(
            cmd,
            returncode=0,
            stdout='    "IOConsoleUsers" = ({"CGSSessionScreenIsLocked"=Yes})\n',
            stderr="",
        )

    mac_provider = MacOSSessionLockProvider(runner=fake_mac_runner)
    assert mac_provider.get_lock_state() == SessionLockState.LOCKED
    assert (
        MacOSSessionLockProvider.parse_ioreg_output('"IOConsoleUsers" = ({"CGSSessionScreenIsLocked"=No})')
        == SessionLockState.UNLOCKED
    )

    # Windows provider with injected process list
    win_locked = WindowsSessionLockProvider(process_names_provider=lambda: ["explorer.exe", "LogonUI.exe"])
    assert win_locked.get_lock_state() == SessionLockState.LOCKED
    win_unlocked = WindowsSessionLockProvider(process_names_provider=lambda: ["explorer.exe", "chrome.exe"])
    assert win_unlocked.get_lock_state() == SessionLockState.UNLOCKED


# ---------------------------------------------------------------------------
# F18.5 — Low Battery Alert Action Trigger Dispatcher
# ---------------------------------------------------------------------------


def test_f18_5_low_battery_thresholds_plugged_in_and_safe_action_dispatch():
    clock = FakeClock()
    forwarded_events: list[SystemTriggerEvent] = []
    executed_actions: list[dict[str, Any]] = []

    class FakeActionExecutor:
        def execute(self, action):
            executed_actions.append(action.model_dump())
            return {"success": True, "battery": {"percent": 9.0}}

    registry = PermissionRegistry()  # uses desktop_agent/policy.yaml
    dispatcher = SafeActionDispatcher(
        permission_registry=registry,
        action_executor=FakeActionExecutor(),
        core_event_sender=lambda ev: (forwarded_events.append(ev), {"accepted": True})[1],
    )

    actions = [
        BatteryTriggerAction(
            action_type="notify",
            category="system_trigger",
            params={"title": "Battery Warning"},
            min_severity=TriggerSeverity.WARNING,
        ),
        BatteryTriggerAction(
            action_type="emit_core_event",
            category="system_trigger",
            min_severity=TriggerSeverity.WARNING,
        ),
        BatteryTriggerAction(
            action_type="get_battery",
            category="read_only_info",
            params={},
            min_severity=TriggerSeverity.CRITICAL,
        ),
        # Unsafe autonomous shell command -> MUST be blocked!
        BatteryTriggerAction(
            action_type="run_script",
            category="shell_command",
            params={"command": "shutdown -h now"},
            min_severity=TriggerSeverity.WARNING,
        ),
        # Confirmation-required action (non-whitelisted app) -> MUST be blocked!
        BatteryTriggerAction(
            action_type="open_app",
            category="app_control",
            params={"app_name": "untrusted_random_binary"},
            min_severity=TriggerSeverity.WARNING,
        ),
    ]

    samples = iter(
        [
            BatterySample(available=False),                                 # desktop without battery -> no-op
            BatterySample(available=True, percent=15.0, power_plugged=True),   # plugged in -> suppressed!
            BatterySample(available=True, percent=18.0, power_plugged=False),  # WARNING!
            BatterySample(available=True, percent=17.0, power_plugged=False),  # within cooldown -> suppressed
            BatterySample(available=True, percent=8.0, power_plugged=False),   # CRITICAL escalation!
            BatterySample(available=True, percent=12.0, power_plugged=True),   # charger plugged in -> recovery!
        ]
    )

    trigger = LowBatteryTrigger(
        config=LowBatteryConfig(
            warning_percent=20.0,
            critical_percent=10.0,
            hysteresis_percent=5.0,
            ignore_when_plugged=True,
            cooldown_seconds=300.0,
            actions=actions,
        ),
        probe=lambda: next(samples),
        dispatcher=dispatcher,
        clock=clock.monotonic,
        utc_now=clock.utc_now,
    )

    # 1. No battery sensor -> empty
    assert trigger.check_once() == []

    # 2. Plugged in at 15% -> ignored
    assert trigger.check_once() == []

    # 3. Unplugged at 18% -> WARNING event + actions dispatched/validated
    ev_warn = trigger.check_once()
    assert len(ev_warn) == 1
    assert ev_warn[0].event_name == "battery.low"
    assert ev_warn[0].severity == TriggerSeverity.WARNING
    dispatched = {d["action_type"]: d for d in ev_warn[0].metadata["dispatched_actions"]}
    assert dispatched["notify"]["status"] == "executed"
    assert dispatched["emit_core_event"]["status"] == "forwarded_to_core"
    assert dispatched["get_battery"]["status"] == "skipped"  # CRITICAL-only
    assert dispatched["run_script"]["status"] == "blocked"
    assert dispatched["open_app"]["status"] == "blocked"
    assert len(forwarded_events) == 1
    assert len(executed_actions) == 0

    # 4. Still warning within cooldown -> suppressed
    clock.advance(30.0)
    assert trigger.check_once() == []

    # 5. Drops to 8% -> escalates to CRITICAL immediately, now executes read_only_info get_battery!
    clock.advance(10.0)
    ev_crit = trigger.check_once()
    assert len(ev_crit) == 1
    assert ev_crit[0].event_name == "battery.critical"
    assert ev_crit[0].severity == TriggerSeverity.CRITICAL
    dispatched_crit = {d["action_type"]: d for d in ev_crit[0].metadata["dispatched_actions"]}
    assert dispatched_crit["get_battery"]["status"] == "executed"
    assert dispatched_crit["run_script"]["status"] == "blocked"
    assert len(executed_actions) == 1
    assert len(forwarded_events) == 2

    # 6. Charger plugged in -> emits recovery event
    clock.advance(5.0)
    ev_rec = trigger.check_once()
    assert len(ev_rec) == 1
    assert ev_rec[0].event_name == "battery.recovered"
    assert ev_rec[0].metrics["recovery_reason"] == "power_plugged"
    assert ev_rec[0].state_transition == {"from": "critical", "to": "ok"}


# ---------------------------------------------------------------------------
# Unified SystemTriggerManager & CoreTriggerClient
# ---------------------------------------------------------------------------


def test_system_trigger_manager_unified_poll_and_core_client():
    clock = FakeClock()
    sent_payloads: list[dict[str, Any]] = []

    class FakeResponse:
        status_code = 201

        def json(self) -> dict[str, Any]:
            return {"success": True}

    def fake_http_post(url: str, json: dict[str, Any], headers: dict[str, str], timeout: float):
        sent_payloads.append({"url": url, "json": json, "headers": headers})
        return FakeResponse()

    core_client = CoreTriggerClient(
        server_url="http://127.0.0.1:8000",
        auth_token="test-jwt-token",
        http_post=fake_http_post,
    )

    net_states = iter([True, False])
    lock_states = iter([SessionLockState.UNLOCKED, SessionLockState.LOCKED])

    manager = SystemTriggerManager(
        config=SystemTriggersConfig(
            disk=DiskThresholdConfig(mount_paths=["/"], warning_percent=80.0, critical_percent=95.0),
            resources=ResourceThresholdConfig(
                cpu_warning_percent=80.0,
                cpu_critical_percent=95.0,
                consecutive_samples=1,
            ),
            battery=LowBatteryConfig(
                warning_percent=25.0,
                critical_percent=10.0,
                actions=[
                    BatteryTriggerAction(
                        action_type="power_saver_hint",
                        category="system_trigger",
                        params={"profile": "eco"},
                    )
                ],
            ),
        ),
        device_id="workstation_01",
        clock=clock.monotonic,
        utc_now=clock.utc_now,
        disk_probe=lambda m: _disk_sample(m, percent=88.0, free_gb=12.0),
        resource_probe=lambda: ResourceSample(cpu_percent=84.0, ram_percent=40.0),
        network_probe=lambda: next(net_states),
        session_lock_provider=lambda: next(lock_states),
        battery_probe=lambda: BatterySample(available=True, percent=22.0, power_plugged=False),
        core_event_sender=core_client.send_event,
    )

    # First poll: disk + cpu + battery fire; network & session establish baselines
    first_events = manager.poll_once()
    assert {e.trigger_type for e in first_events} == {
        TriggerType.DISK_USAGE,
        TriggerType.CPU_UTILIZATION,
        TriggerType.LOW_BATTERY,
    }

    # Second poll: network goes offline + session locks (disk/cpu/battery suppressed by cooldown)
    clock.advance(5.0)
    second_events = manager.poll_once()
    assert {e.event_name for e in second_events} == {"network.offline", "session.locked"}

    # All 5 events recorded in history and forwarded via CoreTriggerClient
    assert len(manager.get_history()) == 5
    assert len(sent_payloads) == 5
    assert sent_payloads[0]["url"] == "http://127.0.0.1:8000/api/v1/triggers/system/events"
    assert sent_payloads[0]["headers"]["Authorization"] == "Bearer test-jwt-token"

    # Automation event compatibility check
    auto_ev = second_events[0].to_automation_event()
    assert auto_ev["type"].startswith("system.")
    assert auto_ev["source"] == "workstation_01"

    status_snapshot = manager.get_status()
    assert status_snapshot["history_count"] == 5
    assert status_snapshot["triggers"]["network"]["state"] == "offline"
    assert status_snapshot["triggers"]["session_lock"]["state"] == "locked"


# ---------------------------------------------------------------------------
# FastAPI REST Endpoints (nova_core/api/routes_system_triggers.py)
# ---------------------------------------------------------------------------


def _build_test_app(db_path: Path | None = None, *, require_auth: bool = False):
    app = FastAPI()
    automation_received: list[dict[str, Any]] = []

    class FakeAutomationEngine:
        async def evaluate_and_fire(self, event: dict[str, Any]) -> None:
            automation_received.append(event)

    if db_path is not None:
        app.state.store = DatabaseStore(db_path=str(db_path))
    app.state.automation = FakeAutomationEngine()

    if require_auth:
        app.include_router(auth_router)
        app.include_router(system_triggers_router, dependencies=[Depends(get_current_user)])
    else:
        app.include_router(system_triggers_router)

    return app, automation_received


def test_rest_api_config_ingest_history_and_status_with_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("ADMIN_PASSWORD", "nova_password")
    app, automation_received = _build_test_app(db_path=tmp_path / "nova_triggers.db", require_auth=True)
    client = TestClient(app)

    # Unauthenticated request rejected
    assert client.get("/api/v1/triggers/system/config").status_code == 401

    # Authenticate via /auth/token
    token_resp = client.post("/auth/token", json={"username": "admin", "password": "nova_password"})
    assert token_resp.status_code == 200
    headers = {"Authorization": f"Bearer {token_resp.json()['access_token']}"}

    # 1. GET default config
    cfg_resp = client.get("/api/v1/triggers/system/config", headers=headers)
    assert cfg_resp.status_code == 200
    assert cfg_resp.json()["config"]["disk"]["warning_percent"] == 85.0

    # 2. PATCH partial threshold update
    patch_resp = client.patch(
        "/api/v1/triggers/system/config",
        headers=headers,
        params={"device_id": "laptop_01"},
        json={
            "disk": {
                "mount_paths": ["/", "/home"],
                "warning_percent": 80.0,
                "critical_percent": 92.0,
                "min_free_gb": 10.0,
            },
            "resources": {
                "cpu_warning_percent": 75.0,
                "cpu_critical_percent": 90.0,
                "consecutive_samples": 4,
            },
        },
    )
    assert patch_resp.status_code == 200
    patched_cfg = patch_resp.json()["config"]
    assert patched_cfg["disk"]["mount_paths"] == ["/", "/home"]
    assert patched_cfg["disk"]["min_free_gb"] == 10.0
    assert patched_cfg["resources"]["consecutive_samples"] == 4

    # 3. PUT unsafe shell_command battery action -> rejected with 400
    unsafe_cfg = patched_cfg.copy()
    unsafe_cfg["battery"]["actions"] = [
        {
            "action_type": "run_script",
            "category": "shell_command",
            "params": {"command": "rm -rf /"},
        }
    ]
    bad_put = client.put(
        "/api/v1/triggers/system/config",
        headers=headers,
        params={"device_id": "laptop_01"},
        json=unsafe_cfg,
    )
    assert bad_put.status_code == 400
    assert "not permitted" in bad_put.json()["detail"]

    # 4. POST ingest system trigger events
    ev1 = client.post(
        "/api/v1/triggers/system/events",
        headers=headers,
        json={
            "trigger_type": "disk_usage",
            "event_name": "disk.threshold_exceeded",
            "severity": "warning",
            "source_device_id": "laptop_01",
            "message": "Disk usage warning on '/home': 83.5% used.",
            "metrics": {"mount_path": "/home", "percent": 83.5, "free_gb": 8.2},
            "state_transition": {"from": "ok", "to": "warning"},
        },
    )
    assert ev1.status_code == 201
    assert ev1.json()["automation_forwarded"] is True
    assert len(automation_received) == 1
    assert automation_received[0]["type"] == "system.disk_usage"

    ev2 = client.post(
        "/api/v1/triggers/system/events",
        headers=headers,
        json={
            "trigger_type": "low_battery",
            "event_name": "battery.critical",
            "severity": "critical",
            "source_device_id": "laptop_01",
            "message": "Battery critical: 8% remaining.",
            "metrics": {"percent": 8.0, "power_plugged": False},
            "state_transition": {"from": "warning", "to": "critical"},
            "forward_to_automation": False,
        },
    )
    assert ev2.status_code == 201
    assert ev2.json()["automation_forwarded"] is False
    assert len(automation_received) == 1

    # 5. GET history with filters & aggregate stats
    hist_all = client.get("/api/v1/triggers/system/history", headers=headers)
    assert hist_all.status_code == 200
    data = hist_all.json()
    assert data["count"] == 2
    assert data["stats"]["total"] == 2
    assert data["stats"]["by_severity"] == {"warning": 1, "critical": 1}
    assert data["stats"]["by_trigger_type"] == {"disk_usage": 1, "low_battery": 1}

    hist_crit = client.get(
        "/api/v1/triggers/system/history",
        headers=headers,
        params={"severity": "critical", "trigger_type": "low_battery"},
    )
    assert hist_crit.status_code == 200
    assert hist_crit.json()["count"] == 1
    assert hist_crit.json()["items"][0]["event_name"] == "battery.critical"

    # 6. GET status
    st_resp = client.get(
        "/api/v1/triggers/system/status",
        headers=headers,
        params={"device_id": "laptop_01"},
    )
    assert st_resp.status_code == 200
    assert st_resp.json()["stats"]["total"] == 2
    assert "low_battery" in st_resp.json()["stats"]["latest_by_type"]

    # 7. DELETE history
    del_resp = client.delete(
        "/api/v1/triggers/system/history",
        headers=headers,
        params={"source_device_id": "laptop_01"},
    )
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] == 2

    hist_empty = client.get("/api/v1/triggers/system/history", headers=headers)
    assert hist_empty.json()["count"] == 0


def test_rest_api_in_memory_fallback_without_database_store():
    """Ensures routes_system_triggers works standalone even without app.state.store."""
    app, _ = _build_test_app(db_path=None, require_auth=False)
    client = TestClient(app)

    put_resp = client.put(
        "/api/v1/triggers/system/config",
        json=SystemTriggersConfig(
            battery=LowBatteryConfig(warning_percent=25.0, critical_percent=12.0)
        ).model_dump(mode="json"),
    )
    assert put_resp.status_code == 200
    assert put_resp.json()["config"]["battery"]["warning_percent"] == 25.0

    post_resp = client.post(
        "/api/v1/triggers/system/events",
        json={
            "trigger_type": "session_lock",
            "event_name": "session.locked",
            "severity": "info",
            "source_device_id": "desktop_mem",
            "message": "Session locked.",
            "metrics": {"locked": True},
            "state_transition": {"from": "unlocked", "to": "locked"},
        },
    )
    assert post_resp.status_code == 201

    hist = client.get("/api/v1/triggers/system/history", params={"source_device_id": "desktop_mem"})
    assert hist.status_code == 200
    assert hist.json()["count"] == 1
    assert hist.json()["items"][0]["event_name"] == "session.locked"
