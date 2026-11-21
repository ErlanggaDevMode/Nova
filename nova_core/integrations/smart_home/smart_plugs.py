"""Smart Plug HTTP & Local Integrations — Shelly & TP-Link Kasa (Phase 20: F20.1–F20.5).

Provides:
- F20.1: `ShellyPlugClient` supporting Shelly Gen1 (`/relay/{channel}?turn=on|off|toggle`, `/status`)
  and Gen2 RPC (`/rpc/Switch.Set`, `/rpc/Switch.GetStatus`) with optional HTTP Basic / Bearer token
  auth, timeouts, and pluggable HTTP transport for offline testing.
- F20.2: TP-Link Kasa Smart Home Protocol XOR-autokey cipher (`kasa_encrypt`, `kasa_decrypt`,
  `kasa_pack_tcp`, `kasa_unpack_tcp`, starting key 171, 4-byte big-endian length framing over TCP
  port 9999) and `KasaPlugClient` supporting `system.get_sysinfo`, `system.set_relay_state`,
  `emeter.get_realtime`, and injectable socket/transport layer.
- F20.3: `SmartPlugDiscoveryScanner` — bounded local-network CIDR/host-list scanner probing Shelly
  HTTP endpoints and Kasa local discovery payloads with concurrency cap, timeout guards,
  private-network validation, and injectable probe backends.
- F20.4: `SmartPlugStorage` (SQLite/DatabaseStore persistence) and `SmartPlugService` coordinating
  registration, live power/status sync, control commands, discovery, and telemetry history.
- F20.5: `SmartPlugRuleParser` parsing and validating custom smart-plug category/room automation
  rules (structured dict or compact DSL) into normalized conditions & action templates compatible
  with Nova's automation engine.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import math
import os
import re
import shlex
import socket
import sqlite3
import struct
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

_MIGRATION_0011_PATH = (
    Path(__file__).resolve().parent.parent.parent / "db" / "migrations" / "0011_smart_plugs.sql"
)

KASA_DEFAULT_PORT = 9999
KASA_XOR_INITIAL_KEY = 171
KASA_MAX_FRAME_BYTES = 1024 * 1024  # 1 MiB safety guard

SmartPlugProvider = Literal["shelly", "kasa"]
ShellyGeneration = Literal["gen1", "gen2"]
SmartPlugAction = Literal["on", "off", "toggle"]
RuleMatchMode = Literal["all", "any"]

# Transport / probe callback types for offline testing
# (method, url, params, json_body, headers, timeout) -> (status_code, parsed_json_or_text)
ShellyHttpTransport = Callable[
    [str, str, Optional[dict[str, Any]], Optional[dict[str, Any]], dict[str, str], float],
    tuple[int, Any] | dict[str, Any],
]

# (host, port, framed_request_bytes, timeout) -> framed_response_bytes
KasaTcpTransport = Callable[[str, int, bytes, float], bytes]

# (host, timeout) -> Optional[DiscoveredSmartPlug]
PlugProbeFn = Callable[[str, float], Optional["DiscoveredSmartPlug"]]

_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


# ---------------------------------------------------------------------------
# Errors & Helpers
# ---------------------------------------------------------------------------


class SmartPlugError(Exception):
    """Base error raised by smart plug clients and service operations."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
        provider: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable
        self.provider = provider


class SmartPlugTimeoutError(SmartPlugError):
    """Raised when a smart plug request times out."""

    def __init__(self, message: str, *, provider: Optional[str] = None) -> None:
        super().__init__(message, status_code=504, retryable=True, provider=provider)


class SmartPlugAuthError(SmartPlugError):
    """Raised when smart plug credentials are rejected."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 401,
        provider: Optional[str] = None,
    ) -> None:
        super().__init__(message, status_code=status_code, retryable=False, provider=provider)


class SmartPlugProtocolError(SmartPlugError):
    """Raised when a device returns malformed or error-flagged protocol data."""

    def __init__(self, message: str, *, provider: Optional[str] = None) -> None:
        super().__init__(message, status_code=502, retryable=False, provider=provider)


class SmartPlugRuleParseError(ValueError):
    """Raised when a smart plug automation rule fails syntax or semantic validation."""

    def __init__(self, message: str, *, errors: Optional[list[str]] = None) -> None:
        self.errors = errors or [message]
        super().__init__(message)


def mask_secret(secret: Optional[str]) -> str:
    """Masks passwords/tokens so raw credentials are never leaked in responses."""
    if not secret:
        return ""
    clean = secret.strip()
    if not clean:
        return ""
    if len(clean) <= 6:
        return "*" * len(clean)
    if len(clean) <= 10:
        return f"{'*' * (len(clean) - 4)}{clean[-4:]}"
    return f"{clean[:2]}{'*' * (len(clean) - 6)}{clean[-4:]}"


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_identifier(value: str, field_name: str) -> str:
    cleaned = value.strip().lower().replace(" ", "_")
    if not cleaned or not _SLUG_RE.match(cleaned):
        raise ValueError(
            f"Invalid {field_name} '{value}': must match [a-zA-Z0-9][a-zA-Z0-9_-]{{0,63}}."
        )
    return cleaned


def _validate_private_ip(host: str) -> str:
    """Validates that `host` is a private, link-local, or loopback IP address."""
    try:
        addr = ipaddress.ip_address(host.strip())
    except ValueError as exc:
        raise ValueError(f"Invalid IP address '{host}': {exc}") from exc

    if not (addr.is_private or addr.is_loopback or addr.is_link_local):
        raise ValueError(
            f"Refusing to scan non-private IP address '{addr}'. "
            "Only private/LAN, loopback, or link-local addresses are allowed."
        )
    if addr.is_multicast or addr.is_unspecified:
        raise ValueError(f"Cannot scan multicast or unspecified IP address '{addr}'.")
    return str(addr)


# ---------------------------------------------------------------------------
# Domain Models
# ---------------------------------------------------------------------------


class SmartPlugStatusSnapshot(BaseModel):
    """Normalized live status & power telemetry snapshot for a smart plug."""

    plug_id: Optional[str] = Field(default=None)
    provider: SmartPlugProvider
    host: str
    channel: int = Field(default=0, ge=0, le=32)
    online: bool = Field(default=True)
    is_on: Optional[bool] = Field(default=None)
    power_watts: Optional[float] = Field(default=None)
    voltage: Optional[float] = Field(default=None)
    current_amps: Optional[float] = Field(default=None)
    total_energy_wh: Optional[float] = Field(default=None)
    temperature_c: Optional[float] = Field(default=None)
    device_model: Optional[str] = Field(default=None)
    device_alias: Optional[str] = Field(default=None)
    firmware: Optional[str] = Field(default=None)
    synced_at: str = Field(default_factory=_utc_now_iso)
    error: Optional[str] = Field(default=None)
    raw: dict[str, Any] = Field(default_factory=dict)


class SmartPlugDevice(BaseModel):
    """Persisted smart plug registration and latest cached status."""

    id: Optional[str] = Field(default=None, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    provider: SmartPlugProvider
    host: str = Field(..., min_length=1, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    protocol_version: str = Field(default="gen1", min_length=1, max_length=32)
    channel: int = Field(default=0, ge=0, le=32)
    category: str = Field(default="general", min_length=1, max_length=64)
    room: Optional[str] = Field(default=None, max_length=64)
    auth_username: Optional[str] = Field(default=None, max_length=128)
    auth_password: Optional[str] = Field(default=None, max_length=256)
    auth_token: Optional[str] = Field(default=None, max_length=512)
    use_https: bool = Field(default=False)
    timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    enabled: bool = Field(default=True)
    is_on: Optional[bool] = Field(default=None)
    power_watts: Optional[float] = Field(default=None)
    voltage: Optional[float] = Field(default=None)
    current_amps: Optional[float] = Field(default=None)
    total_energy_wh: Optional[float] = Field(default=None)
    online: bool = Field(default=False)
    last_synced_at: Optional[str] = Field(default=None)
    last_error: Optional[str] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: Optional[str] = Field(default=None)
    updated_at: Optional[str] = Field(default=None)

    @field_validator("host")
    @classmethod
    def _clean_host(cls, v: str) -> str:
        clean = v.strip()
        if not clean:
            raise ValueError("Smart plug host cannot be empty.")
        if "://" in clean or "/" in clean or " " in clean:
            raise ValueError("Smart plug host must be a bare hostname or IP address without scheme/path.")
        return clean

    @field_validator("category")
    @classmethod
    def _clean_category(cls, v: str) -> str:
        return _normalize_identifier(v, "category")

    @field_validator("room")
    @classmethod
    def _clean_room(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        return _normalize_identifier(v, "room")

    @model_validator(mode="after")
    def _validate_provider_protocol(self) -> "SmartPlugDevice":
        if self.provider == "shelly":
            pv = self.protocol_version.lower()
            if pv not in {"gen1", "gen2"}:
                raise ValueError("Shelly plugs require protocol_version 'gen1' or 'gen2'.")
            self.protocol_version = pv
            if self.port is None:
                self.port = 443 if self.use_https else 80
        elif self.provider == "kasa":
            self.protocol_version = "local_tcp"
            if self.port is None:
                self.port = KASA_DEFAULT_PORT
        return self

    def to_public_dict(self) -> dict[str, Any]:
        """Returns a response-safe dict with credentials masked/omitted."""
        return {
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "host": self.host,
            "port": self.port,
            "protocol_version": self.protocol_version,
            "channel": self.channel,
            "category": self.category,
            "room": self.room,
            "auth_username": self.auth_username,
            "has_auth_password": bool(self.auth_password),
            "has_auth_token": bool(self.auth_token),
            "auth_token_masked": mask_secret(self.auth_token),
            "use_https": self.use_https,
            "timeout_seconds": self.timeout_seconds,
            "enabled": self.enabled,
            "is_on": self.is_on,
            "power_watts": self.power_watts,
            "voltage": self.voltage,
            "current_amps": self.current_amps,
            "total_energy_wh": self.total_energy_wh,
            "online": self.online,
            "last_synced_at": self.last_synced_at,
            "last_error": self.last_error,
            "metadata": self.metadata,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class DiscoveredSmartPlug(BaseModel):
    """Smart plug discovered during a local network scan (F20.3)."""

    provider: SmartPlugProvider
    host: str
    port: int
    protocol_version: str
    name: Optional[str] = Field(default=None)
    model: Optional[str] = Field(default=None)
    mac: Optional[str] = Field(default=None)
    is_on: Optional[bool] = Field(default=None)
    power_watts: Optional[float] = Field(default=None)
    latency_ms: Optional[float] = Field(default=None)
    raw: dict[str, Any] = Field(default_factory=dict)


class DiscoveryScanResult(BaseModel):
    """Aggregate result of a bounded smart plug discovery scan."""

    targets_scanned: int
    providers: list[SmartPlugProvider]
    discovered: list[DiscoveredSmartPlug] = Field(default_factory=list)
    registered_plug_ids: list[str] = Field(default_factory=list)
    duration_ms: float
    scanned_at: str = Field(default_factory=_utc_now_iso)


class SmartPlugCommandResult(BaseModel):
    """Result of sending an on/off/toggle control command to a registered plug."""

    plug_id: str
    provider: SmartPlugProvider
    action: SmartPlugAction
    channel: int
    success: bool
    is_on: Optional[bool] = None
    snapshot: SmartPlugStatusSnapshot


class SmartPlugBatchSyncResult(BaseModel):
    """Aggregate result of syncing multiple registered smart plugs."""

    total: int
    online_count: int
    offline_count: int
    total_power_watts: float
    snapshots: list[SmartPlugStatusSnapshot] = Field(default_factory=list)
    synced_at: str = Field(default_factory=_utc_now_iso)


# ---------------------------------------------------------------------------
# F20.1: Shelly Plug HTTP Commands Sender (Gen1 & Gen2 RPC)
# ---------------------------------------------------------------------------


def _default_shelly_http_transport(
    method: str,
    url: str,
    params: Optional[dict[str, Any]],
    json_body: Optional[dict[str, Any]],
    headers: dict[str, str],
    timeout: float,
) -> tuple[int, Any]:
    """Default httpx-backed transport for Shelly plugs with structured error mapping."""
    try:
        resp = httpx.request(
            method=method.upper(),
            url=url,
            params=params,
            json=json_body,
            headers=headers,
            timeout=timeout,
        )
    except httpx.TimeoutException as exc:
        raise SmartPlugTimeoutError(
            f"Shelly HTTP request to {url} timed out after {timeout}s.",
            provider="shelly",
        ) from exc
    except httpx.HTTPError as exc:
        raise SmartPlugError(
            f"Shelly HTTP network error for {url}: {exc}",
            retryable=True,
            provider="shelly",
        ) from exc

    try:
        payload: Any = resp.json()
    except ValueError:
        payload = resp.text
    return resp.status_code, payload


class ShellyPlugClient:
    """HTTP client for Shelly Gen1 REST and Gen2 JSON-RPC smart plugs (F20.1)."""

    def __init__(
        self,
        host: str,
        *,
        generation: ShellyGeneration = "gen1",
        port: Optional[int] = None,
        use_https: bool = False,
        username: Optional[str] = None,
        password: Optional[str] = None,
        auth_token: Optional[str] = None,
        timeout_seconds: float = 5.0,
        transport: Optional[ShellyHttpTransport] = None,
    ) -> None:
        clean_host = host.strip()
        if not clean_host:
            raise ValueError("Shelly host cannot be empty.")
        if generation not in ("gen1", "gen2"):
            raise ValueError(f"Unsupported Shelly generation '{generation}'. Use 'gen1' or 'gen2'.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")

        self.host = clean_host
        self.generation: ShellyGeneration = generation
        self.use_https = bool(use_https)
        self.port = port or (443 if self.use_https else 80)
        self.username = username
        self.password = password
        self.auth_token = auth_token
        self.timeout_seconds = float(timeout_seconds)
        self._transport: ShellyHttpTransport = transport or _default_shelly_http_transport

    @property
    def base_url(self) -> str:
        scheme = "https" if self.use_https else "http"
        default_port = 443 if self.use_https else 80
        if self.port == default_port:
            return f"{scheme}://{self.host}"
        return f"{scheme}://{self.host}:{self.port}"

    def _build_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Accept": "application/json"}
        if self.auth_token and self.auth_token.strip():
            headers["Authorization"] = f"Bearer {self.auth_token.strip()}"
        elif self.username is not None and self.password is not None:
            raw = f"{self.username}:{self.password}".encode("utf-8")
            encoded = base64.b64encode(raw).decode("ascii")
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        headers = self._build_headers()
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            raw_result = self._transport(
                method.upper(),
                url,
                params,
                json_body,
                headers,
                self.timeout_seconds,
            )
        except SmartPlugError:
            raise
        except TimeoutError as exc:
            raise SmartPlugTimeoutError(
                f"Shelly request to {url} timed out: {exc}",
                provider="shelly",
            ) from exc
        except Exception as exc:
            raise SmartPlugError(
                f"Shelly transport failure for {url}: {exc}",
                retryable=True,
                provider="shelly",
            ) from exc

        if isinstance(raw_result, tuple) and len(raw_result) == 2:
            status_code, payload = raw_result
        elif isinstance(raw_result, dict):
            status_code, payload = 200, raw_result
        else:
            raise SmartPlugProtocolError(
                "Shelly transport returned an unexpected response type.",
                provider="shelly",
            )

        if status_code in (401, 403):
            raise SmartPlugAuthError(
                f"Shelly authentication failed (HTTP {status_code}) for {self.host}.",
                status_code=status_code,
                provider="shelly",
            )
        if status_code == 404:
            raise SmartPlugError(
                f"Shelly endpoint '{path}' not found on {self.host} (HTTP 404).",
                status_code=404,
                provider="shelly",
            )
        if status_code >= 500:
            raise SmartPlugError(
                f"Shelly device error (HTTP {status_code}) on {self.host}.",
                status_code=status_code,
                retryable=True,
                provider="shelly",
            )
        if status_code >= 400:
            raise SmartPlugError(
                f"Shelly request rejected (HTTP {status_code}) on {self.host}.",
                status_code=status_code,
                provider="shelly",
            )

        if not isinstance(payload, dict):
            raise SmartPlugProtocolError(
                f"Shelly endpoint '{path}' returned non-JSON object payload.",
                provider="shelly",
            )

        # Gen2 RPC error envelope: {"code": -103, "message": "..."}
        if self.generation == "gen2" and "code" in payload and "message" in payload:
            code = payload.get("code")
            if isinstance(code, int) and code != 0:
                raise SmartPlugProtocolError(
                    f"Shelly Gen2 RPC error ({code}): {payload.get('message')}",
                    provider="shelly",
                )

        return payload

    def _parse_gen1_status(
        self,
        payload: dict[str, Any],
        *,
        channel: int,
        plug_id: Optional[str] = None,
    ) -> SmartPlugStatusSnapshot:
        relays = payload.get("relays")
        meters = payload.get("meters")

        is_on: Optional[bool] = None
        if isinstance(relays, list) and channel < len(relays) and isinstance(relays[channel], dict):
            if "ison" in relays[channel]:
                is_on = bool(relays[channel]["ison"])
        elif "ison" in payload:
            is_on = bool(payload["ison"])

        power_watts: Optional[float] = None
        total_energy_wh: Optional[float] = None
        if isinstance(meters, list) and channel < len(meters) and isinstance(meters[channel], dict):
            meter = meters[channel]
            if meter.get("power") is not None:
                power_watts = float(meter["power"])
            # Gen1 meter.total is in Watt-minutes; convert to Watt-hours
            if meter.get("total") is not None:
                total_energy_wh = round(float(meter["total"]) / 60.0, 4)

        temp_c: Optional[float] = None
        if payload.get("temperature") is not None:
            temp_c = float(payload["temperature"])
        elif isinstance(payload.get("tmp"), dict) and payload["tmp"].get("tC") is not None:
            temp_c = float(payload["tmp"]["tC"])

        fw: Optional[str] = None
        update = payload.get("update")
        if isinstance(update, dict) and update.get("old_version"):
            fw = str(update["old_version"])

        return SmartPlugStatusSnapshot(
            plug_id=plug_id,
            provider="shelly",
            host=self.host,
            channel=channel,
            online=True,
            is_on=is_on,
            power_watts=power_watts,
            total_energy_wh=total_energy_wh,
            temperature_c=temp_c,
            firmware=fw,
            raw=payload,
        )

    def _parse_gen2_status(
        self,
        payload: dict[str, Any],
        *,
        channel: int,
        plug_id: Optional[str] = None,
    ) -> SmartPlugStatusSnapshot:
        is_on: Optional[bool] = None
        if "output" in payload:
            is_on = bool(payload["output"])
        elif "was_on" in payload:
            is_on = not bool(payload["was_on"])

        power_watts = float(payload["apower"]) if payload.get("apower") is not None else None
        voltage = float(payload["voltage"]) if payload.get("voltage") is not None else None
        current_amps = float(payload["current"]) if payload.get("current") is not None else None

        total_energy_wh: Optional[float] = None
        aenergy = payload.get("aenergy")
        if isinstance(aenergy, dict) and aenergy.get("total") is not None:
            total_energy_wh = float(aenergy["total"])

        temp_c: Optional[float] = None
        temp = payload.get("temperature")
        if isinstance(temp, dict) and temp.get("tC") is not None:
            temp_c = float(temp["tC"])

        return SmartPlugStatusSnapshot(
            plug_id=plug_id,
            provider="shelly",
            host=self.host,
            channel=channel,
            online=True,
            is_on=is_on,
            power_watts=power_watts,
            voltage=voltage,
            current_amps=current_amps,
            total_energy_wh=total_energy_wh,
            temperature_c=temp_c,
            raw=payload,
        )

    def get_status(
        self,
        channel: int = 0,
        *,
        plug_id: Optional[str] = None,
    ) -> SmartPlugStatusSnapshot:
        """Fetches live switch state and power telemetry from the Shelly plug."""
        if channel < 0:
            raise ValueError("Channel must be >= 0.")

        if self.generation == "gen1":
            payload = self._request("GET", "/status")
            return self._parse_gen1_status(payload, channel=channel, plug_id=plug_id)

        payload = self._request("POST", "/rpc/Switch.GetStatus", json_body={"id": channel})
        return self._parse_gen2_status(payload, channel=channel, plug_id=plug_id)

    def send_command(
        self,
        action: SmartPlugAction,
        *,
        channel: int = 0,
        plug_id: Optional[str] = None,
    ) -> SmartPlugStatusSnapshot:
        """Sends an on/off/toggle relay command to a Shelly Gen1 or Gen2 plug."""
        if action not in ("on", "off", "toggle"):
            raise ValueError(f"Unsupported Shelly action '{action}'. Use 'on', 'off', or 'toggle'.")
        if channel < 0:
            raise ValueError("Channel must be >= 0.")

        if self.generation == "gen1":
            payload = self._request(
                "GET",
                f"/relay/{channel}",
                params={"turn": action},
            )
            return self._parse_gen1_status(payload, channel=channel, plug_id=plug_id)

        # Gen2 RPC: Switch.Set (for toggle, inspect current status via Switch.GetStatus first)
        if action == "toggle":
            current = self.get_status(channel=channel, plug_id=plug_id)
            target_on = not bool(current.is_on)
        else:
            target_on = action == "on"

        rpc_res = self._request(
            "POST",
            "/rpc/Switch.Set",
            json_body={"id": channel, "on": target_on},
        )
        return SmartPlugStatusSnapshot(
            plug_id=plug_id,
            provider="shelly",
            host=self.host,
            channel=channel,
            online=True,
            is_on=target_on,
            raw=rpc_res,
        )

    def turn_on(self, channel: int = 0, *, plug_id: Optional[str] = None) -> SmartPlugStatusSnapshot:
        return self.send_command("on", channel=channel, plug_id=plug_id)

    def turn_off(self, channel: int = 0, *, plug_id: Optional[str] = None) -> SmartPlugStatusSnapshot:
        return self.send_command("off", channel=channel, plug_id=plug_id)

    def toggle(self, channel: int = 0, *, plug_id: Optional[str] = None) -> SmartPlugStatusSnapshot:
        return self.send_command("toggle", channel=channel, plug_id=plug_id)

    def get_device_info(self) -> dict[str, Any]:
        """Fetches device identification metadata (Gen1 `/shelly`, Gen2 `/rpc/Shelly.GetDeviceInfo`)."""
        if self.generation == "gen1":
            return self._request("GET", "/shelly")
        return self._request("GET", "/rpc/Shelly.GetDeviceInfo")


# ---------------------------------------------------------------------------
# F20.2: TP-Link Kasa Local Smart Plug Protocol Codec & Driver
# ---------------------------------------------------------------------------


def kasa_encrypt(plaintext: str | bytes) -> bytes:
    """Encrypts payload using the TP-Link Smart Home XOR-autokey cipher (initial key 171)."""
    data = plaintext.encode("utf-8") if isinstance(plaintext, str) else bytes(plaintext)
    key = KASA_XOR_INITIAL_KEY
    out = bytearray(len(data))
    for i, byte in enumerate(data):
        cipher_byte = key ^ byte
        key = cipher_byte
        out[i] = cipher_byte
    return bytes(out)


def kasa_decrypt(ciphertext: bytes) -> str:
    """Decrypts payload using the TP-Link Smart Home XOR-autokey cipher (initial key 171)."""
    key = KASA_XOR_INITIAL_KEY
    out = bytearray(len(ciphertext))
    for i, byte in enumerate(ciphertext):
        plain_byte = key ^ byte
        key = byte
        out[i] = plain_byte
    try:
        return out.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SmartPlugProtocolError(
            f"Failed to UTF-8 decode decrypted Kasa payload: {exc}",
            provider="kasa",
        ) from exc


def kasa_pack_tcp(payload: dict[str, Any] | str) -> bytes:
    """Encodes a JSON dict or string into a 4-byte big-endian length-framed Kasa TCP packet."""
    text = json.dumps(payload, separators=(",", ":")) if isinstance(payload, dict) else payload
    encrypted = kasa_encrypt(text)
    return struct.pack(">I", len(encrypted)) + encrypted


def kasa_unpack_tcp(frame: bytes) -> dict[str, Any]:
    """Unpacks and decrypts a 4-byte big-endian length-framed Kasa TCP packet into a JSON dict."""
    if len(frame) < 4:
        raise SmartPlugProtocolError(
            f"Truncated Kasa TCP frame: expected at least 4 length bytes, got {len(frame)}.",
            provider="kasa",
        )
    declared_len = struct.unpack(">I", frame[:4])[0]
    if declared_len == 0 or declared_len > KASA_MAX_FRAME_BYTES:
        raise SmartPlugProtocolError(
            f"Invalid Kasa TCP frame length header: {declared_len} bytes.",
            provider="kasa",
        )
    body = frame[4:]
    if len(body) < declared_len:
        raise SmartPlugProtocolError(
            f"Incomplete Kasa TCP payload: declared {declared_len} bytes, received {len(body)}.",
            provider="kasa",
        )

    decrypted = kasa_decrypt(body[:declared_len])
    try:
        parsed = json.loads(decrypted)
    except json.JSONDecodeError as exc:
        raise SmartPlugProtocolError(
            f"Kasa device returned invalid JSON payload: {exc}",
            provider="kasa",
        ) from exc

    if not isinstance(parsed, dict):
        raise SmartPlugProtocolError(
            "Kasa device returned non-object JSON payload.",
            provider="kasa",
        )
    return parsed


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise SmartPlugProtocolError(
                f"Kasa TCP connection closed prematurely (expected {size} bytes, got {len(chunks)}).",
                provider="kasa",
            )
        chunks.extend(chunk)
    return bytes(chunks)


def _default_kasa_tcp_transport(host: str, port: int, frame: bytes, timeout: float) -> bytes:
    """Default blocking TCP transport on port 9999 with exact framed reads."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(frame)
            header = _recv_exact(sock, 4)
            length = struct.unpack(">I", header)[0]
            if length == 0 or length > KASA_MAX_FRAME_BYTES:
                raise SmartPlugProtocolError(
                    f"Kasa TCP response frame length out of bounds: {length}.",
                    provider="kasa",
                )
            body = _recv_exact(sock, length)
            return header + body
    except SmartPlugError:
        raise
    except (socket.timeout, TimeoutError) as exc:
        raise SmartPlugTimeoutError(
            f"Kasa TCP request to {host}:{port} timed out after {timeout}s.",
            provider="kasa",
        ) from exc
    except OSError as exc:
        raise SmartPlugError(
            f"Kasa TCP connection error for {host}:{port}: {exc}",
            retryable=True,
            provider="kasa",
        ) from exc


class KasaPlugClient:
    """Local TP-Link Kasa smart plug driver over TCP port 9999 (F20.2)."""

    def __init__(
        self,
        host: str,
        *,
        port: int = KASA_DEFAULT_PORT,
        timeout_seconds: float = 5.0,
        transport: Optional[KasaTcpTransport] = None,
    ) -> None:
        clean_host = host.strip()
        if not clean_host:
            raise ValueError("Kasa host cannot be empty.")
        if not (1 <= port <= 65535):
            raise ValueError(f"Invalid Kasa port: {port}.")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0.")

        self.host = clean_host
        self.port = int(port)
        self.timeout_seconds = float(timeout_seconds)
        self._transport: KasaTcpTransport = transport or _default_kasa_tcp_transport

    def send_raw_command(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Encrypts, frames, transmits, and decodes a Kasa smart home command."""
        frame = kasa_pack_tcp(payload)
        try:
            response_frame = self._transport(self.host, self.port, frame, self.timeout_seconds)
        except SmartPlugError:
            raise
        except (socket.timeout, TimeoutError) as exc:
            raise SmartPlugTimeoutError(
                f"Kasa TCP request to {self.host}:{self.port} timed out: {exc}",
                provider="kasa",
            ) from exc
        except Exception as exc:
            raise SmartPlugError(
                f"Kasa transport error for {self.host}:{self.port}: {exc}",
                retryable=True,
                provider="kasa",
            ) from exc

        return kasa_unpack_tcp(response_frame)

    @staticmethod
    def _extract_section(
        response: dict[str, Any],
        namespace: str,
        method: str,
    ) -> dict[str, Any]:
        ns_obj = response.get(namespace)
        if not isinstance(ns_obj, dict):
            raise SmartPlugProtocolError(
                f"Kasa response missing '{namespace}' namespace.",
                provider="kasa",
            )
        result = ns_obj.get(method)
        if not isinstance(result, dict):
            raise SmartPlugProtocolError(
                f"Kasa response missing '{namespace}.{method}' object.",
                provider="kasa",
            )
        err_code = result.get("err_code", 0)
        if err_code != 0:
            err_msg = result.get("err_msg") or "Unknown Kasa protocol error"
            raise SmartPlugProtocolError(
                f"Kasa command '{namespace}.{method}' failed (err_code={err_code}): {err_msg}",
                provider="kasa",
            )
        return result

    def get_sysinfo(self) -> dict[str, Any]:
        """Executes `system.get_sysinfo` on the Kasa plug."""
        resp = self.send_raw_command({"system": {"get_sysinfo": {}}})
        return self._extract_section(resp, "system", "get_sysinfo")

    def set_relay_state(
        self,
        state: bool,
        *,
        child_ids: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Executes `system.set_relay_state` (1=on, 0=off)."""
        cmd: dict[str, Any] = {
            "system": {"set_relay_state": {"state": 1 if state else 0}}
        }
        if child_ids:
            cmd["context"] = {"child_ids": list(child_ids)}
        resp = self.send_raw_command(cmd)
        return self._extract_section(resp, "system", "set_relay_state")

    def get_realtime_emeter(self) -> dict[str, Any]:
        """Executes `emeter.get_realtime` and returns normalized telemetry fields."""
        resp = self.send_raw_command({"emeter": {"get_realtime": {}}})
        raw_emeter = self._extract_section(resp, "emeter", "get_realtime")

        # Handle both Kasa hardware v1 (milli-units) and v2 (base units)
        if "power_mw" in raw_emeter:
            power_watts = round(float(raw_emeter["power_mw"]) / 1000.0, 4)
        elif "power" in raw_emeter:
            power_watts = float(raw_emeter["power"])
        else:
            power_watts = None

        if "voltage_mv" in raw_emeter:
            voltage = round(float(raw_emeter["voltage_mv"]) / 1000.0, 4)
        elif "voltage" in raw_emeter:
            voltage = float(raw_emeter["voltage"])
        else:
            voltage = None

        if "current_ma" in raw_emeter:
            current_amps = round(float(raw_emeter["current_ma"]) / 1000.0, 4)
        elif "current" in raw_emeter:
            current_amps = float(raw_emeter["current"])
        else:
            current_amps = None

        if "total_wh" in raw_emeter:
            total_energy_wh = float(raw_emeter["total_wh"])
        elif "total" in raw_emeter:
            # Hardware v2 reports total in kWh -> convert to Wh
            total_energy_wh = round(float(raw_emeter["total"]) * 1000.0, 4)
        else:
            total_energy_wh = None

        return {
            "power_watts": power_watts,
            "voltage": voltage,
            "current_amps": current_amps,
            "total_energy_wh": total_energy_wh,
            "raw": raw_emeter,
        }

    def get_status(
        self,
        *,
        channel: int = 0,
        plug_id: Optional[str] = None,
        include_emeter: bool = True,
    ) -> SmartPlugStatusSnapshot:
        """Queries `get_sysinfo` (and `emeter.get_realtime` when supported)."""
        sysinfo = self.get_sysinfo()

        is_on: Optional[bool] = None
        children = sysinfo.get("children")
        if isinstance(children, list) and channel < len(children) and isinstance(children[channel], dict):
            child = children[channel]
            if "state" in child:
                is_on = bool(int(child["state"]))
        elif "relay_state" in sysinfo:
            is_on = bool(int(sysinfo["relay_state"]))

        power_watts: Optional[float] = None
        voltage: Optional[float] = None
        current_amps: Optional[float] = None
        total_energy_wh: Optional[float] = None
        emeter_raw: Optional[dict[str, Any]] = None

        features = str(sysinfo.get("feature", "")).upper()
        supports_emeter = "ENE" in features or not features
        if include_emeter and supports_emeter:
            try:
                emeter = self.get_realtime_emeter()
                power_watts = emeter["power_watts"]
                voltage = emeter["voltage"]
                current_amps = emeter["current_amps"]
                total_energy_wh = emeter["total_energy_wh"]
                emeter_raw = emeter["raw"]
            except SmartPlugProtocolError:
                # Devices without energy monitoring return non-zero err_code; degrade gracefully
                pass

        raw_combined: dict[str, Any] = {"sysinfo": sysinfo}
        if emeter_raw is not None:
            raw_combined["emeter"] = emeter_raw

        return SmartPlugStatusSnapshot(
            plug_id=plug_id,
            provider="kasa",
            host=self.host,
            channel=channel,
            online=True,
            is_on=is_on,
            power_watts=power_watts,
            voltage=voltage,
            current_amps=current_amps,
            total_energy_wh=total_energy_wh,
            device_model=str(sysinfo.get("model")) if sysinfo.get("model") else None,
            device_alias=str(sysinfo.get("alias")) if sysinfo.get("alias") else None,
            firmware=str(sysinfo.get("sw_ver")) if sysinfo.get("sw_ver") else None,
            raw=raw_combined,
        )

    def send_command(
        self,
        action: SmartPlugAction,
        *,
        channel: int = 0,
        plug_id: Optional[str] = None,
    ) -> SmartPlugStatusSnapshot:
        """Sends an on/off/toggle command to the Kasa plug."""
        if action not in ("on", "off", "toggle"):
            raise ValueError(f"Unsupported Kasa action '{action}'. Use 'on', 'off', or 'toggle'.")

        if action == "toggle":
            current = self.get_status(channel=channel, plug_id=plug_id, include_emeter=False)
            target_on = not bool(current.is_on)
        else:
            target_on = action == "on"

        result = self.set_relay_state(target_on)
        return SmartPlugStatusSnapshot(
            plug_id=plug_id,
            provider="kasa",
            host=self.host,
            channel=channel,
            online=True,
            is_on=target_on,
            raw={"set_relay_state": result},
        )

    def turn_on(self, channel: int = 0, *, plug_id: Optional[str] = None) -> SmartPlugStatusSnapshot:
        return self.send_command("on", channel=channel, plug_id=plug_id)

    def turn_off(self, channel: int = 0, *, plug_id: Optional[str] = None) -> SmartPlugStatusSnapshot:
        return self.send_command("off", channel=channel, plug_id=plug_id)

    def toggle(self, channel: int = 0, *, plug_id: Optional[str] = None) -> SmartPlugStatusSnapshot:
        return self.send_command("toggle", channel=channel, plug_id=plug_id)


# ---------------------------------------------------------------------------
# F20.3: Local Network Smart Plug Discovery Scanner
# ---------------------------------------------------------------------------


class SmartPlugDiscoveryScanner:
    """Safe bounded LAN scanner probing Shelly HTTP and Kasa TCP endpoints (F20.3)."""

    MAX_ALLOWED_HOSTS = 256
    MAX_ALLOWED_CONCURRENCY = 64

    def __init__(
        self,
        *,
        shelly_transport: Optional[ShellyHttpTransport] = None,
        kasa_transport: Optional[KasaTcpTransport] = None,
        shelly_probe: Optional[PlugProbeFn] = None,
        kasa_probe: Optional[PlugProbeFn] = None,
        max_hosts: int = MAX_ALLOWED_HOSTS,
    ) -> None:
        if max_hosts < 1 or max_hosts > self.MAX_ALLOWED_HOSTS:
            raise ValueError(f"max_hosts must be between 1 and {self.MAX_ALLOWED_HOSTS}.")
        self._shelly_transport = shelly_transport
        self._kasa_transport = kasa_transport
        self._shelly_probe = shelly_probe
        self._kasa_probe = kasa_probe
        self.max_hosts = max_hosts

    def resolve_targets(
        self,
        *,
        cidr: Optional[str] = None,
        hosts: Optional[list[str]] = None,
    ) -> list[str]:
        """Validates private network constraints and expands CIDR/hosts into bounded IP list."""
        resolved: list[str] = []
        seen: set[str] = set()

        if cidr:
            try:
                network = ipaddress.ip_network(cidr.strip(), strict=False)
            except ValueError as exc:
                raise ValueError(f"Invalid CIDR '{cidr}': {exc}") from exc

            if not (network.is_private or network.is_loopback or network.is_link_local):
                raise ValueError(
                    f"Refusing to scan non-private CIDR '{network}'. "
                    "Only private/LAN, loopback, or link-local networks are allowed."
                )

            if network.num_addresses == 1:
                candidates = [network.network_address]
            else:
                # For /31 or /32 include all; for larger subnets use usable hosts
                candidates = list(network.hosts()) if network.prefixlen < (network.max_prefixlen - 1) else list(network)

            if len(candidates) > self.max_hosts:
                raise ValueError(
                    f"CIDR '{network}' expands to {len(candidates)} hosts, exceeding max_hosts={self.max_hosts}."
                )

            for ip_obj in candidates:
                ip_str = _validate_private_ip(str(ip_obj))
                if ip_str not in seen:
                    seen.add(ip_str)
                    resolved.append(ip_str)

        if hosts:
            for h in hosts:
                ip_str = _validate_private_ip(h)
                if ip_str not in seen:
                    seen.add(ip_str)
                    resolved.append(ip_str)
                if len(resolved) > self.max_hosts:
                    raise ValueError(
                        f"Total scan target count exceeds max_hosts={self.max_hosts}."
                    )

        if not resolved:
            raise ValueError("Provide a valid private 'cidr' or non-empty 'hosts' list to scan.")

        return resolved

    def _default_probe_shelly(self, host: str, timeout: float) -> Optional[DiscoveredSmartPlug]:
        t0 = time.monotonic()
        # Try Shelly Gen2 first via /rpc/Shelly.GetDeviceInfo
        gen2_client = ShellyPlugClient(
            host,
            generation="gen2",
            timeout_seconds=timeout,
            transport=self._shelly_transport,
        )
        try:
            info = gen2_client.get_device_info()
            if isinstance(info, dict) and ("gen" in info or "model" in info or "app" in info):
                latency_ms = round((time.monotonic() - t0) * 1000.0, 2)
                return DiscoveredSmartPlug(
                    provider="shelly",
                    host=host,
                    port=gen2_client.port,
                    protocol_version="gen2",
                    name=info.get("name") or info.get("id") or f"Shelly {host}",
                    model=info.get("model") or info.get("app"),
                    mac=info.get("mac"),
                    latency_ms=latency_ms,
                    raw=info,
                )
        except SmartPlugError:
            pass

        # Fallback to Shelly Gen1 via /shelly
        gen1_client = ShellyPlugClient(
            host,
            generation="gen1",
            timeout_seconds=timeout,
            transport=self._shelly_transport,
        )
        try:
            info = gen1_client.get_device_info()
            if isinstance(info, dict) and ("type" in info or "mac" in info):
                latency_ms = round((time.monotonic() - t0) * 1000.0, 2)
                return DiscoveredSmartPlug(
                    provider="shelly",
                    host=host,
                    port=gen1_client.port,
                    protocol_version="gen1",
                    name=info.get("name") or info.get("type") or f"Shelly {host}",
                    model=info.get("type"),
                    mac=info.get("mac"),
                    latency_ms=latency_ms,
                    raw=info,
                )
        except SmartPlugError:
            pass

        return None

    def _default_probe_kasa(self, host: str, timeout: float) -> Optional[DiscoveredSmartPlug]:
        t0 = time.monotonic()
        client = KasaPlugClient(
            host,
            port=KASA_DEFAULT_PORT,
            timeout_seconds=timeout,
            transport=self._kasa_transport,
        )
        try:
            sysinfo = client.get_sysinfo()
        except SmartPlugError:
            return None

        if not isinstance(sysinfo, dict) or not (
            "model" in sysinfo or "alias" in sysinfo or "relay_state" in sysinfo
        ):
            return None

        latency_ms = round((time.monotonic() - t0) * 1000.0, 2)
        return DiscoveredSmartPlug(
            provider="kasa",
            host=host,
            port=KASA_DEFAULT_PORT,
            protocol_version="local_tcp",
            name=sysinfo.get("alias") or sysinfo.get("dev_name") or f"Kasa {host}",
            model=sysinfo.get("model"),
            mac=sysinfo.get("mac") or sysinfo.get("mic_mac"),
            is_on=bool(int(sysinfo["relay_state"])) if "relay_state" in sysinfo else None,
            latency_ms=latency_ms,
            raw=sysinfo,
        )

    def _probe_host(
        self,
        host: str,
        providers: list[SmartPlugProvider],
        timeout_seconds: float,
    ) -> list[DiscoveredSmartPlug]:
        found: list[DiscoveredSmartPlug] = []
        if "shelly" in providers:
            probe_fn = self._shelly_probe or self._default_probe_shelly
            try:
                res = probe_fn(host, timeout_seconds)
                if res is not None:
                    found.append(res)
            except Exception:
                pass

        if "kasa" in providers:
            probe_fn = self._kasa_probe or self._default_probe_kasa
            try:
                res = probe_fn(host, timeout_seconds)
                if res is not None:
                    found.append(res)
            except Exception:
                pass

        return found

    def scan(
        self,
        *,
        cidr: Optional[str] = None,
        hosts: Optional[list[str]] = None,
        providers: Optional[list[SmartPlugProvider]] = None,
        timeout_seconds: float = 1.5,
        max_concurrency: int = 16,
    ) -> DiscoveryScanResult:
        """Scans private targets concurrently with strict bounds and timeout guards."""
        if not (0.05 <= timeout_seconds <= 15.0):
            raise ValueError("timeout_seconds must be between 0.05 and 15.0 seconds.")
        if not (1 <= max_concurrency <= self.MAX_ALLOWED_CONCURRENCY):
            raise ValueError(
                f"max_concurrency must be between 1 and {self.MAX_ALLOWED_CONCURRENCY}."
            )

        active_providers: list[SmartPlugProvider] = list(providers or ["shelly", "kasa"])
        if not active_providers:
            raise ValueError("At least one provider ('shelly' or 'kasa') must be specified.")
        for p in active_providers:
            if p not in ("shelly", "kasa"):
                raise ValueError(f"Unsupported discovery provider '{p}'.")

        targets = self.resolve_targets(cidr=cidr, hosts=hosts)
        worker_count = min(max_concurrency, len(targets))

        t_start = time.monotonic()
        discovered: list[DiscoveredSmartPlug] = []
        with ThreadPoolExecutor(max_workers=worker_count) as pool:
            futures = {
                pool.submit(self._probe_host, host, active_providers, timeout_seconds): host
                for host in targets
            }
            for fut in as_completed(futures):
                try:
                    discovered.extend(fut.result())
                except Exception:
                    continue

        # Stable deterministic ordering by (host, provider)
        discovered.sort(key=lambda item: (item.host, item.provider))
        duration_ms = round((time.monotonic() - t_start) * 1000.0, 2)

        return DiscoveryScanResult(
            targets_scanned=len(targets),
            providers=active_providers,
            discovered=discovered,
            duration_ms=duration_ms,
        )


# ---------------------------------------------------------------------------
# F20.5: Custom Smart Plug Category Rule Parser
# ---------------------------------------------------------------------------


SUPPORTED_SMART_PLUG_CONDITIONS = {
    "smart_plug.power_gt",
    "smart_plug.power_gte",
    "smart_plug.power_lt",
    "smart_plug.power_lte",
    "smart_plug.state_is",
    "smart_plug.online_is",
    "smart_plug.energy_gt",
}

SUPPORTED_SMART_PLUG_ACTIONS: dict[str, SmartPlugAction] = {
    "smart_plug.turn_on": "on",
    "smart_plug.turn_off": "off",
    "smart_plug.toggle": "toggle",
}


class SmartPlugConditionClause(BaseModel):
    """Single validated smart plug condition clause."""

    type: str
    operator: str
    field: str
    value: float | str | bool


class SmartPlugRuleTarget(BaseModel):
    """Target selector (category, room, and/or explicit plug IDs)."""

    category: Optional[str] = Field(default=None)
    room: Optional[str] = Field(default=None)
    plug_ids: list[str] = Field(default_factory=list)


class ParsedSmartPlugRule(BaseModel):
    """Normalized smart plug rule ready for Nova's automation engine (F20.5)."""

    id: Optional[str] = Field(default=None)
    name: str
    enabled: bool = Field(default=True)
    target: SmartPlugRuleTarget
    match: RuleMatchMode = Field(default="all")
    clauses: list[SmartPlugConditionClause]
    action_type: str
    command: SmartPlugAction
    cooldown_seconds: int = Field(default=60, ge=0, le=86400)
    normalized_condition: dict[str, Any]
    normalized_action: dict[str, Any]
    raw_rule: str
    created_at: Optional[str] = Field(default=None)
    updated_at: Optional[str] = Field(default=None)

    def matches_telemetry(self, event: dict[str, Any]) -> bool:
        """Evaluates this normalized rule against a smart plug telemetry/state event dict."""
        # Check targeting filters when present on the event
        if self.target.category and self.target.category != "all":
            ev_cat = str(event.get("category") or "").strip().lower()
            if ev_cat != self.target.category:
                return False
        if self.target.room:
            ev_room = str(event.get("room") or "").strip().lower()
            if ev_room != self.target.room:
                return False
        if self.target.plug_ids:
            ev_plug_id = event.get("plug_id") or event.get("id")
            if ev_plug_id not in self.target.plug_ids:
                return False

        results: list[bool] = []
        for clause in self.clauses:
            val = event.get(clause.field)
            if clause.field == "state" and val is None and "is_on" in event:
                val = "on" if event["is_on"] else "off"

            if val is None:
                results.append(False)
                continue

            if clause.operator == ">":
                results.append(float(val) > float(clause.value))
            elif clause.operator == ">=":
                results.append(float(val) >= float(clause.value))
            elif clause.operator == "<":
                results.append(float(val) < float(clause.value))
            elif clause.operator == "<=":
                results.append(float(val) <= float(clause.value))
            elif clause.operator == "==":
                if isinstance(clause.value, bool):
                    results.append(bool(val) is clause.value)
                else:
                    results.append(str(val).strip().lower() == str(clause.value).strip().lower())
            else:
                results.append(False)

        if not results:
            return False
        return all(results) if self.match == "all" else any(results)


class SmartPlugRuleParser:
    """Parses & validates custom smart plug category rules into Nova automation format (F20.5)."""

    @staticmethod
    def _parse_bool_token(value: Any, field_name: str) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)) and value in (0, 1):
            return bool(value)
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("true", "1", "yes", "online"):
                return True
            if low in ("false", "0", "no", "offline"):
                return False
        raise SmartPlugRuleParseError(f"Invalid boolean value '{value}' for {field_name}.")

    @staticmethod
    def _parse_state_token(value: Any) -> str:
        if isinstance(value, bool):
            return "on" if value else "off"
        if isinstance(value, int) and value in (0, 1):
            return "on" if value == 1 else "off"
        if isinstance(value, str):
            low = value.strip().lower()
            if low in ("on", "true", "1"):
                return "on"
            if low in ("off", "false", "0"):
                return "off"
        raise SmartPlugRuleParseError(
            f"Invalid smart_plug.state_is value '{value}'. Expected 'on' or 'off'."
        )

    @staticmethod
    def _parse_non_negative_float(value: Any, condition_type: str) -> float:
        if isinstance(value, bool):
            raise SmartPlugRuleParseError(
                f"Condition '{condition_type}' requires a numeric threshold, got boolean."
            )
        try:
            num = float(value)
        except (TypeError, ValueError) as exc:
            raise SmartPlugRuleParseError(
                f"Condition '{condition_type}' requires a numeric threshold, got '{value}'."
            ) from exc
        if not math.isfinite(num) or num < 0.0:
            raise SmartPlugRuleParseError(
                f"Condition '{condition_type}' threshold must be a finite number >= 0, got {value}."
            )
        return num

    def _normalize_clause(self, raw_clause: dict[str, Any]) -> SmartPlugConditionClause:
        ctype = str(raw_clause.get("type") or "").strip().lower()
        if ctype not in SUPPORTED_SMART_PLUG_CONDITIONS:
            raise SmartPlugRuleParseError(
                f"Unsupported smart plug condition '{ctype}'. "
                f"Supported: {sorted(SUPPORTED_SMART_PLUG_CONDITIONS)}."
            )

        if ctype in (
            "smart_plug.power_gt",
            "smart_plug.power_gte",
            "smart_plug.power_lt",
            "smart_plug.power_lte",
        ):
            raw_val = (
                raw_clause.get("threshold_watts")
                if "threshold_watts" in raw_clause
                else raw_clause.get("threshold", raw_clause.get("watts", raw_clause.get("value")))
            )
            if raw_val is None:
                raise SmartPlugRuleParseError(
                    f"Condition '{ctype}' requires 'threshold_watts' (or 'threshold'/'value')."
                )
            num = self._parse_non_negative_float(raw_val, ctype)
            op_map = {
                "smart_plug.power_gt": ">",
                "smart_plug.power_gte": ">=",
                "smart_plug.power_lt": "<",
                "smart_plug.power_lte": "<=",
            }
            return SmartPlugConditionClause(
                type=ctype,
                operator=op_map[ctype],
                field="power_watts",
                value=num,
            )

        if ctype == "smart_plug.energy_gt":
            raw_val = (
                raw_clause.get("threshold_wh")
                if "threshold_wh" in raw_clause
                else raw_clause.get("threshold", raw_clause.get("value"))
            )
            if raw_val is None:
                raise SmartPlugRuleParseError(
                    "Condition 'smart_plug.energy_gt' requires 'threshold_wh' (or 'threshold'/'value')."
                )
            num = self._parse_non_negative_float(raw_val, ctype)
            return SmartPlugConditionClause(
                type=ctype,
                operator=">",
                field="total_energy_wh",
                value=num,
            )

        if ctype == "smart_plug.state_is":
            raw_val = raw_clause.get("state", raw_clause.get("value"))
            if raw_val is None:
                raise SmartPlugRuleParseError(
                    "Condition 'smart_plug.state_is' requires 'state' (or 'value') set to 'on' or 'off'."
                )
            state_val = self._parse_state_token(raw_val)
            return SmartPlugConditionClause(
                type=ctype,
                operator="==",
                field="state",
                value=state_val,
            )

        # smart_plug.online_is
        raw_val = raw_clause.get("online", raw_clause.get("value"))
        if raw_val is None:
            raise SmartPlugRuleParseError(
                "Condition 'smart_plug.online_is' requires boolean 'online' (or 'value')."
            )
        bool_val = self._parse_bool_token(raw_val, ctype)
        return SmartPlugConditionClause(
            type=ctype,
            operator="==",
            field="online",
            value=bool_val,
        )

    def _normalize_target(self, raw_target: dict[str, Any]) -> SmartPlugRuleTarget:
        category_raw = raw_target.get("category")
        room_raw = raw_target.get("room")
        plug_ids_raw = raw_target.get("plug_ids")
        single_plug_id = raw_target.get("plug_id")

        category: Optional[str] = None
        if category_raw is not None and str(category_raw).strip():
            cat_str = str(category_raw).strip().lower()
            category = "all" if cat_str == "*" else _normalize_identifier(cat_str, "category")

        room: Optional[str] = None
        if room_raw is not None and str(room_raw).strip():
            room = _normalize_identifier(str(room_raw), "room")

        plug_ids: list[str] = []
        if single_plug_id is not None and str(single_plug_id).strip():
            plug_ids.append(str(single_plug_id).strip())
        if plug_ids_raw is not None:
            if not isinstance(plug_ids_raw, list):
                raise SmartPlugRuleParseError("'plug_ids' must be a list of plug ID strings.")
            for pid in plug_ids_raw:
                cleaned = str(pid).strip()
                if not cleaned:
                    raise SmartPlugRuleParseError("plug_ids cannot contain empty strings.")
                if cleaned not in plug_ids:
                    plug_ids.append(cleaned)

        if not category and not room and not plug_ids:
            raise SmartPlugRuleParseError(
                "Rule target must specify at least one of 'category', 'room', or 'plug_ids'."
            )

        return SmartPlugRuleTarget(category=category, room=room, plug_ids=plug_ids)

    def _parse_dsl_expression(self, expression: str, *, name: Optional[str] = None) -> dict[str, Any]:
        """Parses compact DSL string:
        `WHEN [category:<cat>] [room:<room>] [plug:<id>] <cond> <val> [AND|OR <cond> <val>...] THEN <action>`
        """
        text = expression.strip()
        if not text:
            raise SmartPlugRuleParseError("Rule expression cannot be empty.")

        tokens = shlex.split(text)
        if not tokens:
            raise SmartPlugRuleParseError("Rule expression cannot be empty.")

        upper_tokens = [t.upper() for t in tokens]
        if upper_tokens[0] in ("WHEN", "IF"):
            tokens = tokens[1:]
            upper_tokens = upper_tokens[1:]

        if "THEN" not in upper_tokens:
            raise SmartPlugRuleParseError(
                "DSL rule expression must contain 'THEN <action>' (e.g. 'THEN smart_plug.turn_off')."
            )
        then_idx = upper_tokens.index("THEN")
        lhs_tokens = tokens[:then_idx]
        rhs_tokens = tokens[then_idx + 1 :]

        if not lhs_tokens:
            raise SmartPlugRuleParseError("DSL rule expression is missing conditions before 'THEN'.")
        if not rhs_tokens:
            raise SmartPlugRuleParseError("DSL rule expression is missing action after 'THEN'.")

        target: dict[str, Any] = {"plug_ids": []}
        cond_tokens: list[str] = []
        for tok in lhs_tokens:
            low = tok.lower()
            if low.startswith("category:"):
                target["category"] = tok.split(":", 1)[1]
            elif low.startswith("room:"):
                target["room"] = tok.split(":", 1)[1]
            elif low.startswith("plug:"):
                target["plug_ids"].append(tok.split(":", 1)[1])
            else:
                cond_tokens.append(tok)

        if not cond_tokens:
            raise SmartPlugRuleParseError("DSL rule expression must include at least one condition.")

        # Split condition tokens on AND / OR (single uniform combinator per rule)
        combinators: set[str] = set()
        groups: list[list[str]] = [[]]
        for tok in cond_tokens:
            if tok.upper() in ("AND", "OR"):
                combinators.add(tok.upper())
                groups.append([])
            else:
                groups[-1].append(tok)

        if len(combinators) > 1:
            raise SmartPlugRuleParseError(
                "Cannot mix 'AND' and 'OR' in a single flat rule expression."
            )
        match_mode: RuleMatchMode = "any" if combinators == {"OR"} else "all"

        clauses: list[dict[str, Any]] = []
        for grp in groups:
            if len(grp) != 2:
                raise SmartPlugRuleParseError(
                    f"Malformed condition clause '{' '.join(grp)}': expected '<condition_type> <value>'."
                )
            clauses.append({"type": grp[0], "value": grp[1]})

        # Parse action and optional action target overrides after THEN
        action_type = rhs_tokens[0]
        cooldown_seconds = 60
        for extra in rhs_tokens[1:]:
            low = extra.lower()
            if low.startswith("cooldown:"):
                try:
                    cooldown_seconds = int(extra.split(":", 1)[1])
                except ValueError as exc:
                    raise SmartPlugRuleParseError(f"Invalid cooldown specifier '{extra}'.") from exc
            elif low.startswith("category:"):
                target["category"] = extra.split(":", 1)[1]
            elif low.startswith("room:"):
                target["room"] = extra.split(":", 1)[1]
            elif low.startswith("plug:"):
                target["plug_ids"].append(extra.split(":", 1)[1])
            else:
                raise SmartPlugRuleParseError(f"Unexpected token '{extra}' after THEN action.")

        return {
            "name": name or f"SmartPlug Rule ({action_type})",
            "target": target,
            "match": match_mode,
            "conditions": clauses,
            "action": {"type": action_type},
            "cooldown_seconds": cooldown_seconds,
        }

    def parse(
        self,
        rule_input: str | dict[str, Any],
        *,
        name: Optional[str] = None,
        enabled: bool = True,
    ) -> ParsedSmartPlugRule:
        """Parses a smart plug rule (dict or DSL string) into a validated, normalized rule."""
        if isinstance(rule_input, str):
            raw_rule = rule_input.strip()
            spec = self._parse_dsl_expression(raw_rule, name=name)
        elif isinstance(rule_input, dict):
            raw_rule = json.dumps(rule_input, sort_keys=True)
            spec = dict(rule_input)
        else:
            raise SmartPlugRuleParseError("Rule input must be a dict or DSL string.")

        rule_name = str(name or spec.get("name") or "").strip()
        if not rule_name:
            raise SmartPlugRuleParseError("Rule 'name' is required and cannot be empty.")

        rule_enabled = bool(spec.get("enabled", enabled))

        # Target extraction (supports nested `target` dict or top-level keys)
        target_dict: dict[str, Any] = {}
        if isinstance(spec.get("target"), dict):
            target_dict.update(spec["target"])
        for key in ("category", "room", "plug_id", "plug_ids"):
            if key in spec and key not in target_dict:
                target_dict[key] = spec[key]
        try:
            target = self._normalize_target(target_dict)
        except ValueError as exc:
            raise SmartPlugRuleParseError(str(exc)) from exc

        # Match mode
        match_raw = str(spec.get("match", "all")).strip().lower()
        if match_raw not in ("all", "any"):
            raise SmartPlugRuleParseError(f"Rule 'match' must be 'all' or 'any', got '{match_raw}'.")
        match_mode: RuleMatchMode = match_raw  # type: ignore[assignment]

        # Conditions (accepts `conditions: [...]` or singular `condition: {...}`)
        raw_conditions: list[Any] = []
        if "conditions" in spec and spec["conditions"] is not None:
            if not isinstance(spec["conditions"], list):
                raise SmartPlugRuleParseError("'conditions' must be a list of condition objects.")
            raw_conditions.extend(spec["conditions"])
        if "condition" in spec and spec["condition"] is not None:
            if not isinstance(spec["condition"], dict):
                raise SmartPlugRuleParseError("'condition' must be a condition object.")
            raw_conditions.append(spec["condition"])

        if not raw_conditions:
            raise SmartPlugRuleParseError("Rule must define at least one condition.")

        clauses: list[SmartPlugConditionClause] = []
        for idx, rc in enumerate(raw_conditions):
            if not isinstance(rc, dict):
                raise SmartPlugRuleParseError(f"Condition at index {idx} must be a JSON object.")
            clauses.append(self._normalize_clause(rc))

        # Action (accepts `action: {"type": "smart_plug.turn_off"}` or `action: "smart_plug.turn_off"`)
        action_spec = spec.get("action")
        if isinstance(action_spec, str):
            action_type = action_spec.strip().lower()
            action_extra_params: dict[str, Any] = {}
        elif isinstance(action_spec, dict):
            action_type = str(action_spec.get("type") or "").strip().lower()
            action_extra_params = dict(action_spec.get("params") or {})
        else:
            raise SmartPlugRuleParseError("Rule must specify an 'action' string or object.")

        if action_type not in SUPPORTED_SMART_PLUG_ACTIONS:
            raise SmartPlugRuleParseError(
                f"Unsupported smart plug action '{action_type}'. "
                f"Supported: {sorted(SUPPORTED_SMART_PLUG_ACTIONS.keys())}."
            )
        command = SUPPORTED_SMART_PLUG_ACTIONS[action_type]

        # Cooldown validation
        cooldown_raw = spec.get("cooldown_seconds", 60)
        if isinstance(cooldown_raw, bool):
            raise SmartPlugRuleParseError("'cooldown_seconds' must be an integer >= 0.")
        try:
            cooldown_seconds = int(cooldown_raw)
        except (TypeError, ValueError) as exc:
            raise SmartPlugRuleParseError("'cooldown_seconds' must be an integer >= 0.") from exc
        if cooldown_seconds < 0 or cooldown_seconds > 86400:
            raise SmartPlugRuleParseError("'cooldown_seconds' must be between 0 and 86400.")

        # Build Nova AutomationEngine-ready condition & action_template
        normalized_condition: dict[str, Any] = {
            "type": "smart_plug",
            "match": match_mode,
            "target": target.model_dump(mode="json"),
            "clauses": [c.model_dump(mode="json") for c in clauses],
        }

        action_params: dict[str, Any] = {
            "command": command,
            "target_category": target.category,
            "target_room": target.room,
            "plug_ids": list(target.plug_ids),
            **action_extra_params,
        }
        normalized_action: dict[str, Any] = {
            "action_type": action_type,
            "category": "smart_home",
            "params": action_params,
            "source_device_id": str(spec.get("source_device_id") or "nova_core"),
        }

        return ParsedSmartPlugRule(
            name=rule_name,
            enabled=rule_enabled,
            target=target,
            match=match_mode,
            clauses=clauses,
            action_type=action_type,
            command=command,
            cooldown_seconds=cooldown_seconds,
            normalized_condition=normalized_condition,
            normalized_action=normalized_action,
            raw_rule=raw_rule,
        )


# ---------------------------------------------------------------------------
# F20.4: SQLite / DatabaseStore Persistence Wrapper
# ---------------------------------------------------------------------------


class SmartPlugStorage:
    """Self-contained SQLite/DatabaseStore persistence for smart plugs, snapshots & rules."""

    def __init__(self, store_or_path: Any) -> None:
        self._store = store_or_path if hasattr(store_or_path, "get_connection") else None
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
        """Ensures migration 0011 tables exist (idempotent via CREATE IF NOT EXISTS)."""
        migration_sql = _MIGRATION_0011_PATH.read_text(encoding="utf-8")
        with self._connection() as conn:
            conn.executescript(migration_sql)

    # ---- Plugs CRUD ----

    @staticmethod
    def _row_to_plug(row: Any) -> SmartPlugDevice:
        data = dict(row)
        return SmartPlugDevice(
            id=data["id"],
            name=data["name"],
            provider=data["provider"],
            host=data["host"],
            port=data["port"],
            protocol_version=data["protocol_version"],
            channel=int(data["channel"]),
            category=data["category"],
            room=data["room"],
            auth_username=data["auth_username"],
            auth_password=data["auth_password"],
            auth_token=data["auth_token"],
            use_https=bool(data["use_https"]),
            timeout_seconds=float(data["timeout_seconds"]),
            enabled=bool(data["enabled"]),
            is_on=bool(data["is_on"]) if data["is_on"] is not None else None,
            power_watts=data["power_watts"],
            voltage=data["voltage"],
            current_amps=data["current_amps"],
            total_energy_wh=data["total_energy_wh"],
            online=bool(data["online"]),
            last_synced_at=data["last_synced_at"],
            last_error=data["last_error"],
            metadata=json.loads(data["metadata"] or "{}"),
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def create_plug(self, plug: SmartPlugDevice) -> SmartPlugDevice:
        now = _utc_now_iso()
        plug_id = plug.id or f"plug_{os.urandom(8).hex()}"
        stored = plug.model_copy(
            update={"id": plug_id, "created_at": now, "updated_at": now}
        )
        sql = """
        INSERT INTO smart_plugs (
            id, name, provider, host, port, protocol_version, channel,
            category, room, auth_username, auth_password, auth_token,
            use_https, timeout_seconds, enabled, is_on, power_watts,
            voltage, current_amps, total_energy_wh, online, last_synced_at,
            last_error, metadata, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    stored.id,
                    stored.name,
                    stored.provider,
                    stored.host,
                    stored.port,
                    stored.protocol_version,
                    stored.channel,
                    stored.category,
                    stored.room,
                    stored.auth_username,
                    stored.auth_password,
                    stored.auth_token,
                    1 if stored.use_https else 0,
                    stored.timeout_seconds,
                    1 if stored.enabled else 0,
                    None if stored.is_on is None else (1 if stored.is_on else 0),
                    stored.power_watts,
                    stored.voltage,
                    stored.current_amps,
                    stored.total_energy_wh,
                    1 if stored.online else 0,
                    stored.last_synced_at,
                    stored.last_error,
                    json.dumps(stored.metadata),
                    stored.created_at,
                    stored.updated_at,
                ),
            )
        return stored

    def get_plug(self, plug_id: str) -> Optional[SmartPlugDevice]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM smart_plugs WHERE id = ?",
                (plug_id,),
            ).fetchone()
        return self._row_to_plug(row) if row is not None else None

    def find_plug_by_endpoint(
        self,
        provider: SmartPlugProvider,
        host: str,
        channel: int = 0,
    ) -> Optional[SmartPlugDevice]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM smart_plugs WHERE provider = ? AND host = ? AND channel = ?",
                (provider, host, channel),
            ).fetchone()
        return self._row_to_plug(row) if row is not None else None

    def list_plugs(
        self,
        *,
        provider: Optional[SmartPlugProvider] = None,
        category: Optional[str] = None,
        room: Optional[str] = None,
        enabled_only: bool = False,
    ) -> list[SmartPlugDevice]:
        clauses: list[str] = []
        params: list[Any] = []
        if provider:
            clauses.append("provider = ?")
            params.append(provider)
        if category:
            clauses.append("category = ?")
            params.append(category.strip().lower())
        if room:
            clauses.append("room = ?")
            params.append(room.strip().lower())
        if enabled_only:
            clauses.append("enabled = 1")

        sql = "SELECT * FROM smart_plugs"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"

        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_plug(r) for r in rows]

    def update_plug_status(
        self,
        plug_id: str,
        snapshot: SmartPlugStatusSnapshot,
    ) -> Optional[SmartPlugDevice]:
        now = _utc_now_iso()
        sql = """
        UPDATE smart_plugs
        SET
            is_on = COALESCE(?, is_on),
            power_watts = COALESCE(?, power_watts),
            voltage = COALESCE(?, voltage),
            current_amps = COALESCE(?, current_amps),
            total_energy_wh = COALESCE(?, total_energy_wh),
            online = ?,
            last_synced_at = ?,
            last_error = ?,
            updated_at = ?
        WHERE id = ?
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    None if snapshot.is_on is None else (1 if snapshot.is_on else 0),
                    snapshot.power_watts,
                    snapshot.voltage,
                    snapshot.current_amps,
                    snapshot.total_energy_wh,
                    1 if snapshot.online else 0,
                    snapshot.synced_at,
                    snapshot.error,
                    now,
                    plug_id,
                ),
            )
            conn.execute(
                """
                INSERT INTO smart_plug_status_history (
                    plug_id, online, is_on, power_watts, voltage,
                    current_amps, total_energy_wh, temperature_c, raw_payload, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plug_id,
                    1 if snapshot.online else 0,
                    None if snapshot.is_on is None else (1 if snapshot.is_on else 0),
                    snapshot.power_watts,
                    snapshot.voltage,
                    snapshot.current_amps,
                    snapshot.total_energy_wh,
                    snapshot.temperature_c,
                    json.dumps(snapshot.raw),
                    snapshot.synced_at,
                ),
            )
        return self.get_plug(plug_id)

    def get_status_history(
        self,
        plug_id: str,
        *,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit), 500))
        with self._connection() as conn:
            rows = conn.execute(
                """
                SELECT * FROM smart_plug_status_history
                WHERE plug_id = ?
                ORDER BY synced_at DESC, id DESC
                LIMIT ?
                """,
                (plug_id, safe_limit),
            ).fetchall()
        results: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["online"] = bool(item["online"])
            item["is_on"] = bool(item["is_on"]) if item["is_on"] is not None else None
            item["raw_payload"] = json.loads(item["raw_payload"] or "{}")
            results.append(item)
        return results

    def delete_plug(self, plug_id: str) -> bool:
        with self._connection() as conn:
            conn.execute(
                "DELETE FROM smart_plug_status_history WHERE plug_id = ?",
                (plug_id,),
            )
            cursor = conn.execute(
                "DELETE FROM smart_plugs WHERE id = ?",
                (plug_id,),
            )
            return cursor.rowcount > 0

    # ---- Parsed Smart Plug Rules CRUD ----

    @staticmethod
    def _row_to_rule(row: Any) -> ParsedSmartPlugRule:
        data = dict(row)
        norm_cond = json.loads(data["normalized_condition"])
        norm_action = json.loads(data["normalized_action"])
        clauses = [SmartPlugConditionClause(**c) for c in norm_cond.get("clauses", [])]
        target = SmartPlugRuleTarget(
            category=data["category"],
            room=data["room"],
            plug_ids=json.loads(data["plug_ids"] or "[]"),
        )
        action_type = str(norm_action.get("action_type", "smart_plug.turn_off"))
        command = SUPPORTED_SMART_PLUG_ACTIONS.get(action_type, "off")
        return ParsedSmartPlugRule(
            id=data["id"],
            name=data["name"],
            enabled=bool(data["enabled"]),
            target=target,
            match=norm_cond.get("match", "all"),
            clauses=clauses,
            action_type=action_type,
            command=command,
            cooldown_seconds=int(data["cooldown_seconds"]),
            normalized_condition=norm_cond,
            normalized_action=norm_action,
            raw_rule=data["raw_rule"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def save_rule(self, rule: ParsedSmartPlugRule) -> ParsedSmartPlugRule:
        now = _utc_now_iso()
        rule_id = rule.id or f"sprule_{os.urandom(8).hex()}"
        stored = rule.model_copy(
            update={"id": rule_id, "created_at": now, "updated_at": now}
        )
        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO smart_plug_rules (
                    id, name, enabled, category, room, plug_ids,
                    raw_rule, normalized_condition, normalized_action,
                    cooldown_seconds, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored.id,
                    stored.name,
                    1 if stored.enabled else 0,
                    stored.target.category,
                    stored.target.room,
                    json.dumps(stored.target.plug_ids),
                    stored.raw_rule,
                    json.dumps(stored.normalized_condition),
                    json.dumps(stored.normalized_action),
                    stored.cooldown_seconds,
                    stored.created_at,
                    stored.updated_at,
                ),
            )
        return stored

    def list_rules(
        self,
        *,
        category: Optional[str] = None,
        room: Optional[str] = None,
        enabled_only: bool = False,
    ) -> list[ParsedSmartPlugRule]:
        clauses: list[str] = []
        params: list[Any] = []
        if category:
            clauses.append("category = ?")
            params.append(category.strip().lower())
        if room:
            clauses.append("room = ?")
            params.append(room.strip().lower())
        if enabled_only:
            clauses.append("enabled = 1")

        sql = "SELECT * FROM smart_plug_rules"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def delete_rule(self, rule_id: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM smart_plug_rules WHERE id = ?",
                (rule_id,),
            )
            return cursor.rowcount > 0


# ---------------------------------------------------------------------------
# Unified SmartPlugService Facade
# ---------------------------------------------------------------------------


class SmartPlugService:
    """Coordinates Shelly/Kasa drivers, LAN discovery, SQLite status sync, and rule parser."""

    def __init__(
        self,
        storage: SmartPlugStorage,
        *,
        shelly_transport: Optional[ShellyHttpTransport] = None,
        kasa_transport: Optional[KasaTcpTransport] = None,
        shelly_probe: Optional[PlugProbeFn] = None,
        kasa_probe: Optional[PlugProbeFn] = None,
    ) -> None:
        self.storage = storage
        self.rule_parser = SmartPlugRuleParser()
        self._shelly_transport = shelly_transport
        self._kasa_transport = kasa_transport
        self.scanner = SmartPlugDiscoveryScanner(
            shelly_transport=shelly_transport,
            kasa_transport=kasa_transport,
            shelly_probe=shelly_probe,
            kasa_probe=kasa_probe,
        )

    def _build_client(self, plug: SmartPlugDevice) -> ShellyPlugClient | KasaPlugClient:
        if plug.provider == "shelly":
            gen: ShellyGeneration = "gen2" if plug.protocol_version == "gen2" else "gen1"
            return ShellyPlugClient(
                plug.host,
                generation=gen,
                port=plug.port,
                use_https=plug.use_https,
                username=plug.auth_username,
                password=plug.auth_password,
                auth_token=plug.auth_token,
                timeout_seconds=plug.timeout_seconds,
                transport=self._shelly_transport,
            )
        return KasaPlugClient(
            plug.host,
            port=plug.port or KASA_DEFAULT_PORT,
            timeout_seconds=plug.timeout_seconds,
            transport=self._kasa_transport,
        )

    # ---- Registry CRUD ----

    def register_plug(self, plug: SmartPlugDevice) -> SmartPlugDevice:
        return self.storage.create_plug(plug)

    def get_plug(self, plug_id: str) -> Optional[SmartPlugDevice]:
        return self.storage.get_plug(plug_id)

    def list_plugs(
        self,
        *,
        provider: Optional[SmartPlugProvider] = None,
        category: Optional[str] = None,
        room: Optional[str] = None,
        enabled_only: bool = False,
    ) -> list[SmartPlugDevice]:
        return self.storage.list_plugs(
            provider=provider,
            category=category,
            room=room,
            enabled_only=enabled_only,
        )

    def delete_plug(self, plug_id: str) -> bool:
        return self.storage.delete_plug(plug_id)

    # ---- Control & Live Status Sync (F20.4) ----

    def send_power_command(
        self,
        plug_id: str,
        action: SmartPlugAction,
        *,
        channel: Optional[int] = None,
    ) -> SmartPlugCommandResult:
        plug = self.storage.get_plug(plug_id)
        if plug is None:
            raise KeyError(f"Smart plug '{plug_id}' not found.")

        effective_channel = plug.channel if channel is None else channel
        client = self._build_client(plug)
        try:
            snapshot = client.send_command(
                action,
                channel=effective_channel,
                plug_id=plug_id,
            )
        except SmartPlugError as exc:
            offline_snap = SmartPlugStatusSnapshot(
                plug_id=plug_id,
                provider=plug.provider,
                host=plug.host,
                channel=effective_channel,
                online=False,
                error=str(exc),
            )
            self.storage.update_plug_status(plug_id, offline_snap)
            raise

        self.storage.update_plug_status(plug_id, snapshot)
        return SmartPlugCommandResult(
            plug_id=plug_id,
            provider=plug.provider,
            action=action,
            channel=effective_channel,
            success=True,
            is_on=snapshot.is_on,
            snapshot=snapshot,
        )

    def sync_plug(
        self,
        plug_id: str,
        *,
        raise_on_error: bool = False,
    ) -> SmartPlugStatusSnapshot:
        plug = self.storage.get_plug(plug_id)
        if plug is None:
            raise KeyError(f"Smart plug '{plug_id}' not found.")

        client = self._build_client(plug)
        try:
            snapshot = client.get_status(channel=plug.channel, plug_id=plug_id)
        except SmartPlugError as exc:
            offline_snap = SmartPlugStatusSnapshot(
                plug_id=plug_id,
                provider=plug.provider,
                host=plug.host,
                channel=plug.channel,
                online=False,
                error=str(exc),
            )
            self.storage.update_plug_status(plug_id, offline_snap)
            if raise_on_error:
                raise
            return offline_snap

        self.storage.update_plug_status(plug_id, snapshot)
        return snapshot

    def sync_all_plugs(
        self,
        *,
        provider: Optional[SmartPlugProvider] = None,
        category: Optional[str] = None,
        room: Optional[str] = None,
    ) -> SmartPlugBatchSyncResult:
        plugs = self.storage.list_plugs(
            provider=provider,
            category=category,
            room=room,
            enabled_only=True,
        )
        snapshots: list[SmartPlugStatusSnapshot] = []
        online_count = 0
        offline_count = 0
        total_power = 0.0

        for plug in plugs:
            assert plug.id is not None
            snap = self.sync_plug(plug.id, raise_on_error=False)
            snapshots.append(snap)
            if snap.online:
                online_count += 1
                if snap.power_watts is not None:
                    total_power += snap.power_watts
            else:
                offline_count += 1

        return SmartPlugBatchSyncResult(
            total=len(plugs),
            online_count=online_count,
            offline_count=offline_count,
            total_power_watts=round(total_power, 4),
            snapshots=snapshots,
        )

    def get_status_history(self, plug_id: str, *, limit: int = 50) -> list[dict[str, Any]]:
        if self.storage.get_plug(plug_id) is None:
            raise KeyError(f"Smart plug '{plug_id}' not found.")
        return self.storage.get_status_history(plug_id, limit=limit)

    # ---- Discovery (F20.3) ----

    def discover_plugs(
        self,
        *,
        cidr: Optional[str] = None,
        hosts: Optional[list[str]] = None,
        providers: Optional[list[SmartPlugProvider]] = None,
        timeout_seconds: float = 1.5,
        max_concurrency: int = 16,
        auto_register: bool = False,
        default_category: str = "general",
        default_room: Optional[str] = None,
    ) -> DiscoveryScanResult:
        result = self.scanner.scan(
            cidr=cidr,
            hosts=hosts,
            providers=providers,
            timeout_seconds=timeout_seconds,
            max_concurrency=max_concurrency,
        )
        if auto_register and result.discovered:
            registered_ids: list[str] = []
            for item in result.discovered:
                existing = self.storage.find_plug_by_endpoint(item.provider, item.host, channel=0)
                if existing is not None and existing.id is not None:
                    registered_ids.append(existing.id)
                    continue
                created = self.storage.create_plug(
                    SmartPlugDevice(
                        name=item.name or f"{item.provider.title()} {item.host}",
                        provider=item.provider,
                        host=item.host,
                        port=item.port,
                        protocol_version=item.protocol_version,
                        category=default_category,
                        room=default_room,
                        is_on=item.is_on,
                        power_watts=item.power_watts,
                        online=True,
                        metadata={
                            "model": item.model,
                            "mac": item.mac,
                            "discovered": True,
                        },
                    )
                )
                assert created.id is not None
                registered_ids.append(created.id)
            result = result.model_copy(update={"registered_plug_ids": registered_ids})
        return result

    # ---- Rule Parsing & Persistence (F20.5) ----

    def parse_rule(
        self,
        rule_input: str | dict[str, Any],
        *,
        name: Optional[str] = None,
        enabled: bool = True,
        save: bool = False,
    ) -> ParsedSmartPlugRule:
        parsed = self.rule_parser.parse(rule_input, name=name, enabled=enabled)
        if save:
            return self.storage.save_rule(parsed)
        return parsed

    def list_rules(
        self,
        *,
        category: Optional[str] = None,
        room: Optional[str] = None,
        enabled_only: bool = False,
    ) -> list[ParsedSmartPlugRule]:
        return self.storage.list_rules(
            category=category,
            room=room,
            enabled_only=enabled_only,
        )

    def delete_rule(self, rule_id: str) -> bool:
        return self.storage.delete_rule(rule_id)
