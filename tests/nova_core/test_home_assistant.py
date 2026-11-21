"""Comprehensive offline tests for Phase 21: Home Assistant Event Adapters (F21.1–F21.5)."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nova_core.api.routes_auth import router as auth_router
from nova_core.api.routes_home_assistant import router as ha_router
from nova_core.auth import get_current_user
from nova_core.db.store import DatabaseStore
from nova_core.integrations.smart_home.home_assistant import (
    EntityStateTracker,
    HACapabilityMapper,
    HAConfig,
    HAEntityTrigger,
    HAWSConnectionState,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    HomeAssistantEventDispatcher,
    HomeAssistantService,
    HomeAssistantServiceError,
    HomeAssistantStorage,
    HomeAssistantWSManager,
    NovaAutomationEvent,
    derive_ws_url,
    mask_access_token,
)

MIGRATION_UP = (
    Path(__file__).resolve().parents[2]
    / "nova_core"
    / "db"
    / "migrations"
    / "0012_home_assistant.sql"
)
MIGRATION_DOWN = (
    Path(__file__).resolve().parents[2]
    / "nova_core"
    / "db"
    / "migrations"
    / "0012_home_assistant.rollback.sql"
)


# ---------------------------------------------------------------------------
# Offline Mock WebSocket Transport
# ---------------------------------------------------------------------------


class FakeHAWebSocket:
    """Deterministic in-memory WebSocket transport simulating Home Assistant WS API."""

    def __init__(self, incoming_frames: list[dict[str, Any]]) -> None:
        self._incoming = [json.dumps(f) for f in incoming_frames]
        self.sent: list[dict[str, Any]] = []
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        if not self._incoming:
            raise RuntimeError("FakeHAWebSocket has no more scripted frames.")
        return self._incoming.pop(0)

    async def close(self) -> None:
        self.closed = True


def _sample_entities() -> list[dict[str, Any]]:
    return [
        {
            "entity_id": "light.living_room",
            "state": "on",
            "attributes": {
                "friendly_name": "Living Room Lamp",
                "brightness": 200,
                "supported_color_modes": ["brightness", "color_temp", "rgb"],
                "rgb_color": [255, 220, 180],
            },
        },
        {
            "entity_id": "switch.desk_plug",
            "state": "off",
            "attributes": {
                "friendly_name": "Desk Smart Plug",
                "current_power_w": 0.0,
            },
        },
        {
            "entity_id": "climate.bedroom_ac",
            "state": "cool",
            "attributes": {
                "friendly_name": "Bedroom AC",
                "current_temperature": 26.5,
                "temperature": 23.0,
                "hvac_modes": ["off", "cool", "dry", "fan_only"],
                "fan_modes": ["auto", "low", "high"],
            },
        },
        {
            "entity_id": "sensor.living_room_temp",
            "state": "27.4",
            "attributes": {
                "friendly_name": "Living Room Temp",
                "device_class": "temperature",
                "unit_of_measurement": "°C",
            },
        },
        {
            "entity_id": "binary_sensor.front_door",
            "state": "off",
            "attributes": {
                "friendly_name": "Front Door Contact",
                "device_class": "door",
            },
        },
        {
            "entity_id": "cover.living_blinds",
            "state": "open",
            "attributes": {
                "friendly_name": "Living Room Blinds",
                "current_position": 80,
                "current_tilt_position": 45,
            },
        },
        {
            "entity_id": "lock.main_entry",
            "state": "locked",
            "attributes": {"friendly_name": "Main Entry Lock"},
        },
        {
            "entity_id": "media_player.studio_tv",
            "state": "playing",
            "attributes": {
                "friendly_name": "Studio TV",
                "volume_level": 0.4,
                "source_list": ["HDMI 1", "Netflix"],
            },
        },
        {
            "entity_id": "fan.office_fan",
            "state": "on",
            "attributes": {
                "friendly_name": "Office Fan",
                "percentage": 66,
                "oscillating": True,
            },
        },
    ]


# ---------------------------------------------------------------------------
# F21.1: Home Assistant WebSocket Connection Manager
# ---------------------------------------------------------------------------


def test_f21_1_ws_handshake_subscribe_ping_and_interleaved_events():
    fake_ws = FakeHAWebSocket(
        [
            {"type": "auth_required", "ha_version": "2026.11.0"},
            {"type": "auth_ok", "ha_version": "2026.11.0"},
            {"id": 1, "type": "result", "success": True, "result": None},
            # Interleaved event arrives before pong for command id=2
            {
                "id": 1,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.living_room",
                        "old_state": {"state": "off", "attributes": {}},
                        "new_state": {"state": "on", "attributes": {"brightness": 180}},
                    },
                },
            },
            {"id": 2, "type": "pong"},
            {"id": 3, "type": "result", "success": True, "result": {"context": {"id": "ctx1"}}},
            {"id": 4, "type": "result", "success": True, "result": None},
        ]
    )

    manager = HomeAssistantWSManager(
        ws_url="ws://ha.local:8123/api/websocket",
        access_token="eyJ_long_lived_token_123456",
        ws_factory=lambda _url: fake_ws,
    )

    async def scenario():
        status = await manager.connect()
        assert status.connected is True
        assert status.state == HAWSConnectionState.CONNECTED
        assert status.ha_version == "2026.11.0"
        assert status.reconnect_attempts == 0

        sub_id = await manager.subscribe_events("state_changed")
        assert sub_id == 1
        assert manager.subscriptions == {1: "state_changed"}

        pong = await manager.ping()
        assert pong["id"] == 2
        assert pong["type"] == "pong"
        assert pong["latency_ms"] >= 0.0

        # Interleaved event was buffered and can be popped cleanly
        buffered = await manager.receive_event()
        assert buffered is not None
        assert buffered["type"] == "event"
        assert buffered["event"]["data"]["entity_id"] == "light.living_room"

        svc_reply = await manager.call_service(
            "light",
            "turn_on",
            service_data={"brightness": 255},
            target={"entity_id": "light.living_room"},
        )
        assert svc_reply["id"] == 3
        assert svc_reply["success"] is True

        unsub_ok = await manager.unsubscribe_events(sub_id)
        assert unsub_ok is True
        assert manager.subscriptions == {}

        await manager.disconnect()
        assert manager.is_connected is False
        assert fake_ws.closed is True

    asyncio.run(scenario())

    # Verify wire protocol frames sent in exact order with incrementing IDs
    assert fake_ws.sent[0] == {
        "type": "auth",
        "access_token": "eyJ_long_lived_token_123456",
    }
    assert fake_ws.sent[1] == {
        "id": 1,
        "type": "subscribe_events",
        "event_type": "state_changed",
    }
    assert fake_ws.sent[2] == {"id": 2, "type": "ping"}
    assert fake_ws.sent[3] == {
        "id": 3,
        "type": "call_service",
        "domain": "light",
        "service": "turn_on",
        "service_data": {"brightness": 255},
        "target": {"entity_id": "light.living_room"},
    }
    assert fake_ws.sent[4] == {
        "id": 4,
        "type": "unsubscribe_events",
        "subscription": 1,
    }


def test_f21_1_ws_auth_invalid_and_missing_token():
    # Missing token fails immediately without opening socket
    no_tok = HomeAssistantWSManager(access_token="")
    with pytest.raises(HomeAssistantAuthError, match="token is required"):
        asyncio.run(no_tok.connect())
    assert no_tok.state == HAWSConnectionState.AUTH_FAILED

    # auth_invalid closes socket and raises HomeAssistantAuthError
    bad_ws = FakeHAWebSocket(
        [
            {"type": "auth_required", "ha_version": "2026.11.0"},
            {"type": "auth_invalid", "message": "Invalid access token or password"},
        ]
    )
    mgr = HomeAssistantWSManager(
        access_token="bad_token_xyz",
        ws_factory=lambda _url: bad_ws,
    )
    with pytest.raises(HomeAssistantAuthError, match="Invalid access token"):
        asyncio.run(mgr.connect())
    assert mgr.state == HAWSConnectionState.AUTH_FAILED
    assert mgr.is_connected is False
    assert bad_ws.closed is True


def test_f21_1_backoff_tracking_and_helpers():
    mgr = HomeAssistantWSManager(
        reconnect_base_delay=2.0,
        reconnect_max_delay=10.0,
    )
    assert mgr.record_disconnect("socket dropped") == 2.0
    assert mgr.record_disconnect("timeout") == 4.0
    assert mgr.record_disconnect("refused") == 8.0
    assert mgr.record_disconnect("still down") == 10.0
    assert mgr.record_disconnect("capped") == 10.0

    st = mgr.get_status()
    assert st.state == HAWSConnectionState.RECONNECTING
    assert st.reconnect_attempts == 5
    assert st.next_backoff_seconds == 10.0
    assert st.last_error == "capped"

    mgr.reset_backoff()
    assert mgr.get_status().reconnect_attempts == 0
    assert mgr.get_status().next_backoff_seconds == 0.0

    assert derive_ws_url("http://192.168.1.50:8123") == "ws://192.168.1.50:8123/api/websocket"
    assert derive_ws_url("https://ha.example.com/") == "wss://ha.example.com/api/websocket"
    assert derive_ws_url("http://ha.local:8123", "wss://custom/ws") == "wss://custom/ws"

    assert mask_access_token("") == ""
    assert mask_access_token("short") == "*****"
    masked = mask_access_token("eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9_secret9999")
    assert masked.startswith("eyJh")
    assert masked.endswith("9999")
    assert "secret" not in masked


# ---------------------------------------------------------------------------
# F21.2: Entity State Modification Trigger Tracking
# ---------------------------------------------------------------------------


def test_f21_2_state_changed_parsing_diffs_and_trigger_matching():
    tracker = EntityStateTracker()

    raw_event = {
        "type": "event",
        "event": {
            "event_type": "state_changed",
            "time_fired": "2026-11-21T20:00:00+00:00",
            "data": {
                "entity_id": "light.living_room",
                "old_state": {
                    "entity_id": "light.living_room",
                    "state": "off",
                    "attributes": {"brightness": 0, "color_temp": 400, "old_only": True},
                },
                "new_state": {
                    "entity_id": "light.living_room",
                    "state": "on",
                    "attributes": {"brightness": 220, "color_temp": 400, "effect": "warm"},
                },
            },
        },
    }

    transition = tracker.parse_state_changed(raw_event)
    assert transition is not None
    assert transition.entity_id == "light.living_room"
    assert transition.domain == "light"
    assert transition.old_state == "off"
    assert transition.new_state == "on"
    assert transition.state_changed is True
    assert transition.attribute_diffs == {
        "brightness": {"old": 0, "new": 220},
        "effect": {"old": None, "new": "warm"},
        "old_only": {"old": True, "new": None},
    }

    triggers = [
        HAEntityTrigger(
            id="t1",
            name="Any light turned on",
            entity_id="light.*",
            from_state="off",
            to_state="on",
            cooldown_seconds=300,
        ),
        HAEntityTrigger(
            id="t2",
            name="Brightness above 200",
            entity_id="light.living_room",
            attribute_name="brightness",
            attribute_operator="gt",
            attribute_value=200,
        ),
        HAEntityTrigger(
            id="t3",
            name="Brightness below 50 (should not match)",
            entity_id="light.living_room",
            attribute_name="brightness",
            attribute_operator="lt",
            attribute_value=50,
        ),
        HAEntityTrigger(
            id="t4",
            name="Effect contains warm",
            entity_id="light.living_room",
            attribute_name="effect",
            attribute_operator="contains",
            attribute_value="warm",
        ),
    ]

    t0 = datetime(2026, 11, 21, 20, 0, tzinfo=timezone.utc)
    matched, suppressed = tracker.evaluate_triggers(transition, triggers, evaluated_at=t0)
    assert {m.trigger_id for m in matched} == {"t1", "t2", "t4"}
    assert suppressed == []

    # Simulate cooldown on t1 -> suppressed 60s later, fires again 301s later
    triggers[0].last_triggered_at = t0.isoformat()
    matched2, suppressed2 = tracker.evaluate_triggers(
        transition, [triggers[0]], evaluated_at=t0 + timedelta(seconds=60)
    )
    assert matched2 == []
    assert len(suppressed2) == 1
    assert suppressed2[0].trigger_id == "t1"
    assert suppressed2[0].suppressed is True

    matched3, suppressed3 = tracker.evaluate_triggers(
        transition, [triggers[0]], evaluated_at=t0 + timedelta(seconds=301)
    )
    assert len(matched3) == 1
    assert suppressed3 == []


# ---------------------------------------------------------------------------
# F21.3: Event Dispatch API (Inbound Translation & Outbound call_service)
# ---------------------------------------------------------------------------


def test_f21_3_inbound_dispatch_and_outbound_ws_and_rest_service_calls(tmp_path: Path):
    storage = HomeAssistantStorage(tmp_path / "ha_dispatch.db")
    storage.upsert_config(
        HAConfig(
            base_url="http://ha.local:8123",
            access_token="eyJ_valid_token_98765",
        )
    )
    trig = storage.create_trigger(
        HAEntityTrigger(
            name="Front Door Opened",
            entity_id="binary_sensor.front_door",
            from_state="off",
            to_state="on",
            event_type="security.door_opened",
        )
    )

    received_events: list[NovaAutomationEvent] = []
    http_calls: list[dict[str, Any]] = []

    def mock_http(method, url, headers, body, timeout):
        http_calls.append(
            {"method": method, "url": url, "headers": headers, "body": body}
        )
        return [{"entity_id": "light.hallway", "state": "on"}]

    fake_ws = FakeHAWebSocket(
        [
            {"type": "auth_required", "ha_version": "2026.11.0"},
            {"type": "auth_ok", "ha_version": "2026.11.0"},
            {"id": 1, "type": "result", "success": True, "result": {"ok": True}},
        ]
    )
    ws_mgr = HomeAssistantWSManager(
        access_token="eyJ_valid_token_98765",
        ws_factory=lambda _url: fake_ws,
    )

    async def async_listener(evt: NovaAutomationEvent) -> None:
        received_events.append(evt)

    dispatcher = HomeAssistantEventDispatcher(
        storage=storage,
        ws_manager=ws_mgr,
        http_requester=mock_http,
        on_event=async_listener,
    )

    async def scenario():
        # 1. Inbound state_changed event -> translates to NovaAutomationEvent & triggers rule
        res = await dispatcher.dispatch_incoming_event(
            {
                "event_type": "state_changed",
                "data": {
                    "entity_id": "binary_sensor.front_door",
                    "old_state": {"state": "off", "attributes": {"device_class": "door"}},
                    "new_state": {"state": "on", "attributes": {"device_class": "door"}},
                },
            }
        )
        assert res.event.event_type == "ha.state_changed"
        assert res.event.entity_id == "binary_sensor.front_door"
        assert res.listeners_notified == 1
        assert len(res.triggered) == 1
        assert res.triggered[0].trigger_id == trig.id
        assert len(received_events) == 1

        # 2. Outbound call_service over REST (WS not connected yet, transport="auto")
        rest_res = await dispatcher.call_service(
            "light",
            "turn_on",
            entity_id="light.hallway",
            service_data={"brightness": 200},
            transport="auto",
        )
        assert rest_res.transport == "rest"
        assert rest_res.success is True
        assert len(http_calls) == 1
        assert http_calls[0]["url"] == "http://ha.local:8123/api/services/light/turn_on"
        assert http_calls[0]["body"] == {"brightness": 200, "entity_id": "light.hallway"}

        # 3. Connect WS -> outbound call_service with transport="auto" now uses WS!
        await ws_mgr.connect()
        ws_res = await dispatcher.call_service(
            "switch",
            "toggle",
            entity_id="switch.desk_plug",
            transport="auto",
        )
        assert ws_res.transport == "ws"
        assert ws_res.success is True

    asyncio.run(scenario())

    logs = storage.list_dispatch_logs(limit=10)
    assert len(logs) == 3
    directions = {entry.direction for entry in logs}
    assert directions == {"inbound", "outbound"}


# ---------------------------------------------------------------------------
# F21.4: Device Capabilities Sync Maps & Migration Rollback
# ---------------------------------------------------------------------------


def test_f21_4_domain_capability_mapping_and_sqlite_sync(tmp_path: Path):
    mapper = HACapabilityMapper()
    domain_map = mapper.get_domain_map()

    expected_domains = {
        "light",
        "switch",
        "climate",
        "sensor",
        "binary_sensor",
        "cover",
        "lock",
        "media_player",
        "fan",
    }
    assert expected_domains.issubset(set(domain_map.keys()))

    # Verify domain-specific capability detection across all 9 domains
    mapped_entities = [mapper.map_entity(raw) for raw in _sample_entities()]
    by_id = {e.entity_id: e for e in mapped_entities}

    assert by_id["light.living_room"].capabilities.category == "lighting"
    assert "dimming" in by_id["light.living_room"].capabilities.detected_features
    assert "color_rgb" in by_id["light.living_room"].capabilities.detected_features
    assert "turn_on" in by_id["light.living_room"].capabilities.supported_actions

    assert by_id["switch.desk_plug"].capabilities.category == "power"
    assert "power_monitoring" in by_id["switch.desk_plug"].capabilities.detected_features

    assert by_id["climate.bedroom_ac"].capabilities.category == "hvac"
    assert "mode:cool" in by_id["climate.bedroom_ac"].capabilities.detected_features
    assert "fan_control" in by_id["climate.bedroom_ac"].capabilities.detected_features

    assert by_id["sensor.living_room_temp"].capabilities.supported_actions == []
    assert "unit:°C" in by_id["sensor.living_room_temp"].capabilities.detected_features

    assert by_id["binary_sensor.front_door"].capabilities.state_type == "binary"
    assert "position_control" in by_id["cover.living_blinds"].capabilities.detected_features
    assert "lock" in by_id["lock.main_entry"].capabilities.supported_actions
    assert "volume_control" in by_id["media_player.studio_tv"].capabilities.detected_features
    assert "oscillation" in by_id["fan.office_fan"].capabilities.detected_features

    # Unknown domain falls back gracefully to generic
    generic = mapper.map_entity(
        {"entity_id": "vacuum.roborock", "state": "docked", "attributes": {"battery_level": 100}}
    )
    assert generic.capabilities.category == "generic"

    # Persist to SQLite and verify round-trip + rollback script cleanliness
    db_file = tmp_path / "ha_caps.db"
    storage = HomeAssistantStorage(db_file)
    storage.upsert_entities(mapped_entities)
    assert len(storage.list_entities()) == 9
    assert len(storage.list_entities(domain="light")) == 1
    assert storage.get_entity("climate.bedroom_ac").capabilities.category == "hvac"

    # Test rollback SQL drops all 0012 tables cleanly
    conn = sqlite3.connect(db_file)
    conn.executescript(MIGRATION_DOWN.read_text(encoding="utf-8"))
    remaining = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'ha_%'"
        ).fetchall()
    }
    conn.close()
    assert remaining == set()


# ---------------------------------------------------------------------------
# F21.5: REST Integration Endpoints (FastAPI TestClient)
# ---------------------------------------------------------------------------


def _build_test_app(
    tmp_path: Path,
    *,
    http_requester: Any = None,
    ws_factory: Any = None,
    on_event: Any = None,
) -> FastAPI:
    app = FastAPI()
    app.state.store = DatabaseStore(db_path=tmp_path / "nova_ha_test.db")
    if http_requester is not None:
        app.state.ha_http_requester = http_requester
    if ws_factory is not None:
        app.state.ha_ws_factory = ws_factory
    if on_event is not None:
        app.state.ha_event_callback = on_event
    app.include_router(auth_router)
    app.include_router(ha_router, dependencies=[Depends(get_current_user)])
    return app


def test_f21_5_api_jwt_auth_enforcement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "ha-secret-21")
    app = _build_test_app(tmp_path)
    client = TestClient(app)

    assert client.get("/api/v1/home-assistant/config").status_code == 401
    assert client.get("/api/v1/home-assistant/status").status_code == 401
    assert client.get("/api/v1/home-assistant/entities").status_code == 401
    assert client.post("/api/v1/home-assistant/sync").status_code == 401
    assert client.get("/api/v1/home-assistant/triggers").status_code == 401
    assert client.get("/api/v1/home-assistant/capabilities").status_code == 401

    login = client.post(
        "/auth/token", json={"username": "admin", "password": "ha-secret-21"}
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    resp = client.get("/api/v1/home-assistant/config", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["success"] is True


def test_f21_5_full_rest_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nova-ha-pass")
    dispatched_events: list[NovaAutomationEvent] = []
    outbound_calls: list[dict[str, Any]] = []

    def mock_http(method, url, headers, body, timeout):
        outbound_calls.append({"method": method, "url": url, "body": body})
        if method == "GET" and url.endswith("/api/states"):
            return _sample_entities()
        if method == "POST" and "/api/services/" in url:
            return [{"entity_id": body.get("entity_id"), "state": "on"}]
        return {}

    app = _build_test_app(
        tmp_path,
        http_requester=mock_http,
        on_event=lambda evt: dispatched_events.append(evt),
    )
    client = TestClient(app)

    token = client.post(
        "/auth/token", json={"username": "admin", "password": "nova-ha-pass"}
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. PUT /config -> masks access_token; partial update preserves stored token
    raw_token = "eyJ_super_secret_home_assistant_token_4321"
    put_cfg = client.put(
        "/api/v1/home-assistant/config",
        headers=headers,
        json={
            "base_url": "http://192.168.1.20:8123",
            "access_token": raw_token,
            "timeout_seconds": 8.0,
        },
    )
    assert put_cfg.status_code == 200
    cfg_body = put_cfg.json()["config"]
    assert cfg_body["has_access_token"] is True
    assert "access_token" not in cfg_body
    assert raw_token not in cfg_body["access_token_masked"]
    assert cfg_body["access_token_masked"].endswith("4321")
    assert cfg_body["resolved_ws_url"] == "ws://192.168.1.20:8123/api/websocket"

    patch_cfg = client.put(
        "/api/v1/home-assistant/config",
        headers=headers,
        json={"timeout_seconds": 12.0},
    )
    assert patch_cfg.status_code == 200
    assert patch_cfg.json()["config"]["has_access_token"] is True
    assert patch_cfg.json()["config"]["timeout_seconds"] == 12.0

    # Invalid base_url returns 400
    bad_cfg = client.put(
        "/api/v1/home-assistant/config",
        headers=headers,
        json={"base_url": "not-a-valid-url"},
    )
    assert bad_cfg.status_code == 400

    # 2. POST /sync via mocked REST /api/states -> syncs all 9 entities & capabilities
    sync_resp = client.post("/api/v1/home-assistant/sync", headers=headers, json={})
    assert sync_resp.status_code == 200
    assert sync_resp.json()["sync"]["synced_count"] == 9

    # 3. GET /entities & GET /entities/{entity_id}
    ent_all = client.get("/api/v1/home-assistant/entities", headers=headers)
    assert ent_all.status_code == 200
    assert ent_all.json()["count"] == 9

    ent_lights = client.get(
        "/api/v1/home-assistant/entities", headers=headers, params={"domain": "light"}
    )
    assert ent_lights.status_code == 200
    assert ent_lights.json()["count"] == 1

    ent_one = client.get(
        "/api/v1/home-assistant/entities/climate.bedroom_ac", headers=headers
    )
    assert ent_one.status_code == 200
    assert ent_one.json()["entity"]["capabilities"]["category"] == "hvac"

    ent_missing = client.get(
        "/api/v1/home-assistant/entities/light.does_not_exist", headers=headers
    )
    assert ent_missing.status_code == 404

    # 4. GET /capabilities (full & filtered by domain)
    caps_all = client.get("/api/v1/home-assistant/capabilities", headers=headers)
    assert caps_all.status_code == 200
    assert "light" in caps_all.json()["capabilities"]["supported_domains"]
    assert caps_all.json()["capabilities"]["synced_entities_count"] == 9

    caps_light = client.get(
        "/api/v1/home-assistant/capabilities",
        headers=headers,
        params={"domain": "light"},
    )
    assert caps_light.status_code == 200
    assert caps_light.json()["capabilities"]["capability"]["category"] == "lighting"
    assert caps_light.json()["capabilities"]["synced_entities_count"] == 1

    # 5. POST /triggers, GET /triggers, invalid trigger 400
    bad_trig = client.post(
        "/api/v1/home-assistant/triggers",
        headers=headers,
        json={"name": "Empty Criteria", "entity_id": "light.living_room"},
    )
    assert bad_trig.status_code == 400

    create_trig = client.post(
        "/api/v1/home-assistant/triggers",
        headers=headers,
        json={
            "name": "Door Opened Alert",
            "entity_id": "binary_sensor.front_door",
            "from_state": "off",
            "to_state": "on",
            "cooldown_seconds": 300,
            "event_type": "security.door_opened",
        },
    )
    assert create_trig.status_code == 201
    trig_id = create_trig.json()["trigger"]["id"]

    list_trigs = client.get("/api/v1/home-assistant/triggers", headers=headers)
    assert list_trigs.status_code == 200
    assert list_trigs.json()["count"] == 1

    # 6. POST /events/ingest -> fires trigger, then suppresses within cooldown
    t0 = datetime(2026, 11, 21, 21, 0, tzinfo=timezone.utc)
    door_event = {
        "event_type": "state_changed",
        "data": {
            "entity_id": "binary_sensor.front_door",
            "old_state": {"state": "off", "attributes": {"device_class": "door"}},
            "new_state": {"state": "on", "attributes": {"device_class": "door"}},
        },
    }
    ing1 = client.post(
        "/api/v1/home-assistant/events/ingest",
        headers=headers,
        json={"event": door_event, "evaluated_at": t0.isoformat()},
    )
    assert ing1.status_code == 200
    d1 = ing1.json()["dispatch"]
    assert len(d1["triggered"]) == 1
    assert d1["triggered"][0]["trigger_id"] == trig_id
    assert d1["listeners_notified"] == 1
    assert len(dispatched_events) == 1

    ing2 = client.post(
        "/api/v1/home-assistant/events/ingest",
        headers=headers,
        json={
            "event": door_event,
            "evaluated_at": (t0 + timedelta(seconds=30)).isoformat(),
        },
    )
    assert ing2.status_code == 200
    d2 = ing2.json()["dispatch"]
    assert d2["triggered"] == []
    assert len(d2["suppressed"]) == 1

    # 7. POST /services/call -> dispatches outbound REST service call
    svc_resp = client.post(
        "/api/v1/home-assistant/services/call",
        headers=headers,
        json={
            "domain": "light",
            "service": "turn_on",
            "entity_id": "light.living_room",
            "service_data": {"brightness": 190},
        },
    )
    assert svc_resp.status_code == 200
    svc_body = svc_resp.json()["result"]
    assert svc_body["success"] is True
    assert svc_body["transport"] == "rest"
    assert svc_body["audit_id"].startswith("hadisp_")

    # 8. GET /status -> reflects configured state, synced entities, and triggers
    st_resp = client.get("/api/v1/home-assistant/status", headers=headers)
    assert st_resp.status_code == 200
    st = st_resp.json()["status"]
    assert st["configured"] is True
    assert st["synced_entities_count"] == 9
    assert st["triggers_count"] == 1

    # 9. DELETE /triggers/{id} & verify 404 on second delete
    del1 = client.delete(
        f"/api/v1/home-assistant/triggers/{trig_id}", headers=headers
    )
    assert del1.status_code == 200
    assert del1.json()["success"] is True

    del2 = client.delete(
        f"/api/v1/home-assistant/triggers/{trig_id}", headers=headers
    )
    assert del2.status_code == 404
