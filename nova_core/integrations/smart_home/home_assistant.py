"""Home Assistant Event Adapters & Smart Home Integration (Phase 21: F21.1–F21.5).

Provides:
- F21.1: `HomeAssistantWSManager` implementing the Home Assistant WebSocket API
  handshake (`auth_required` -> `auth` -> `auth_ok`/`auth_invalid`), monotonically
  incrementing command IDs, `subscribe_events`, `ping`/`pong`, reconnect backoff
  state tracking, and an injectable WebSocket client factory for offline tests.
- F21.2: `EntityStateTracker` for parsing HA `state_changed` events, tracking
  `old_state -> new_state` transitions and attribute diffs, and evaluating
  registered entity triggers (`from_state`, `to_state`, attribute threshold/match,
  and cooldown protection).
- F21.3: `HomeAssistantEventDispatcher` translating incoming HA events into
  normalized `NovaAutomationEvent` payloads, forwarding to registered callbacks /
  automation engines, and dispatching outbound `call_service` commands over WS or
  REST with SQLite audit logging.
- F21.4: `HACapabilityMapper` mapping Home Assistant entity domains (`light`,
  `switch`, `climate`, `sensor`, `binary_sensor`, `cover`, `lock`, `media_player`,
  `fan`, plus generic fallback) into structured Nova device capability profiles
  and persisting synced entities/capabilities to SQLite.
- F21.5: `HomeAssistantStorage` (via `DatabaseStore.get_connection()` or direct
  `sqlite3`) and `HomeAssistantService` facade for REST API integration.
"""

from __future__ import annotations

import asyncio
import fnmatch
import inspect
import json
import os
import sqlite3
import time
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterator, Literal, Optional
from urllib.parse import urlparse, urlunparse

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

_MIGRATION_0012_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "db"
    / "migrations"
    / "0012_home_assistant.sql"
)

AttributeOperator = Literal[
    "eq", "ne", "gt", "gte", "lt", "lte", "contains", "changed"
]
DispatchDirection = Literal["inbound", "outbound"]
DispatchTransport = Literal["auto", "ws", "rest", "ingest", "internal"]

WSClientFactory = Callable[[str], Any]
HttpRequester = Callable[
    [str, str, dict[str, str], Optional[dict[str, Any]], float], Any
]
EventCallback = Callable[["NovaAutomationEvent"], Any]


# ---------------------------------------------------------------------------
# Errors & Helpers
# ---------------------------------------------------------------------------


class HomeAssistantError(Exception):
    """Base error for Home Assistant integration failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class HomeAssistantAuthError(HomeAssistantError):
    """Raised when Home Assistant rejects or lacks a long-lived access token."""

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message, status_code=status_code, retryable=False)


class HomeAssistantConnectionError(HomeAssistantError):
    """Raised when the Home Assistant WebSocket or HTTP transport cannot connect."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message, status_code=status_code, retryable=True)


class HomeAssistantServiceError(HomeAssistantError):
    """Raised when an outbound `call_service` command fails."""

    def __init__(self, message: str, *, status_code: int = 502) -> None:
        super().__init__(message, status_code=status_code, retryable=False)


def mask_access_token(token: Optional[str]) -> str:
    """Masks a Home Assistant long-lived access token for safe API responses."""
    if not token:
        return ""
    clean = token.strip()
    if not clean:
        return ""
    if len(clean) <= 6:
        return "*" * len(clean)
    if len(clean) <= 10:
        return f"{'*' * (len(clean) - 4)}{clean[-4:]}"
    return f"{clean[:4]}{'*' * (len(clean) - 8)}{clean[-4:]}"


def derive_ws_url(base_url: str, ws_url: Optional[str] = None) -> str:
    """Derives the Home Assistant WebSocket endpoint from `base_url` if `ws_url` is unset."""
    if ws_url and ws_url.strip():
        return ws_url.strip()

    parsed = urlparse(base_url.strip())
    scheme = "wss" if parsed.scheme == "https" else "ws"
    if parsed.scheme in ("ws", "wss"):
        scheme = parsed.scheme
    path = parsed.path.rstrip("/")
    if not path.endswith("/api/websocket"):
        path = f"{path}/api/websocket"
    return urlunparse((scheme, parsed.netloc, path, "", "", ""))


def extract_domain(entity_id: str) -> str:
    """Extracts the Home Assistant domain prefix from an entity_id (e.g. 'light.desk' -> 'light')."""
    if "." in entity_id:
        return entity_id.split(".", 1)[0].strip().lower()
    return "unknown"


def _parse_iso_datetime(value: Optional[str | datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _to_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _default_http_requester(
    method: str,
    url: str,
    headers: dict[str, str],
    json_body: Optional[dict[str, Any]],
    timeout: float,
) -> Any:
    """Default synchronous HTTP requester backed by httpx."""
    try:
        resp = httpx.request(
            method=method.upper(),
            url=url,
            headers=headers,
            json=json_body,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise HomeAssistantConnectionError(
            f"Home Assistant HTTP request timed out: {exc}", status_code=504
        ) from exc
    except httpx.HTTPError as exc:
        raise HomeAssistantConnectionError(
            f"Home Assistant HTTP transport error: {exc}", status_code=502
        ) from exc

    if resp.status_code in (401, 403):
        raise HomeAssistantAuthError(
            f"Home Assistant rejected credentials (HTTP {resp.status_code}).",
            status_code=resp.status_code,
        )
    if resp.status_code >= 400:
        raise HomeAssistantServiceError(
            f"Home Assistant HTTP {resp.status_code}: {resp.text[:300]}",
            status_code=resp.status_code,
        )
    if not resp.text.strip():
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


# ---------------------------------------------------------------------------
# Domain Models
# ---------------------------------------------------------------------------


class HAWSConnectionState(str, Enum):
    DISCONNECTED = "disconnected"
    CONNECTING = "connecting"
    AUTHENTICATING = "authenticating"
    CONNECTED = "connected"
    RECONNECTING = "reconnecting"
    AUTH_FAILED = "auth_failed"
    CLOSED = "closed"


class HAConfig(BaseModel):
    """Persisted Home Assistant connection configuration."""

    id: str = Field(default="default")
    base_url: str = Field(default="http://homeassistant.local:8123", min_length=1)
    ws_url: Optional[str] = Field(default=None)
    access_token: str = Field(default="")
    verify_ssl: bool = Field(default=True)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    reconnect_base_delay: float = Field(default=1.0, gt=0.0, le=300.0)
    reconnect_max_delay: float = Field(default=60.0, gt=0.0, le=3600.0)
    auto_subscribe_events: list[str] = Field(default_factory=lambda: ["state_changed"])
    enabled: bool = Field(default=True)
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    @field_validator("base_url")
    @classmethod
    def _validate_base_url(cls, value: str) -> str:
        clean = value.strip().rstrip("/")
        parsed = urlparse(clean)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError(
                "base_url must be a valid http:// or https:// URL with host."
            )
        return clean

    @model_validator(mode="after")
    def _validate_delays(self) -> "HAConfig":
        if self.reconnect_max_delay < self.reconnect_base_delay:
            raise ValueError(
                "reconnect_max_delay must be greater than or equal to reconnect_base_delay."
            )
        return self

    def resolved_ws_url(self) -> str:
        return derive_ws_url(self.base_url, self.ws_url)

    def to_public_dict(self) -> dict[str, Any]:
        """Returns a safe dict with the long-lived access token masked and omitted."""
        return {
            "id": self.id,
            "base_url": self.base_url,
            "ws_url": self.ws_url,
            "resolved_ws_url": self.resolved_ws_url(),
            "has_access_token": bool(self.access_token and self.access_token.strip()),
            "access_token_masked": mask_access_token(self.access_token),
            "verify_ssl": self.verify_ssl,
            "timeout_seconds": self.timeout_seconds,
            "reconnect_base_delay": self.reconnect_base_delay,
            "reconnect_max_delay": self.reconnect_max_delay,
            "auto_subscribe_events": list(self.auto_subscribe_events),
            "enabled": self.enabled,
            "updated_at": self.updated_at,
        }


class HAWSStatus(BaseModel):
    """Snapshot of the Home Assistant WebSocket connection manager state."""

    state: HAWSConnectionState = HAWSConnectionState.DISCONNECTED
    connected: bool = False
    ws_url: str = ""
    ha_version: Optional[str] = None
    last_command_id: int = 0
    subscriptions: dict[int, Optional[str]] = Field(default_factory=dict)
    reconnect_attempts: int = 0
    next_backoff_seconds: float = 0.0
    last_ping_at: Optional[str] = None
    last_pong_at: Optional[str] = None
    last_error: Optional[str] = None


class HAEntityState(BaseModel):
    """Normalized snapshot of a Home Assistant entity state."""

    entity_id: str
    domain: str
    state: str = "unknown"
    attributes: dict[str, Any] = Field(default_factory=dict)
    friendly_name: str = ""
    device_class: Optional[str] = None
    area_id: Optional[str] = None
    last_changed: Optional[str] = None
    last_updated: Optional[str] = None


class HAStateTransition(BaseModel):
    """Parsed `state_changed` transition with old/new states and attribute diffs."""

    entity_id: str
    domain: str
    old_state: Optional[str] = None
    new_state: Optional[str] = None
    old_attributes: dict[str, Any] = Field(default_factory=dict)
    new_attributes: dict[str, Any] = Field(default_factory=dict)
    attribute_diffs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    state_changed: bool = False
    changed_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    context: dict[str, Any] = Field(default_factory=dict)


class HAEntityTrigger(BaseModel):
    """Registered trigger rule evaluated against Home Assistant entity state transitions."""

    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=128)
    enabled: bool = True
    entity_id: str = Field(..., min_length=1, max_length=256)
    from_state: Optional[str] = None
    to_state: Optional[str] = None
    attribute_name: Optional[str] = None
    attribute_operator: Optional[AttributeOperator] = None
    attribute_value: Any = None
    cooldown_seconds: int = Field(default=0, ge=0, le=86400 * 7)
    event_type: str = Field(default="ha.state_trigger", min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)
    last_triggered_at: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @model_validator(mode="after")
    def _validate_trigger_criteria(self) -> "HAEntityTrigger":
        if (
            self.from_state is None
            and self.to_state is None
            and self.attribute_name is None
        ):
            raise ValueError(
                "At least one of 'from_state', 'to_state', or 'attribute_name' must be specified."
            )
        if self.attribute_name is not None:
            if self.attribute_operator is None:
                self.attribute_operator = "changed"
            if (
                self.attribute_operator != "changed"
                and self.attribute_value is None
            ):
                raise ValueError(
                    f"attribute_value is required when attribute_operator is '{self.attribute_operator}'."
                )
        return self


class HATriggerMatch(BaseModel):
    """Evaluation match (or cooldown-suppressed match) for an entity trigger."""

    trigger_id: str
    trigger_name: str
    entity_id: str
    event_type: str
    old_state: Optional[str] = None
    new_state: Optional[str] = None
    attribute_diffs: dict[str, dict[str, Any]] = Field(default_factory=dict)
    matched_reason: str
    triggered_at: str
    suppressed: bool = False


class NovaAutomationEvent(BaseModel):
    """Normalized Nova automation event translated from Home Assistant."""

    event_id: str
    source: str = "home_assistant"
    event_type: str
    ha_event_type: str
    entity_id: Optional[str] = None
    domain: Optional[str] = None
    timestamp: str
    payload: dict[str, Any] = Field(default_factory=dict)
    matched_triggers: list[HATriggerMatch] = Field(default_factory=list)


class HADeviceCapabilityProfile(BaseModel):
    """Structured Nova device capability profile mapped from a Home Assistant domain."""

    domain: str
    category: str
    supports_state: bool = True
    state_type: str = "string"
    readable_properties: list[str] = Field(default_factory=list)
    writable_properties: list[str] = Field(default_factory=list)
    supported_actions: list[str] = Field(default_factory=list)
    ha_services: list[str] = Field(default_factory=list)
    detected_features: list[str] = Field(default_factory=list)


class HASyncedEntity(BaseModel):
    """Home Assistant entity enriched with Nova device capability profile."""

    entity_id: str
    domain: str
    friendly_name: str = ""
    state: str = "unknown"
    attributes: dict[str, Any] = Field(default_factory=dict)
    capabilities: HADeviceCapabilityProfile
    area_id: Optional[str] = None
    device_class: Optional[str] = None
    last_changed: Optional[str] = None
    last_updated: Optional[str] = None
    synced_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class HAServiceCallResult(BaseModel):
    """Result of an outbound Home Assistant `call_service` dispatch."""

    audit_id: str
    domain: str
    service: str
    entity_id: Optional[str] = None
    target: dict[str, Any] = Field(default_factory=dict)
    service_data: dict[str, Any] = Field(default_factory=dict)
    transport: str
    success: bool
    response: Any = None
    error: Optional[str] = None
    executed_at: str


class HADispatchLogEntry(BaseModel):
    """Persisted audit log entry for inbound events and outbound service calls."""

    id: str
    direction: DispatchDirection
    transport: str
    event_type: str
    entity_id: Optional[str] = None
    domain: Optional[str] = None
    service: Optional[str] = None
    payload: dict[str, Any] = Field(default_factory=dict)
    result: dict[str, Any] = Field(default_factory=dict)
    status: str = "ok"
    error_message: Optional[str] = None
    created_at: str


class HAEventDispatchResult(BaseModel):
    """Summary returned when an inbound Home Assistant event is processed."""

    audit_id: str
    event: NovaAutomationEvent
    transition: Optional[HAStateTransition] = None
    triggered: list[HATriggerMatch] = Field(default_factory=list)
    suppressed: list[HATriggerMatch] = Field(default_factory=list)
    listeners_notified: int = 0


class HASyncResult(BaseModel):
    """Summary returned after syncing Home Assistant entities and capabilities."""

    synced_count: int
    domains: dict[str, int] = Field(default_factory=dict)
    entities: list[HASyncedEntity] = Field(default_factory=list)
    synced_at: str


# ---------------------------------------------------------------------------
# F21.1: Home Assistant WebSocket Connection Manager
# ---------------------------------------------------------------------------


class HomeAssistantWSManager:
    """Manages Home Assistant WebSocket API lifecycle, auth, commands, ping/pong, and backoff."""

    def __init__(
        self,
        *,
        ws_url: str = "ws://homeassistant.local:8123/api/websocket",
        access_token: str = "",
        reconnect_base_delay: float = 1.0,
        reconnect_max_delay: float = 60.0,
        ws_factory: Optional[WSClientFactory] = None,
    ) -> None:
        self.ws_url = ws_url
        self.access_token = access_token
        self.reconnect_base_delay = float(reconnect_base_delay)
        self.reconnect_max_delay = float(reconnect_max_delay)
        self._ws_factory = ws_factory

        self._ws: Any = None
        self._state: HAWSConnectionState = HAWSConnectionState.DISCONNECTED
        self._ha_version: Optional[str] = None
        self._command_id: int = 0
        self._subscriptions: dict[int, Optional[str]] = {}
        self._event_queue: deque[dict[str, Any]] = deque()

        self._reconnect_attempts: int = 0
        self._next_backoff_seconds: float = 0.0
        self._last_ping_at: Optional[str] = None
        self._last_pong_at: Optional[str] = None
        self._last_error: Optional[str] = None

    @property
    def state(self) -> HAWSConnectionState:
        return self._state

    @property
    def is_connected(self) -> bool:
        return self._state == HAWSConnectionState.CONNECTED and self._ws is not None

    @property
    def ha_version(self) -> Optional[str]:
        return self._ha_version

    @property
    def subscriptions(self) -> dict[int, Optional[str]]:
        return dict(self._subscriptions)

    def configure(
        self,
        *,
        ws_url: Optional[str] = None,
        access_token: Optional[str] = None,
        reconnect_base_delay: Optional[float] = None,
        reconnect_max_delay: Optional[float] = None,
    ) -> None:
        if ws_url is not None:
            self.ws_url = ws_url
        if access_token is not None:
            self.access_token = access_token
        if reconnect_base_delay is not None:
            self.reconnect_base_delay = float(reconnect_base_delay)
        if reconnect_max_delay is not None:
            self.reconnect_max_delay = float(reconnect_max_delay)

    def _next_id(self) -> int:
        self._command_id += 1
        return self._command_id

    def compute_backoff_delay(self, attempt: Optional[int] = None) -> float:
        """Computes capped exponential reconnect backoff delay in seconds."""
        idx = self._reconnect_attempts if attempt is None else max(0, attempt)
        delay = self.reconnect_base_delay * (2 ** idx)
        return float(min(delay, self.reconnect_max_delay))

    def record_disconnect(self, error: Optional[str] = None) -> float:
        """Records a disconnect/failure and updates reconnect backoff tracking state."""
        delay = self.compute_backoff_delay(self._reconnect_attempts)
        self._reconnect_attempts += 1
        self._next_backoff_seconds = delay
        self._state = HAWSConnectionState.RECONNECTING
        self._last_error = error
        self._ws = None
        return delay

    def reset_backoff(self) -> None:
        """Resets reconnect backoff tracking after a healthy connection."""
        self._reconnect_attempts = 0
        self._next_backoff_seconds = 0.0
        self._last_error = None

    async def _open_socket(self) -> Any:
        if self._ws_factory is not None:
            ws = self._ws_factory(self.ws_url)
            if inspect.isawaitable(ws):
                ws = await ws
            return ws

        import websockets  # Lazy import to avoid unnecessary overhead if mocked

        return await websockets.connect(self.ws_url)

    async def _send_json(self, payload: dict[str, Any]) -> None:
        if self._ws is None:
            raise HomeAssistantConnectionError("WebSocket is not connected.")
        await self._ws.send(json.dumps(payload))

    async def _recv_json(self, timeout: Optional[float] = None) -> dict[str, Any]:
        if self._ws is None:
            raise HomeAssistantConnectionError("WebSocket is not connected.")
        coro = self._ws.recv()
        raw = await asyncio.wait_for(coro, timeout=timeout) if timeout else await coro
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)

    async def connect(self, timeout: float = 10.0) -> HAWSStatus:
        """Opens WebSocket and completes HA `auth_required` -> `auth` -> `auth_ok`/`auth_invalid` handshake."""
        if not self.access_token or not self.access_token.strip():
            self._state = HAWSConnectionState.AUTH_FAILED
            self._last_error = "Home Assistant access token is required."
            raise HomeAssistantAuthError("Home Assistant access token is required.")

        self._state = HAWSConnectionState.CONNECTING
        self._last_error = None

        try:
            self._ws = await self._open_socket()
        except Exception as exc:
            self.record_disconnect(str(exc))
            raise HomeAssistantConnectionError(
                f"Failed to connect to Home Assistant WebSocket at {self.ws_url}: {exc}"
            ) from exc

        try:
            self._state = HAWSConnectionState.AUTHENTICATING
            greeting = await self._recv_json(timeout=timeout)
            if greeting.get("type") != "auth_required":
                raise HomeAssistantConnectionError(
                    f"Expected 'auth_required' from Home Assistant WS, got '{greeting.get('type')}'."
                )

            await self._send_json(
                {"type": "auth", "access_token": self.access_token}
            )
            auth_reply = await self._recv_json(timeout=timeout)
            reply_type = auth_reply.get("type")
            if reply_type == "auth_ok":
                self._ha_version = auth_reply.get("ha_version")
                self._state = HAWSConnectionState.CONNECTED
                self.reset_backoff()
                return self.get_status()

            if reply_type == "auth_invalid":
                message = auth_reply.get("message") or "Invalid access token."
                self._state = HAWSConnectionState.AUTH_FAILED
                self._last_error = message
                await self._close_ws_quietly()
                raise HomeAssistantAuthError(
                    f"Home Assistant WS authentication failed: {message}"
                )

            raise HomeAssistantConnectionError(
                f"Unexpected authentication response type: '{reply_type}'."
            )
        except HomeAssistantAuthError:
            raise
        except Exception as exc:
            await self._close_ws_quietly()
            self.record_disconnect(str(exc))
            if isinstance(exc, HomeAssistantConnectionError):
                raise
            raise HomeAssistantConnectionError(
                f"Home Assistant WS handshake error: {exc}"
            ) from exc

    async def _close_ws_quietly(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is not None and hasattr(ws, "close"):
            try:
                res = ws.close()
                if inspect.isawaitable(res):
                    await res
            except Exception:
                pass

    async def disconnect(self) -> None:
        """Gracefully closes the WebSocket connection."""
        await self._close_ws_quietly()
        self._subscriptions.clear()
        self._state = HAWSConnectionState.DISCONNECTED

    async def send_command(
        self,
        command_type: str,
        *,
        timeout: float = 10.0,
        **fields: Any,
    ) -> dict[str, Any]:
        """Sends an ID-tagged HA WS command and awaits the matching response frame."""
        if not self.is_connected:
            raise HomeAssistantConnectionError(
                "Cannot send command: Home Assistant WebSocket is not connected."
            )

        cmd_id = self._next_id()
        payload = {"id": cmd_id, "type": command_type, **fields}
        await self._send_json(payload)

        while True:
            frame = await self._recv_json(timeout=timeout)
            frame_type = frame.get("type")
            if frame_type == "event":
                # Buffer interleaved events so callers do not drop them
                self._event_queue.append(frame)
                continue
            if frame.get("id") == cmd_id:
                return frame
            # Buffer unexpected non-matching frames if they are events
            if frame_type:
                self._event_queue.append(frame)

    async def subscribe_events(
        self,
        event_type: Optional[str] = "state_changed",
        *,
        timeout: float = 10.0,
    ) -> int:
        """Subscribes to Home Assistant events and returns the subscription command ID."""
        extra: dict[str, Any] = {}
        if event_type:
            extra["event_type"] = event_type
        reply = await self.send_command("subscribe_events", timeout=timeout, **extra)
        if not reply.get("success", False):
            err = reply.get("error", {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise HomeAssistantServiceError(
                f"Failed to subscribe to '{event_type or '*'}': {msg}"
            )
        sub_id = int(reply["id"])
        self._subscriptions[sub_id] = event_type
        return sub_id

    async def unsubscribe_events(
        self, subscription_id: int, *, timeout: float = 10.0
    ) -> bool:
        """Unsubscribes from a previously registered event subscription."""
        reply = await self.send_command(
            "unsubscribe_events", subscription=subscription_id, timeout=timeout
        )
        ok = bool(reply.get("success", False))
        if ok:
            self._subscriptions.pop(subscription_id, None)
        return ok

    async def ping(self, *, timeout: float = 10.0) -> dict[str, Any]:
        """Sends a HA WebSocket `ping` and validates the matching `pong` response."""
        started = time.perf_counter()
        self._last_ping_at = datetime.now(timezone.utc).isoformat()
        reply = await self.send_command("ping", timeout=timeout)
        if reply.get("type") != "pong":
            raise HomeAssistantConnectionError(
                f"Expected 'pong' response, got '{reply.get('type')}'."
            )
        latency_ms = round((time.perf_counter() - started) * 1000.0, 3)
        self._last_pong_at = datetime.now(timezone.utc).isoformat()
        return {
            "id": reply.get("id"),
            "type": "pong",
            "latency_ms": latency_ms,
            "ping_at": self._last_ping_at,
            "pong_at": self._last_pong_at,
        }

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        service_data: Optional[dict[str, Any]] = None,
        target: Optional[dict[str, Any]] = None,
        timeout: float = 10.0,
    ) -> dict[str, Any]:
        """Dispatches an outbound `call_service` command over the active WebSocket."""
        fields: dict[str, Any] = {"domain": domain, "service": service}
        if service_data:
            fields["service_data"] = service_data
        if target:
            fields["target"] = target
        reply = await self.send_command("call_service", timeout=timeout, **fields)
        if not reply.get("success", False):
            err = reply.get("error", {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise HomeAssistantServiceError(
                f"HA WS call_service {domain}.{service} failed: {msg}"
            )
        return reply

    async def get_states(self, *, timeout: float = 10.0) -> list[dict[str, Any]]:
        """Fetches all current entity states over the active WebSocket."""
        reply = await self.send_command("get_states", timeout=timeout)
        if not reply.get("success", False):
            err = reply.get("error", {})
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise HomeAssistantServiceError(f"HA WS get_states failed: {msg}")
        result = reply.get("result")
        return list(result) if isinstance(result, list) else []

    async def receive_event(
        self, *, timeout: Optional[float] = None
    ) -> Optional[dict[str, Any]]:
        """Returns the next buffered or incoming event frame from Home Assistant."""
        if self._event_queue:
            return self._event_queue.popleft()
        if not self.is_connected:
            return None
        return await self._recv_json(timeout=timeout)

    def get_status(self) -> HAWSStatus:
        return HAWSStatus(
            state=self._state,
            connected=self.is_connected,
            ws_url=self.ws_url,
            ha_version=self._ha_version,
            last_command_id=self._command_id,
            subscriptions=dict(self._subscriptions),
            reconnect_attempts=self._reconnect_attempts,
            next_backoff_seconds=self._next_backoff_seconds,
            last_ping_at=self._last_ping_at,
            last_pong_at=self._last_pong_at,
            last_error=self._last_error,
        )


# ---------------------------------------------------------------------------
# F21.2: Entity State Modification Trigger Tracking
# ---------------------------------------------------------------------------


class EntityStateTracker:
    """Parses HA `state_changed` events, computes attribute diffs, and evaluates triggers."""

    def __init__(self) -> None:
        self._last_states: dict[str, HAEntityState] = {}

    def seed_entity(self, entity: HAEntityState | dict[str, Any]) -> HAEntityState:
        """Seeds or updates the last-known entity state in memory."""
        if isinstance(entity, dict):
            entity_id = str(entity.get("entity_id") or "")
            attrs = dict(entity.get("attributes") or {})
            model = HAEntityState(
                entity_id=entity_id,
                domain=extract_domain(entity_id),
                state=str(entity.get("state", "unknown")),
                attributes=attrs,
                friendly_name=str(
                    entity.get("friendly_name")
                    or attrs.get("friendly_name")
                    or entity_id
                ),
                device_class=attrs.get("device_class") or entity.get("device_class"),
                area_id=entity.get("area_id"),
                last_changed=entity.get("last_changed"),
                last_updated=entity.get("last_updated"),
            )
        else:
            model = entity
        if model.entity_id:
            self._last_states[model.entity_id] = model
        return model

    def get_last_state(self, entity_id: str) -> Optional[HAEntityState]:
        return self._last_states.get(entity_id)

    @staticmethod
    def compute_attribute_diffs(
        old_attrs: dict[str, Any], new_attrs: dict[str, Any]
    ) -> dict[str, dict[str, Any]]:
        """Computes per-key attribute diffs between old and new attribute dicts."""
        diffs: dict[str, dict[str, Any]] = {}
        all_keys = set(old_attrs.keys()) | set(new_attrs.keys())
        for key in sorted(all_keys):
            old_val = old_attrs.get(key)
            new_val = new_attrs.get(key)
            if old_val != new_val:
                diffs[key] = {"old": old_val, "new": new_val}
        return diffs

    def parse_state_changed(
        self, raw_event: dict[str, Any]
    ) -> Optional[HAStateTransition]:
        """Parses a Home Assistant `state_changed` event into an `HAStateTransition`.

        Accepts:
        - Full WS envelope: `{"type": "event", "event": {"event_type": "state_changed", "data": {...}}}`
        - Event object: `{"event_type": "state_changed", "data": {...}}`
        - Raw data dict: `{"entity_id": "...", "old_state": {...}, "new_state": {...}}`
        """
        event_obj = raw_event
        if raw_event.get("type") == "event" and isinstance(raw_event.get("event"), dict):
            event_obj = raw_event["event"]

        event_type = event_obj.get("event_type")
        if event_type is not None and event_type != "state_changed":
            return None

        data = event_obj.get("data") if isinstance(event_obj.get("data"), dict) else event_obj
        entity_id = data.get("entity_id")
        if not entity_id:
            # Attempt fallback from new_state or old_state
            new_obj = data.get("new_state")
            old_obj = data.get("old_state")
            if isinstance(new_obj, dict):
                entity_id = new_obj.get("entity_id")
            if not entity_id and isinstance(old_obj, dict):
                entity_id = old_obj.get("entity_id")
        if not entity_id:
            return None

        entity_id = str(entity_id)
        old_obj = data.get("old_state")
        new_obj = data.get("new_state")

        if old_obj is None and entity_id in self._last_states:
            prev = self._last_states[entity_id]
            old_state_val: Optional[str] = prev.state
            old_attrs: dict[str, Any] = dict(prev.attributes)
        elif isinstance(old_obj, dict):
            old_state_val = (
                str(old_obj["state"]) if old_obj.get("state") is not None else None
            )
            old_attrs = dict(old_obj.get("attributes") or {})
        elif isinstance(old_obj, str):
            old_state_val = old_obj
            old_attrs = {}
        else:
            old_state_val = None
            old_attrs = {}

        if isinstance(new_obj, dict):
            new_state_val: Optional[str] = (
                str(new_obj["state"]) if new_obj.get("state") is not None else None
            )
            new_attrs: dict[str, Any] = dict(new_obj.get("attributes") or {})
            changed_at = (
                new_obj.get("last_updated")
                or new_obj.get("last_changed")
                or event_obj.get("time_fired")
                or datetime.now(timezone.utc).isoformat()
            )
            self.seed_entity(
                {
                    "entity_id": entity_id,
                    "state": new_state_val or "unknown",
                    "attributes": new_attrs,
                    "last_changed": new_obj.get("last_changed"),
                    "last_updated": new_obj.get("last_updated"),
                }
            )
        elif isinstance(new_obj, str):
            new_state_val = new_obj
            new_attrs = {}
            changed_at = (
                event_obj.get("time_fired")
                or datetime.now(timezone.utc).isoformat()
            )
            self.seed_entity({"entity_id": entity_id, "state": new_state_val})
        else:
            new_state_val = None
            new_attrs = {}
            changed_at = (
                event_obj.get("time_fired")
                or datetime.now(timezone.utc).isoformat()
            )

        diffs = self.compute_attribute_diffs(old_attrs, new_attrs)
        return HAStateTransition(
            entity_id=entity_id,
            domain=extract_domain(entity_id),
            old_state=old_state_val,
            new_state=new_state_val,
            old_attributes=old_attrs,
            new_attributes=new_attrs,
            attribute_diffs=diffs,
            state_changed=(old_state_val != new_state_val),
            changed_at=str(changed_at),
            context=dict(event_obj.get("context") or {}),
        )

    @staticmethod
    def _entity_matches(pattern: str, entity_id: str) -> bool:
        return fnmatch.fnmatchcase(entity_id.lower(), pattern.strip().lower())

    @staticmethod
    def _attribute_matches(
        trigger: HAEntityTrigger, transition: HAStateTransition
    ) -> tuple[bool, str]:
        attr_name = trigger.attribute_name
        if not attr_name:
            return True, ""

        op = trigger.attribute_operator or "changed"
        target = trigger.attribute_value

        if op == "changed":
            if attr_name in transition.attribute_diffs:
                diff = transition.attribute_diffs[attr_name]
                return True, f"attribute '{attr_name}' changed ({diff['old']} -> {diff['new']})"
            return False, ""

        if attr_name not in transition.new_attributes:
            return False, ""

        actual = transition.new_attributes[attr_name]
        if op == "eq":
            if actual == target or str(actual).lower() == str(target).lower():
                return True, f"attribute '{attr_name}' == {target!r}"
            return False, ""
        if op == "ne":
            if actual != target:
                return True, f"attribute '{attr_name}' != {target!r}"
            return False, ""
        if op == "contains":
            if isinstance(actual, (list, tuple, set)):
                matched = target in actual
            else:
                matched = str(target).lower() in str(actual).lower()
            return (
                (True, f"attribute '{attr_name}' contains {target!r}")
                if matched
                else (False, "")
            )

        # Numeric comparisons: gt, gte, lt, lte
        actual_num = _to_float(actual)
        target_num = _to_float(target)
        if actual_num is None or target_num is None:
            return False, ""

        checks = {
            "gt": actual_num > target_num,
            "gte": actual_num >= target_num,
            "lt": actual_num < target_num,
            "lte": actual_num <= target_num,
        }
        if checks.get(op, False):
            return True, f"attribute '{attr_name}' ({actual_num}) {op} {target_num}"
        return False, ""

    def evaluate_triggers(
        self,
        transition: HAStateTransition,
        triggers: list[HAEntityTrigger],
        *,
        evaluated_at: Optional[datetime] = None,
    ) -> tuple[list[HATriggerMatch], list[HATriggerMatch]]:
        """Evaluates registered triggers against a transition.

        Returns `(triggered_matches, suppressed_matches)`.
        """
        now_dt = _parse_iso_datetime(evaluated_at) or datetime.now(timezone.utc)
        now_iso = now_dt.isoformat()

        triggered: list[HATriggerMatch] = []
        suppressed: list[HATriggerMatch] = []

        for trig in triggers:
            if not trig.enabled:
                continue
            if not self._entity_matches(trig.entity_id, transition.entity_id):
                continue

            reasons: list[str] = []

            if trig.from_state is not None:
                if (
                    transition.old_state is None
                    or transition.old_state.lower() != trig.from_state.strip().lower()
                ):
                    continue
                reasons.append(f"from_state='{transition.old_state}'")

            if trig.to_state is not None:
                if (
                    transition.new_state is None
                    or transition.new_state.lower() != trig.to_state.strip().lower()
                ):
                    continue
                reasons.append(f"to_state='{transition.new_state}'")

            if trig.attribute_name is not None:
                attr_ok, attr_reason = self._attribute_matches(trig, transition)
                if not attr_ok:
                    continue
                reasons.append(attr_reason)

            # Cooldown check
            in_cooldown = False
            if trig.cooldown_seconds > 0 and trig.last_triggered_at:
                last_dt = _parse_iso_datetime(trig.last_triggered_at)
                if last_dt is not None:
                    elapsed = (now_dt - last_dt).total_seconds()
                    if elapsed < trig.cooldown_seconds:
                        in_cooldown = True

            match = HATriggerMatch(
                trigger_id=trig.id or "",
                trigger_name=trig.name,
                entity_id=transition.entity_id,
                event_type=trig.event_type,
                old_state=transition.old_state,
                new_state=transition.new_state,
                attribute_diffs=transition.attribute_diffs,
                matched_reason="; ".join(reasons) or "matched",
                triggered_at=now_iso,
                suppressed=in_cooldown,
            )
            if in_cooldown:
                suppressed.append(match)
            else:
                triggered.append(match)

        return triggered, suppressed


# ---------------------------------------------------------------------------
# F21.4: Device Capabilities Sync Maps
# ---------------------------------------------------------------------------


_DOMAIN_CAPABILITY_TEMPLATES: dict[str, dict[str, Any]] = {
    "light": {
        "category": "lighting",
        "supports_state": True,
        "state_type": "binary",
        "readable_properties": [
            "state",
            "brightness",
            "color_temp",
            "rgb_color",
            "hs_color",
            "color_mode",
            "supported_color_modes",
        ],
        "writable_properties": [
            "brightness",
            "color_temp",
            "rgb_color",
            "hs_color",
            "effect",
        ],
        "supported_actions": [
            "turn_on",
            "turn_off",
            "toggle",
            "set_brightness",
            "set_color_temp",
            "set_rgb_color",
        ],
        "ha_services": ["light.turn_on", "light.turn_off", "light.toggle"],
    },
    "switch": {
        "category": "power",
        "supports_state": True,
        "state_type": "binary",
        "readable_properties": ["state", "current_power_w", "today_energy_kwh"],
        "writable_properties": [],
        "supported_actions": ["turn_on", "turn_off", "toggle"],
        "ha_services": ["switch.turn_on", "switch.turn_off", "switch.toggle"],
    },
    "climate": {
        "category": "hvac",
        "supports_state": True,
        "state_type": "enum",
        "readable_properties": [
            "state",
            "current_temperature",
            "temperature",
            "current_humidity",
            "hvac_action",
            "hvac_modes",
            "fan_mode",
            "preset_mode",
        ],
        "writable_properties": [
            "temperature",
            "hvac_mode",
            "fan_mode",
            "preset_mode",
            "swing_mode",
        ],
        "supported_actions": [
            "set_temperature",
            "set_hvac_mode",
            "set_fan_mode",
            "turn_on",
            "turn_off",
        ],
        "ha_services": [
            "climate.set_temperature",
            "climate.set_hvac_mode",
            "climate.set_fan_mode",
            "climate.turn_on",
            "climate.turn_off",
        ],
    },
    "sensor": {
        "category": "telemetry",
        "supports_state": True,
        "state_type": "numeric_or_string",
        "readable_properties": [
            "state",
            "unit_of_measurement",
            "device_class",
            "state_class",
        ],
        "writable_properties": [],
        "supported_actions": [],
        "ha_services": [],
    },
    "binary_sensor": {
        "category": "binary_telemetry",
        "supports_state": True,
        "state_type": "binary",
        "readable_properties": ["state", "device_class"],
        "writable_properties": [],
        "supported_actions": [],
        "ha_services": [],
    },
    "cover": {
        "category": "shading_and_access",
        "supports_state": True,
        "state_type": "enum",
        "readable_properties": [
            "state",
            "current_position",
            "current_tilt_position",
            "device_class",
        ],
        "writable_properties": ["position", "tilt_position"],
        "supported_actions": [
            "open_cover",
            "close_cover",
            "stop_cover",
            "set_cover_position",
        ],
        "ha_services": [
            "cover.open_cover",
            "cover.close_cover",
            "cover.stop_cover",
            "cover.set_cover_position",
        ],
    },
    "lock": {
        "category": "security",
        "supports_state": True,
        "state_type": "enum",
        "readable_properties": ["state", "changed_by", "code_format"],
        "writable_properties": [],
        "supported_actions": ["lock", "unlock"],
        "ha_services": ["lock.lock", "lock.unlock"],
    },
    "media_player": {
        "category": "media",
        "supports_state": True,
        "state_type": "enum",
        "readable_properties": [
            "state",
            "volume_level",
            "is_volume_muted",
            "media_title",
            "media_artist",
            "source",
            "source_list",
        ],
        "writable_properties": ["volume_level", "is_volume_muted", "source"],
        "supported_actions": [
            "turn_on",
            "turn_off",
            "media_play",
            "media_pause",
            "media_stop",
            "volume_set",
            "volume_mute",
            "select_source",
        ],
        "ha_services": [
            "media_player.turn_on",
            "media_player.turn_off",
            "media_player.media_play",
            "media_player.media_pause",
            "media_player.media_stop",
            "media_player.volume_set",
            "media_player.volume_mute",
            "media_player.select_source",
        ],
    },
    "fan": {
        "category": "ventilation",
        "supports_state": True,
        "state_type": "binary",
        "readable_properties": [
            "state",
            "percentage",
            "oscillating",
            "direction",
            "preset_mode",
            "preset_modes",
        ],
        "writable_properties": [
            "percentage",
            "oscillating",
            "direction",
            "preset_mode",
        ],
        "supported_actions": [
            "turn_on",
            "turn_off",
            "toggle",
            "set_percentage",
            "oscillate",
            "set_direction",
        ],
        "ha_services": [
            "fan.turn_on",
            "fan.turn_off",
            "fan.toggle",
            "fan.set_percentage",
            "fan.oscillate",
            "fan.set_direction",
        ],
    },
}


class HACapabilityMapper:
    """Maps Home Assistant domains and entity attributes into Nova device capability profiles."""

    def get_domain_map(self) -> dict[str, HADeviceCapabilityProfile]:
        """Returns the full baseline capability profile map for all supported HA domains."""
        return {
            domain: HADeviceCapabilityProfile(domain=domain, **spec)
            for domain, spec in _DOMAIN_CAPABILITY_TEMPLATES.items()
        }

    def map_domain(
        self, domain: str, attributes: Optional[dict[str, Any]] = None
    ) -> HADeviceCapabilityProfile:
        """Maps a single HA domain (with optional live entity attributes) to a capability profile."""
        norm_domain = domain.strip().lower()
        attrs = attributes or {}
        template = _DOMAIN_CAPABILITY_TEMPLATES.get(norm_domain)

        if template is None:
            return HADeviceCapabilityProfile(
                domain=norm_domain,
                category="generic",
                supports_state=True,
                state_type="string",
                readable_properties=["state", *sorted(attrs.keys())[:10]],
                writable_properties=[],
                supported_actions=[],
                ha_services=[],
                detected_features=[],
            )

        detected: list[str] = []
        if norm_domain == "light":
            color_modes = attrs.get("supported_color_modes") or []
            if "brightness" in attrs or any(
                m in color_modes for m in ("brightness", "color_temp", "rgb", "hs", "xy")
            ):
                detected.append("dimming")
            if "color_temp" in attrs or "color_temp" in color_modes:
                detected.append("color_temperature")
            if "rgb_color" in attrs or any(
                m in color_modes for m in ("rgb", "rgbw", "rgbww", "hs", "xy")
            ):
                detected.append("color_rgb")
        elif norm_domain == "climate":
            if attrs.get("hvac_modes"):
                detected.extend(f"mode:{m}" for m in attrs["hvac_modes"])
            if "target_temp_high" in attrs and "target_temp_low" in attrs:
                detected.append("dual_setpoint")
            if "fan_modes" in attrs:
                detected.append("fan_control")
        elif norm_domain == "switch":
            if "current_power_w" in attrs or "today_energy_kwh" in attrs:
                detected.append("power_monitoring")
        elif norm_domain == "sensor":
            if attrs.get("device_class"):
                detected.append(f"device_class:{attrs['device_class']}")
            if attrs.get("unit_of_measurement"):
                detected.append(f"unit:{attrs['unit_of_measurement']}")
        elif norm_domain == "binary_sensor":
            if attrs.get("device_class"):
                detected.append(f"device_class:{attrs['device_class']}")
        elif norm_domain == "cover":
            if "current_position" in attrs:
                detected.append("position_control")
            if "current_tilt_position" in attrs:
                detected.append("tilt_control")
        elif norm_domain == "media_player":
            if "volume_level" in attrs:
                detected.append("volume_control")
            if attrs.get("source_list"):
                detected.append("source_selection")
        elif norm_domain == "fan":
            if "percentage" in attrs:
                detected.append("speed_percentage")
            if "oscillating" in attrs:
                detected.append("oscillation")

        return HADeviceCapabilityProfile(
            domain=norm_domain,
            category=template["category"],
            supports_state=template["supports_state"],
            state_type=template["state_type"],
            readable_properties=list(template["readable_properties"]),
            writable_properties=list(template["writable_properties"]),
            supported_actions=list(template["supported_actions"]),
            ha_services=list(template["ha_services"]),
            detected_features=detected,
        )

    def map_entity(self, raw_state: dict[str, Any]) -> HASyncedEntity:
        """Transforms a raw Home Assistant state dict into a structured `HASyncedEntity`."""
        entity_id = str(raw_state.get("entity_id") or "").strip()
        if not entity_id or "." not in entity_id:
            raise ValueError(
                f"Invalid Home Assistant entity_id: {entity_id!r} (expected '<domain>.<object_id>')."
            )
        domain = extract_domain(entity_id)
        attrs = dict(raw_state.get("attributes") or {})
        capabilities = self.map_domain(domain, attrs)

        return HASyncedEntity(
            entity_id=entity_id,
            domain=domain,
            friendly_name=str(
                raw_state.get("friendly_name")
                or attrs.get("friendly_name")
                or entity_id
            ),
            state=str(raw_state.get("state", "unknown")),
            attributes=attrs,
            capabilities=capabilities,
            area_id=raw_state.get("area_id") or attrs.get("area_id"),
            device_class=raw_state.get("device_class") or attrs.get("device_class"),
            last_changed=raw_state.get("last_changed"),
            last_updated=raw_state.get("last_updated"),
            synced_at=datetime.now(timezone.utc).isoformat(),
        )


# ---------------------------------------------------------------------------
# F21.5: Standalone Storage Layer (DatabaseStore.get_connection or sqlite3)
# ---------------------------------------------------------------------------


class HomeAssistantStorage:
    """Self-contained SQLite/DatabaseStore persistence for Phase 21 Home Assistant adapter."""

    DEFAULT_CONFIG_ID = "default"

    def __init__(self, store_or_path: Any) -> None:
        self._store = (
            store_or_path if hasattr(store_or_path, "get_connection") else None
        )
        self._db_path = None if self._store is not None else Path(store_or_path)
        self.ensure_schema()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self._store is not None:
            with self._store.get_connection() as conn:
                yield conn
            return

        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """Ensures migration 0012 tables exist (idempotent via CREATE IF NOT EXISTS)."""
        migration_sql = _MIGRATION_0012_PATH.read_text(encoding="utf-8")
        with self._connection() as conn:
            conn.executescript(migration_sql)

    # ---- Config ----

    def get_config(self, config_id: str = DEFAULT_CONFIG_ID) -> HAConfig:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM ha_config WHERE id = ?", (config_id,)
            ).fetchone()
        if row is None:
            return HAConfig(id=config_id)
        data = dict(row)
        return HAConfig(
            id=data["id"],
            base_url=data["base_url"],
            ws_url=data["ws_url"],
            access_token=data["access_token"],
            verify_ssl=bool(data["verify_ssl"]),
            timeout_seconds=float(data["timeout_seconds"]),
            reconnect_base_delay=float(data["reconnect_base_delay"]),
            reconnect_max_delay=float(data["reconnect_max_delay"]),
            auto_subscribe_events=json.loads(
                data["auto_subscribe_events"] or '["state_changed"]'
            ),
            enabled=bool(data["enabled"]),
            updated_at=data["updated_at"],
        )

    def upsert_config(self, config: HAConfig) -> HAConfig:
        now = datetime.now(timezone.utc).isoformat()
        stored = config.model_copy(update={"updated_at": now})
        sql = """
        INSERT INTO ha_config (
            id, base_url, ws_url, access_token, verify_ssl, timeout_seconds,
            reconnect_base_delay, reconnect_max_delay, auto_subscribe_events,
            enabled, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            base_url = excluded.base_url,
            ws_url = excluded.ws_url,
            access_token = excluded.access_token,
            verify_ssl = excluded.verify_ssl,
            timeout_seconds = excluded.timeout_seconds,
            reconnect_base_delay = excluded.reconnect_base_delay,
            reconnect_max_delay = excluded.reconnect_max_delay,
            auto_subscribe_events = excluded.auto_subscribe_events,
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    stored.id,
                    stored.base_url,
                    stored.ws_url,
                    stored.access_token,
                    1 if stored.verify_ssl else 0,
                    stored.timeout_seconds,
                    stored.reconnect_base_delay,
                    stored.reconnect_max_delay,
                    json.dumps(stored.auto_subscribe_events),
                    1 if stored.enabled else 0,
                    stored.updated_at,
                ),
            )
        return stored

    # ---- Synced Entities & Capabilities ----

    @staticmethod
    def _row_to_entity(row: Any) -> HASyncedEntity:
        data = dict(row)
        cap_raw = json.loads(data["capabilities"] or "{}")
        cap = HADeviceCapabilityProfile(**cap_raw)
        return HASyncedEntity(
            entity_id=data["entity_id"],
            domain=data["domain"],
            friendly_name=data["friendly_name"],
            state=data["state"],
            attributes=json.loads(data["attributes"] or "{}"),
            capabilities=cap,
            area_id=data["area_id"],
            device_class=data["device_class"],
            last_changed=data["last_changed"],
            last_updated=data["last_updated"],
            synced_at=data["synced_at"],
        )

    def upsert_entity(self, entity: HASyncedEntity) -> HASyncedEntity:
        sql = """
        INSERT INTO ha_entities (
            entity_id, domain, friendly_name, state, attributes, capabilities,
            area_id, device_class, last_changed, last_updated, synced_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET
            domain = excluded.domain,
            friendly_name = excluded.friendly_name,
            state = excluded.state,
            attributes = excluded.attributes,
            capabilities = excluded.capabilities,
            area_id = excluded.area_id,
            device_class = excluded.device_class,
            last_changed = excluded.last_changed,
            last_updated = excluded.last_updated,
            synced_at = excluded.synced_at
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    entity.entity_id,
                    entity.domain,
                    entity.friendly_name,
                    entity.state,
                    json.dumps(entity.attributes),
                    json.dumps(entity.capabilities.model_dump(mode="json")),
                    entity.area_id,
                    entity.device_class,
                    entity.last_changed,
                    entity.last_updated,
                    entity.synced_at,
                ),
            )
        return entity

    def upsert_entities(self, entities: list[HASyncedEntity]) -> list[HASyncedEntity]:
        for entity in entities:
            self.upsert_entity(entity)
        return entities

    def get_entity(self, entity_id: str) -> Optional[HASyncedEntity]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM ha_entities WHERE entity_id = ?", (entity_id,)
            ).fetchone()
        return self._row_to_entity(row) if row is not None else None

    def list_entities(self, *, domain: Optional[str] = None) -> list[HASyncedEntity]:
        sql = "SELECT * FROM ha_entities"
        params: list[Any] = []
        if domain:
            sql += " WHERE domain = ?"
            params.append(domain.strip().lower())
        sql += " ORDER BY entity_id ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_entity(r) for r in rows]

    def delete_entity(self, entity_id: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM ha_entities WHERE entity_id = ?", (entity_id,)
            )
            return cursor.rowcount > 0

    # ---- Entity Triggers ----

    @staticmethod
    def _row_to_trigger(row: Any) -> HAEntityTrigger:
        data = dict(row)
        raw_attr_val = data["attribute_value"]
        parsed_attr_val = json.loads(raw_attr_val) if raw_attr_val is not None else None
        return HAEntityTrigger(
            id=data["id"],
            name=data["name"],
            enabled=bool(data["enabled"]),
            entity_id=data["entity_id"],
            from_state=data["from_state"],
            to_state=data["to_state"],
            attribute_name=data["attribute_name"],
            attribute_operator=data["attribute_operator"],
            attribute_value=parsed_attr_val,
            cooldown_seconds=int(data["cooldown_seconds"]),
            event_type=data["event_type"],
            description=data["description"],
            last_triggered_at=data["last_triggered_at"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def create_trigger(self, trigger: HAEntityTrigger) -> HAEntityTrigger:
        now = datetime.now(timezone.utc).isoformat()
        trigger_id = trigger.id or f"hatrig_{os.urandom(8).hex()}"
        stored = trigger.model_copy(
            update={"id": trigger_id, "created_at": now, "updated_at": now}
        )
        sql = """
        INSERT INTO ha_entity_triggers (
            id, name, enabled, entity_id, from_state, to_state,
            attribute_name, attribute_operator, attribute_value,
            cooldown_seconds, event_type, description,
            last_triggered_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        attr_val_json = (
            json.dumps(stored.attribute_value)
            if stored.attribute_value is not None
            else None
        )
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    stored.id,
                    stored.name,
                    1 if stored.enabled else 0,
                    stored.entity_id,
                    stored.from_state,
                    stored.to_state,
                    stored.attribute_name,
                    stored.attribute_operator,
                    attr_val_json,
                    stored.cooldown_seconds,
                    stored.event_type,
                    stored.description,
                    stored.last_triggered_at,
                    stored.created_at,
                    stored.updated_at,
                ),
            )
        return stored

    def get_trigger(self, trigger_id: str) -> Optional[HAEntityTrigger]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM ha_entity_triggers WHERE id = ?", (trigger_id,)
            ).fetchone()
        return self._row_to_trigger(row) if row is not None else None

    def list_triggers(
        self,
        *,
        enabled_only: bool = False,
        entity_id: Optional[str] = None,
    ) -> list[HAEntityTrigger]:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        sql = "SELECT * FROM ha_entity_triggers"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_trigger(r) for r in rows]

    def update_trigger_last_triggered(
        self, trigger_id: str, triggered_at: str
    ) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE ha_entity_triggers SET last_triggered_at = ?, updated_at = ? WHERE id = ?",
                (triggered_at, triggered_at, trigger_id),
            )

    def delete_trigger(self, trigger_id: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM ha_entity_triggers WHERE id = ?", (trigger_id,)
            )
            return cursor.rowcount > 0

    # ---- Dispatch Audit Log ----

    def record_dispatch(
        self,
        *,
        direction: DispatchDirection,
        transport: str,
        event_type: str,
        entity_id: Optional[str] = None,
        domain: Optional[str] = None,
        service: Optional[str] = None,
        payload: Optional[dict[str, Any]] = None,
        result: Optional[dict[str, Any]] = None,
        status: str = "ok",
        error_message: Optional[str] = None,
    ) -> HADispatchLogEntry:
        entry_id = f"hadisp_{os.urandom(8).hex()}"
        now = datetime.now(timezone.utc).isoformat()
        entry = HADispatchLogEntry(
            id=entry_id,
            direction=direction,
            transport=transport,
            event_type=event_type,
            entity_id=entity_id,
            domain=domain,
            service=service,
            payload=payload or {},
            result=result or {},
            status=status,
            error_message=error_message,
            created_at=now,
        )
        sql = """
        INSERT INTO ha_dispatch_log (
            id, direction, transport, event_type, entity_id, domain, service,
            payload, result, status, error_message, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    entry.id,
                    entry.direction,
                    entry.transport,
                    entry.event_type,
                    entry.entity_id,
                    entry.domain,
                    entry.service,
                    json.dumps(entry.payload),
                    json.dumps(entry.result),
                    entry.status,
                    entry.error_message,
                    entry.created_at,
                ),
            )
        return entry

    def list_dispatch_logs(
        self,
        *,
        direction: Optional[DispatchDirection] = None,
        entity_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[HADispatchLogEntry]:
        clauses: list[str] = []
        params: list[Any] = []
        if direction:
            clauses.append("direction = ?")
            params.append(direction)
        if entity_id:
            clauses.append("entity_id = ?")
            params.append(entity_id)
        sql = "SELECT * FROM ha_dispatch_log"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [
            HADispatchLogEntry(
                id=r["id"],
                direction=r["direction"],
                transport=r["transport"],
                event_type=r["event_type"],
                entity_id=r["entity_id"],
                domain=r["domain"],
                service=r["service"],
                payload=json.loads(r["payload"] or "{}"),
                result=json.loads(r["result"] or "{}"),
                status=r["status"],
                error_message=r["error_message"],
                created_at=r["created_at"],
            )
            for r in rows
        ]


# ---------------------------------------------------------------------------
# F21.3: Event Dispatch API (Inbound Translation & Outbound call_service)
# ---------------------------------------------------------------------------


class HomeAssistantEventDispatcher:
    """Translates HA events into Nova automation events and dispatches outbound HA service calls."""

    def __init__(
        self,
        *,
        tracker: Optional[EntityStateTracker] = None,
        storage: Optional[HomeAssistantStorage] = None,
        ws_manager: Optional[HomeAssistantWSManager] = None,
        capability_mapper: Optional[HACapabilityMapper] = None,
        http_requester: Optional[HttpRequester] = None,
        on_event: Optional[EventCallback] = None,
    ) -> None:
        self.tracker = tracker or EntityStateTracker()
        self.storage = storage
        self.ws_manager = ws_manager
        self.capability_mapper = capability_mapper or HACapabilityMapper()
        self._http_requester = http_requester or _default_http_requester
        self._listeners: list[EventCallback] = []
        if on_event is not None:
            self._listeners.append(on_event)

    def add_listener(self, callback: EventCallback) -> None:
        self._listeners.append(callback)

    def remove_listener(self, callback: EventCallback) -> None:
        self._listeners = [cb for cb in self._listeners if cb is not callback]

    async def dispatch_incoming_event(
        self,
        raw_event: dict[str, Any],
        *,
        triggers: Optional[list[HAEntityTrigger]] = None,
        transport: str = "ingest",
        evaluated_at: Optional[datetime] = None,
    ) -> HAEventDispatchResult:
        """Normalizes an incoming HA event, evaluates triggers, notifies listeners, and logs audit."""
        event_obj = (
            raw_event["event"]
            if raw_event.get("type") == "event"
            and isinstance(raw_event.get("event"), dict)
            else raw_event
        )
        ha_event_type = str(
            event_obj.get("event_type")
            or ("state_changed" if "new_state" in event_obj else "custom")
        )
        timestamp = str(
            event_obj.get("time_fired")
            or (evaluated_at.isoformat() if evaluated_at else datetime.now(timezone.utc).isoformat())
        )

        transition: Optional[HAStateTransition] = None
        triggered: list[HATriggerMatch] = []
        suppressed: list[HATriggerMatch] = []
        entity_id: Optional[str] = None
        domain: Optional[str] = None
        payload: dict[str, Any] = dict(event_obj.get("data") or event_obj)

        if ha_event_type == "state_changed":
            transition = self.tracker.parse_state_changed(raw_event)
            if transition is not None:
                entity_id = transition.entity_id
                domain = transition.domain
                payload = transition.model_dump(mode="json")

                active_triggers = (
                    triggers
                    if triggers is not None
                    else (
                        self.storage.list_triggers(enabled_only=True)
                        if self.storage is not None
                        else []
                    )
                )
                triggered, suppressed = self.tracker.evaluate_triggers(
                    transition, active_triggers, evaluated_at=evaluated_at
                )

                if self.storage is not None:
                    # Keep synced entity table up to date with latest state
                    if transition.new_state is not None:
                        synced = self.capability_mapper.map_entity(
                            {
                                "entity_id": transition.entity_id,
                                "state": transition.new_state,
                                "attributes": transition.new_attributes,
                                "last_updated": transition.changed_at,
                            }
                        )
                        self.storage.upsert_entity(synced)
                    for match in triggered:
                        if match.trigger_id:
                            self.storage.update_trigger_last_triggered(
                                match.trigger_id, match.triggered_at
                            )
        else:
            entity_id = payload.get("entity_id")
            if entity_id:
                domain = extract_domain(str(entity_id))

        nova_event_type = f"ha.{ha_event_type}"
        nova_event = NovaAutomationEvent(
            event_id=f"haevt_{os.urandom(8).hex()}",
            source="home_assistant",
            event_type=nova_event_type,
            ha_event_type=ha_event_type,
            entity_id=entity_id,
            domain=domain,
            timestamp=timestamp,
            payload=payload,
            matched_triggers=triggered,
        )

        notified = 0
        for cb in list(self._listeners):
            res = cb(nova_event)
            if inspect.isawaitable(res):
                await res
            notified += 1

        audit_id = nova_event.event_id
        if self.storage is not None:
            entry = self.storage.record_dispatch(
                direction="inbound",
                transport=transport,
                event_type=nova_event.event_type,
                entity_id=entity_id,
                domain=domain,
                payload=payload,
                result={
                    "event_id": nova_event.event_id,
                    "triggered_count": len(triggered),
                    "suppressed_count": len(suppressed),
                    "listeners_notified": notified,
                },
                status="ok",
            )
            audit_id = entry.id

        return HAEventDispatchResult(
            audit_id=audit_id,
            event=nova_event,
            transition=transition,
            triggered=triggered,
            suppressed=suppressed,
            listeners_notified=notified,
        )

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        entity_id: Optional[str] = None,
        service_data: Optional[dict[str, Any]] = None,
        target: Optional[dict[str, Any]] = None,
        transport: Literal["auto", "ws", "rest"] = "auto",
        config: Optional[HAConfig] = None,
    ) -> HAServiceCallResult:
        """Dispatches an outbound Home Assistant `call_service` via WS or REST with audit logging."""
        clean_domain = domain.strip().lower()
        clean_service = service.strip().lower()
        if not clean_domain or not clean_service:
            raise ValueError("Both 'domain' and 'service' must be non-empty.")

        resolved_target = dict(target or {})
        resolved_data = dict(service_data or {})
        if entity_id:
            resolved_target.setdefault("entity_id", entity_id)

        cfg = config or (
            self.storage.get_config() if self.storage is not None else HAConfig()
        )

        use_ws = False
        if transport == "ws":
            use_ws = True
        elif transport == "auto":
            use_ws = self.ws_manager is not None and self.ws_manager.is_connected

        chosen_transport = "ws" if use_ws else "rest"
        executed_at = datetime.now(timezone.utc).isoformat()

        try:
            if use_ws:
                if self.ws_manager is None or not self.ws_manager.is_connected:
                    raise HomeAssistantConnectionError(
                        "WS transport requested, but Home Assistant WebSocket is not connected."
                    )
                response = await self.ws_manager.call_service(
                    clean_domain,
                    clean_service,
                    service_data=resolved_data or None,
                    target=resolved_target or None,
                    timeout=cfg.timeout_seconds,
                )
            else:
                if not cfg.access_token or not cfg.access_token.strip():
                    raise HomeAssistantAuthError(
                        "Home Assistant access token is not configured for REST call_service."
                    )
                url = f"{cfg.base_url}/api/services/{clean_domain}/{clean_service}"
                headers = {
                    "Authorization": f"Bearer {cfg.access_token}",
                    "Content-Type": "application/json",
                }
                rest_body = dict(resolved_data)
                if resolved_target:
                    rest_body.update(resolved_target)
                raw_resp = self._http_requester(
                    "POST", url, headers, rest_body, cfg.timeout_seconds
                )
                response = (
                    await raw_resp if inspect.isawaitable(raw_resp) else raw_resp
                )

            audit_id = f"hadisp_{os.urandom(8).hex()}"
            if self.storage is not None:
                entry = self.storage.record_dispatch(
                    direction="outbound",
                    transport=chosen_transport,
                    event_type="ha.call_service",
                    entity_id=entity_id or (
                        resolved_target.get("entity_id")
                        if isinstance(resolved_target.get("entity_id"), str)
                        else None
                    ),
                    domain=clean_domain,
                    service=clean_service,
                    payload={"target": resolved_target, "service_data": resolved_data},
                    result={"response": response},
                    status="ok",
                )
                audit_id = entry.id

            return HAServiceCallResult(
                audit_id=audit_id,
                domain=clean_domain,
                service=clean_service,
                entity_id=entity_id,
                target=resolved_target,
                service_data=resolved_data,
                transport=chosen_transport,
                success=True,
                response=response,
                error=None,
                executed_at=executed_at,
            )
        except Exception as exc:
            if self.storage is not None:
                self.storage.record_dispatch(
                    direction="outbound",
                    transport=chosen_transport,
                    event_type="ha.call_service",
                    entity_id=entity_id,
                    domain=clean_domain,
                    service=clean_service,
                    payload={"target": resolved_target, "service_data": resolved_data},
                    result={},
                    status="error",
                    error_message=str(exc),
                )
            raise


# ---------------------------------------------------------------------------
# High-Level Service Facade (F21.5)
# ---------------------------------------------------------------------------


class HomeAssistantService:
    """Facade combining storage, WS manager, entity tracker, capability mapper, and dispatcher."""

    def __init__(
        self,
        *,
        storage: HomeAssistantStorage,
        ws_factory: Optional[WSClientFactory] = None,
        http_requester: Optional[HttpRequester] = None,
        on_event: Optional[EventCallback] = None,
    ) -> None:
        self.storage = storage
        cfg = self.storage.get_config()
        self.ws_manager = HomeAssistantWSManager(
            ws_url=cfg.resolved_ws_url(),
            access_token=cfg.access_token,
            reconnect_base_delay=cfg.reconnect_base_delay,
            reconnect_max_delay=cfg.reconnect_max_delay,
            ws_factory=ws_factory,
        )
        self.tracker = EntityStateTracker()
        self.capability_mapper = HACapabilityMapper()
        self._http_requester = http_requester or _default_http_requester
        self.dispatcher = HomeAssistantEventDispatcher(
            tracker=self.tracker,
            storage=self.storage,
            ws_manager=self.ws_manager,
            capability_mapper=self.capability_mapper,
            http_requester=self._http_requester,
            on_event=on_event,
        )

        # Seed in-memory tracker from persisted entities
        for ent in self.storage.list_entities():
            self.tracker.seed_entity(
                HAEntityState(
                    entity_id=ent.entity_id,
                    domain=ent.domain,
                    state=ent.state,
                    attributes=ent.attributes,
                    friendly_name=ent.friendly_name,
                    device_class=ent.device_class,
                    area_id=ent.area_id,
                    last_changed=ent.last_changed,
                    last_updated=ent.last_updated,
                )
            )

    # ---- Config & Status ----

    def get_config(self) -> HAConfig:
        return self.storage.get_config()

    def update_config(self, **updates: Any) -> HAConfig:
        current = self.storage.get_config()
        merged = current.model_dump()
        for key, value in updates.items():
            if value is not None:
                merged[key] = value
        updated = HAConfig(**merged)
        saved = self.storage.upsert_config(updated)
        self.ws_manager.configure(
            ws_url=saved.resolved_ws_url(),
            access_token=saved.access_token,
            reconnect_base_delay=saved.reconnect_base_delay,
            reconnect_max_delay=saved.reconnect_max_delay,
        )
        return saved

    def get_status(self) -> dict[str, Any]:
        cfg = self.storage.get_config()
        ws_status = self.ws_manager.get_status()
        entities = self.storage.list_entities()
        triggers = self.storage.list_triggers()
        return {
            "enabled": cfg.enabled,
            "configured": bool(cfg.access_token and cfg.access_token.strip()),
            "base_url": cfg.base_url,
            "websocket": ws_status.model_dump(mode="json"),
            "synced_entities_count": len(entities),
            "triggers_count": len(triggers),
        }

    # ---- Entities & Capabilities Sync ----

    async def sync_entities(
        self,
        *,
        raw_states: Optional[list[dict[str, Any]]] = None,
        transport: Literal["auto", "ws", "rest"] = "auto",
    ) -> HASyncResult:
        """Syncs HA entities & capability maps from explicit payload, active WS, or HA REST API."""
        states = raw_states
        if states is None:
            cfg = self.storage.get_config()
            use_ws = transport == "ws" or (
                transport == "auto" and self.ws_manager.is_connected
            )
            if use_ws:
                states = await self.ws_manager.get_states(timeout=cfg.timeout_seconds)
            else:
                if not cfg.access_token or not cfg.access_token.strip():
                    raise HomeAssistantAuthError(
                        "Home Assistant access token is required to sync entities via REST."
                    )
                url = f"{cfg.base_url}/api/states"
                headers = {
                    "Authorization": f"Bearer {cfg.access_token}",
                    "Content-Type": "application/json",
                }
                raw_resp = self._http_requester(
                    "GET", url, headers, None, cfg.timeout_seconds
                )
                resolved = (
                    await raw_resp if inspect.isawaitable(raw_resp) else raw_resp
                )
                if not isinstance(resolved, list):
                    raise HomeAssistantServiceError(
                        "Unexpected response from Home Assistant /api/states (expected JSON array)."
                    )
                states = resolved

        synced_entities: list[HASyncedEntity] = []
        domain_counts: dict[str, int] = {}
        for item in states:
            mapped = self.capability_mapper.map_entity(item)
            synced_entities.append(mapped)
            domain_counts[mapped.domain] = domain_counts.get(mapped.domain, 0) + 1
            self.tracker.seed_entity(
                HAEntityState(
                    entity_id=mapped.entity_id,
                    domain=mapped.domain,
                    state=mapped.state,
                    attributes=mapped.attributes,
                    friendly_name=mapped.friendly_name,
                    device_class=mapped.device_class,
                    area_id=mapped.area_id,
                    last_changed=mapped.last_changed,
                    last_updated=mapped.last_updated,
                )
            )

        self.storage.upsert_entities(synced_entities)
        return HASyncResult(
            synced_count=len(synced_entities),
            domains=domain_counts,
            entities=synced_entities,
            synced_at=datetime.now(timezone.utc).isoformat(),
        )

    def list_entities(self, *, domain: Optional[str] = None) -> list[HASyncedEntity]:
        return self.storage.list_entities(domain=domain)

    def get_entity(self, entity_id: str) -> Optional[HASyncedEntity]:
        return self.storage.get_entity(entity_id)

    def get_capabilities(
        self, *, domain: Optional[str] = None
    ) -> dict[str, Any]:
        domain_map = self.capability_mapper.get_domain_map()
        if domain:
            norm = domain.strip().lower()
            profile = self.capability_mapper.map_domain(norm)
            entities = self.storage.list_entities(domain=norm)
            return {
                "domain": norm,
                "capability": profile.model_dump(mode="json"),
                "synced_entities_count": len(entities),
                "entities": [
                    {
                        "entity_id": e.entity_id,
                        "friendly_name": e.friendly_name,
                        "capabilities": e.capabilities.model_dump(mode="json"),
                    }
                    for e in entities
                ],
            }

        entities = self.storage.list_entities()
        return {
            "supported_domains": sorted(domain_map.keys()),
            "domain_capabilities": {
                k: v.model_dump(mode="json") for k, v in domain_map.items()
            },
            "synced_entities_count": len(entities),
        }

    # ---- Triggers ----

    def create_trigger(self, trigger: HAEntityTrigger) -> HAEntityTrigger:
        return self.storage.create_trigger(trigger)

    def list_triggers(
        self, *, enabled_only: bool = False, entity_id: Optional[str] = None
    ) -> list[HAEntityTrigger]:
        return self.storage.list_triggers(
            enabled_only=enabled_only, entity_id=entity_id
        )

    def delete_trigger(self, trigger_id: str) -> bool:
        return self.storage.delete_trigger(trigger_id)

    # ---- Events & Outbound Services ----

    async def ingest_event(
        self,
        raw_event: dict[str, Any],
        *,
        transport: str = "ingest",
        evaluated_at: Optional[datetime] = None,
    ) -> HAEventDispatchResult:
        return await self.dispatcher.dispatch_incoming_event(
            raw_event, transport=transport, evaluated_at=evaluated_at
        )

    async def call_service(
        self,
        domain: str,
        service: str,
        *,
        entity_id: Optional[str] = None,
        service_data: Optional[dict[str, Any]] = None,
        target: Optional[dict[str, Any]] = None,
        transport: Literal["auto", "ws", "rest"] = "auto",
    ) -> HAServiceCallResult:
        return await self.dispatcher.call_service(
            domain,
            service,
            entity_id=entity_id,
            service_data=service_data,
            target=target,
            transport=transport,
            config=self.storage.get_config(),
        )

    def list_dispatch_logs(
        self,
        *,
        direction: Optional[DispatchDirection] = None,
        entity_id: Optional[str] = None,
        limit: int = 50,
    ) -> list[HADispatchLogEntry]:
        return self.storage.list_dispatch_logs(
            direction=direction, entity_id=entity_id, limit=limit
        )
