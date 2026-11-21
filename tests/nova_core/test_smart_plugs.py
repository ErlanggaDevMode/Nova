"""Comprehensive offline test suite for Phase 20: Smart Plug HTTP Integrations (F20.1–F20.5)."""

from __future__ import annotations

import base64
import json
import struct
from pathlib import Path
from typing import Any, Optional

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nova_core.api.routes_auth import router as auth_router
from nova_core.api.routes_smart_plugs import router as smart_plugs_router
from nova_core.auth import get_current_user
from nova_core.db.store import DatabaseStore
from nova_core.integrations.smart_home.smart_plugs import (
    KASA_XOR_INITIAL_KEY,
    DiscoveredSmartPlug,
    KasaPlugClient,
    ShellyPlugClient,
    SmartPlugAuthError,
    SmartPlugDevice,
    SmartPlugDiscoveryScanner,
    SmartPlugError,
    SmartPlugProtocolError,
    SmartPlugRuleParseError,
    SmartPlugRuleParser,
    SmartPlugService,
    SmartPlugStorage,
    SmartPlugTimeoutError,
    kasa_decrypt,
    kasa_encrypt,
    kasa_pack_tcp,
    kasa_unpack_tcp,
    mask_secret,
)


# ---------------------------------------------------------------------------
# F20.1: Shelly Plug HTTP Commands Sender (Gen1 & Gen2 RPC)
# ---------------------------------------------------------------------------


def test_f20_1_shelly_gen1_commands_status_and_basic_auth():
    recorded: list[dict[str, Any]] = []
    relay_state = {"ison": False}

    def fake_transport(
        method: str,
        url: str,
        params: Optional[dict[str, Any]],
        json_body: Optional[dict[str, Any]],
        headers: dict[str, str],
        timeout: float,
    ):
        recorded.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "json_body": json_body,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if url.endswith("/relay/0"):
            turn = (params or {}).get("turn")
            if turn == "on":
                relay_state["ison"] = True
            elif turn == "off":
                relay_state["ison"] = False
            elif turn == "toggle":
                relay_state["ison"] = not relay_state["ison"]
            return 200, {"ison": relay_state["ison"], "has_timer": False}

        if url.endswith("/status"):
            return 200, {
                "relays": [{"ison": relay_state["ison"]}],
                "meters": [{"power": 142.5, "total": 3600}],  # 3600 W-min = 60.0 Wh
                "temperature": 34.2,
                "update": {"old_version": "20230913-114010/v1.14.0"},
            }
        return 404, {"error": "not found"}

    client = ShellyPlugClient(
        "192.168.1.50",
        generation="gen1",
        username="admin",
        password="shelly-secret",
        timeout_seconds=3.5,
        transport=fake_transport,
    )

    on_snap = client.turn_on(channel=0)
    assert on_snap.is_on is True
    assert on_snap.online is True
    assert recorded[0]["method"] == "GET"
    assert recorded[0]["url"] == "http://192.168.1.50/relay/0"
    assert recorded[0]["params"] == {"turn": "on"}
    assert recorded[0]["timeout"] == 3.5

    expected_b64 = base64.b64encode(b"admin:shelly-secret").decode("ascii")
    assert recorded[0]["headers"]["Authorization"] == f"Basic {expected_b64}"

    toggle_snap = client.toggle(channel=0)
    assert toggle_snap.is_on is False
    assert recorded[1]["params"] == {"turn": "toggle"}

    client.turn_on(channel=0)
    status_snap = client.get_status(channel=0)
    assert status_snap.is_on is True
    assert status_snap.power_watts == 142.5
    assert status_snap.total_energy_wh == 60.0
    assert status_snap.temperature_c == 34.2
    assert status_snap.firmware == "20230913-114010/v1.14.0"


def test_f20_1_shelly_gen2_rpc_commands_status_and_bearer_auth():
    recorded: list[dict[str, Any]] = []
    switch_on = {"output": False}

    def fake_transport(method, url, params, json_body, headers, timeout):
        recorded.append(
            {
                "method": method,
                "url": url,
                "json_body": json_body,
                "headers": headers,
            }
        )
        if url.endswith("/rpc/Switch.Set"):
            was_on = switch_on["output"]
            switch_on["output"] = bool(json_body["on"])
            return 200, {"was_on": was_on}
        if url.endswith("/rpc/Switch.GetStatus"):
            return 200, {
                "id": json_body["id"],
                "output": switch_on["output"],
                "apower": 85.4,
                "voltage": 230.2,
                "current": 0.371,
                "aenergy": {"total": 1245.5},
                "temperature": {"tC": 39.8},
            }
        return 404, {"code": 404, "message": "not found"}

    client = ShellyPlugClient(
        "192.168.1.51",
        generation="gen2",
        port=8443,
        use_https=True,
        auth_token="tok_gen2_9999",
        transport=fake_transport,
    )

    res_on = client.turn_on(channel=1)
    assert res_on.is_on is True
    assert recorded[0]["method"] == "POST"
    assert recorded[0]["url"] == "https://192.168.1.51:8443/rpc/Switch.Set"
    assert recorded[0]["json_body"] == {"id": 1, "on": True}
    assert recorded[0]["headers"]["Authorization"] == "Bearer tok_gen2_9999"

    status = client.get_status(channel=1)
    assert status.is_on is True
    assert status.power_watts == 85.4
    assert status.voltage == 230.2
    assert status.current_amps == 0.371
    assert status.total_energy_wh == 1245.5
    assert status.temperature_c == 39.8

    # Gen2 toggle queries Switch.GetStatus then invokes Switch.Set with negated state
    toggled = client.toggle(channel=1)
    assert toggled.is_on is False
    assert recorded[-2]["url"].endswith("/rpc/Switch.GetStatus")
    assert recorded[-1]["url"].endswith("/rpc/Switch.Set")
    assert recorded[-1]["json_body"] == {"id": 1, "on": False}


def test_f20_1_shelly_error_handling():
    def err_401(*_args):
        return 401, {"error": "unauthorized"}

    def err_500(*_args):
        return 500, {"error": "internal"}

    def err_timeout(*_args):
        raise TimeoutError("timed out")

    def err_rpc(*_args):
        return 200, {"code": -103, "message": "Invalid argument 'id'"}

    def err_non_json(*_args):
        return 200, "<html>unexpected</html>"

    with pytest.raises(SmartPlugAuthError):
        ShellyPlugClient("192.168.1.50", transport=err_401).get_status()

    with pytest.raises(SmartPlugError) as exc_500:
        ShellyPlugClient("192.168.1.50", transport=err_500).get_status()
    assert exc_500.value.status_code == 500
    assert exc_500.value.retryable is True

    with pytest.raises(SmartPlugTimeoutError):
        ShellyPlugClient("192.168.1.50", transport=err_timeout).get_status()

    with pytest.raises(SmartPlugProtocolError, match="-103"):
        ShellyPlugClient("192.168.1.50", generation="gen2", transport=err_rpc).get_status()

    with pytest.raises(SmartPlugProtocolError, match="non-JSON"):
        ShellyPlugClient("192.168.1.50", transport=err_non_json).get_status()


# ---------------------------------------------------------------------------
# F20.2: TP-Link Kasa XOR-Autokey Cipher & Local Driver
# ---------------------------------------------------------------------------


def test_f20_2_kasa_xor_cipher_and_tcp_framing_roundtrip():
    plaintext = '{"system":{"get_sysinfo":{}}}'
    encrypted = kasa_encrypt(plaintext)

    # First encrypted byte must equal 171 XOR ord('{')
    assert encrypted[0] == (KASA_XOR_INITIAL_KEY ^ ord("{"))
    # Second byte must use previous ciphertext byte as autokey
    assert encrypted[1] == (encrypted[0] ^ ord('"'))
    assert kasa_decrypt(encrypted) == plaintext

    # 4-byte big-endian length framing round-trip
    payload = {"system": {"set_relay_state": {"state": 1}}}
    frame = kasa_pack_tcp(payload)
    declared_len = struct.unpack(">I", frame[:4])[0]
    assert declared_len == len(frame) - 4
    assert kasa_unpack_tcp(frame) == payload

    # Malformed frames rejected cleanly
    with pytest.raises(SmartPlugProtocolError, match="Truncated"):
        kasa_unpack_tcp(b"\x00\x01")
    with pytest.raises(SmartPlugProtocolError, match="Invalid Kasa TCP frame length"):
        kasa_unpack_tcp(struct.pack(">I", 0))
    with pytest.raises(SmartPlugProtocolError, match="Incomplete"):
        kasa_unpack_tcp(struct.pack(">I", 20) + b"\x01\x02")
    with pytest.raises(SmartPlugProtocolError, match="invalid JSON"):
        bad_enc = kasa_encrypt("not-json")
        kasa_unpack_tcp(struct.pack(">I", len(bad_enc)) + bad_enc)


def test_f20_2_kasa_driver_commands_and_emeter_variants():
    state = {"relay_state": 0, "emeter_mode": "v1"}
    sent_commands: list[dict[str, Any]] = []

    def fake_kasa_transport(host: str, port: int, frame: bytes, timeout: float) -> bytes:
        assert host == "192.168.1.60"
        assert port == 9999
        cmd = kasa_unpack_tcp(frame)
        sent_commands.append(cmd)

        if "system" in cmd and "get_sysinfo" in cmd["system"]:
            return kasa_pack_tcp(
                {
                    "system": {
                        "get_sysinfo": {
                            "err_code": 0,
                            "alias": "Office Desk Plug",
                            "model": "HS110(US)",
                            "sw_ver": "1.2.6",
                            "feature": "TIM:ENE",
                            "relay_state": state["relay_state"],
                        }
                    }
                }
            )
        if "system" in cmd and "set_relay_state" in cmd["system"]:
            state["relay_state"] = int(cmd["system"]["set_relay_state"]["state"])
            return kasa_pack_tcp({"system": {"set_relay_state": {"err_code": 0}}})
        if "emeter" in cmd and "get_realtime" in cmd["emeter"]:
            if state["emeter_mode"] == "v1":
                return kasa_pack_tcp(
                    {
                        "emeter": {
                            "get_realtime": {
                                "err_code": 0,
                                "power_mw": 45200,
                                "voltage_mv": 120500,
                                "current_ma": 375,
                                "total_wh": 812,
                            }
                        }
                    }
                )
            if state["emeter_mode"] == "v2":
                return kasa_pack_tcp(
                    {
                        "emeter": {
                            "get_realtime": {
                                "err_code": 0,
                                "power": 62.5,
                                "voltage": 229.0,
                                "current": 0.27,
                                "total": 1.5,  # 1.5 kWh -> 1500 Wh
                            }
                        }
                    }
                )
            return kasa_pack_tcp(
                {
                    "emeter": {
                        "get_realtime": {
                            "err_code": -2,
                            "err_msg": "member not support",
                        }
                    }
                }
            )
        raise AssertionError(f"Unexpected command: {cmd}")

    client = KasaPlugClient("192.168.1.60", transport=fake_kasa_transport)

    # Turn on & check status with v1 emeter
    res_on = client.turn_on()
    assert res_on.is_on is True

    snap_v1 = client.get_status()
    assert snap_v1.is_on is True
    assert snap_v1.device_alias == "Office Desk Plug"
    assert snap_v1.device_model == "HS110(US)"
    assert snap_v1.power_watts == 45.2
    assert snap_v1.voltage == 120.5
    assert snap_v1.current_amps == 0.375
    assert snap_v1.total_energy_wh == 812.0

    # Toggle -> turns off
    res_toggle = client.toggle()
    assert res_toggle.is_on is False
    assert state["relay_state"] == 0

    # Hardware v2 emeter conversion (kWh -> Wh)
    state["emeter_mode"] = "v2"
    snap_v2 = client.get_status()
    assert snap_v2.power_watts == 62.5
    assert snap_v2.total_energy_wh == 1500.0

    # Unsupported emeter degrades gracefully during get_status()
    state["emeter_mode"] = "unsupported"
    snap_no_emeter = client.get_status()
    assert snap_no_emeter.online is True
    assert snap_no_emeter.power_watts is None

    # Timeout translation
    def timeout_transport(*_args):
        raise TimeoutError("socket timeout")

    with pytest.raises(SmartPlugTimeoutError):
        KasaPlugClient("192.168.1.60", transport=timeout_transport).get_sysinfo()


# ---------------------------------------------------------------------------
# F20.3: Local Network Smart Plug Discovery Scanner
# ---------------------------------------------------------------------------


def test_f20_3_discovery_scanner_validation_and_mocked_probes():
    def fake_shelly_transport(method, url, params, json_body, headers, timeout):
        if url == "http://192.168.10.2/rpc/Shelly.GetDeviceInfo":
            return 200, {
                "id": "shellyplusplugs-01",
                "name": "Coffee Maker Plug",
                "model": "SNPL-00112EU",
                "gen": 2,
                "mac": "AA:BB:CC:DD:EE:01",
            }
        if url == "http://192.168.10.3/shelly":
            return 200, {
                "type": "SHPLG-S",
                "mac": "AA:BB:CC:DD:EE:02",
            }
        raise TimeoutError("unreachable")

    def fake_kasa_transport(host, port, frame, timeout):
        if host == "192.168.10.4":
            return kasa_pack_tcp(
                {
                    "system": {
                        "get_sysinfo": {
                            "err_code": 0,
                            "alias": "Living Room Lamp",
                            "model": "KP115(US)",
                            "mac": "11:22:33:44:55:66",
                            "relay_state": 1,
                        }
                    }
                }
            )
        raise TimeoutError("unreachable")

    scanner = SmartPlugDiscoveryScanner(
        shelly_transport=fake_shelly_transport,
        kasa_transport=fake_kasa_transport,
    )

    # Scan bounded private /29 subnet (192.168.10.1 .. 192.168.10.6)
    scan_res = scanner.scan(
        cidr="192.168.10.0/29",
        timeout_seconds=0.5,
        max_concurrency=4,
    )
    assert scan_res.targets_scanned == 6
    assert len(scan_res.discovered) == 3

    by_host = {d.host: d for d in scan_res.discovered}
    assert by_host["192.168.10.2"].provider == "shelly"
    assert by_host["192.168.10.2"].protocol_version == "gen2"
    assert by_host["192.168.10.3"].provider == "shelly"
    assert by_host["192.168.10.3"].protocol_version == "gen1"
    assert by_host["192.168.10.4"].provider == "kasa"
    assert by_host["192.168.10.4"].is_on is True

    # Safety guards: reject public IPs, public CIDRs, oversized CIDRs, bad concurrency
    with pytest.raises(ValueError, match="non-private"):
        scanner.scan(hosts=["8.8.8.8"])
    with pytest.raises(ValueError, match="non-private"):
        scanner.scan(cidr="1.1.1.0/29")
    with pytest.raises(ValueError, match="exceeding max_hosts"):
        scanner.scan(cidr="10.0.0.0/16")
    with pytest.raises(ValueError, match="max_concurrency"):
        scanner.scan(hosts=["192.168.1.10"], max_concurrency=100)


# ---------------------------------------------------------------------------
# F20.5: Custom Smart Plug Category Rule Parser
# ---------------------------------------------------------------------------


def test_f20_5_rule_parser_dict_and_dsl_valid_cases():
    parser = SmartPlugRuleParser()

    # Structured dict rule
    parsed_dict = parser.parse(
        {
            "name": "Office Heater Overload Cutoff",
            "target": {"category": "appliance", "room": "office"},
            "condition": {"type": "smart_plug.power_gt", "threshold_watts": 1500},
            "action": {"type": "smart_plug.turn_off"},
            "cooldown_seconds": 120,
        }
    )
    assert parsed_dict.name == "Office Heater Overload Cutoff"
    assert parsed_dict.target.category == "appliance"
    assert parsed_dict.target.room == "office"
    assert parsed_dict.action_type == "smart_plug.turn_off"
    assert parsed_dict.command == "off"
    assert parsed_dict.normalized_condition["type"] == "smart_plug"
    assert parsed_dict.normalized_action["category"] == "smart_home"
    assert parsed_dict.normalized_action["action_type"] == "smart_plug.turn_off"
    assert parsed_dict.normalized_action["params"]["command"] == "off"

    # Evaluate telemetry matching
    assert parsed_dict.matches_telemetry(
        {"category": "appliance", "room": "office", "power_watts": 1620.0}
    ) is True
    assert parsed_dict.matches_telemetry(
        {"category": "appliance", "room": "office", "power_watts": 900.0}
    ) is False
    assert parsed_dict.matches_telemetry(
        {"category": "lighting", "room": "office", "power_watts": 1800.0}
    ) is False

    # Compact DSL with compound AND conditions
    parsed_dsl = parser.parse(
        "WHEN category:workstation room:office smart_plug.state_is on AND smart_plug.power_lt 10 "
        "THEN smart_plug.turn_off cooldown:300",
        name="Idle Workstation Auto-Off",
    )
    assert parsed_dsl.match == "all"
    assert len(parsed_dsl.clauses) == 2
    assert parsed_dsl.cooldown_seconds == 300
    assert parsed_dsl.matches_telemetry(
        {"category": "workstation", "room": "office", "is_on": True, "power_watts": 4.2}
    ) is True
    assert parsed_dsl.matches_telemetry(
        {"category": "workstation", "room": "office", "is_on": True, "power_watts": 85.0}
    ) is False

    # Compact DSL with OR and explicit plug targeting + toggle
    parsed_or = parser.parse(
        "IF plug:plug_lamp_1 smart_plug.state_is off OR smart_plug.online_is true "
        "THEN smart_plug.toggle"
    )
    assert parsed_or.match == "any"
    assert parsed_or.target.plug_ids == ["plug_lamp_1"]
    assert parsed_or.command == "toggle"
    assert parsed_or.matches_telemetry(
        {"plug_id": "plug_lamp_1", "is_on": True, "online": True}
    ) is True
    assert parsed_or.matches_telemetry(
        {"plug_id": "other_plug", "is_on": False, "online": True}
    ) is False


def test_f20_5_rule_parser_invalid_cases():
    parser = SmartPlugRuleParser()

    # Unknown condition
    with pytest.raises(SmartPlugRuleParseError, match="Unsupported smart plug condition"):
        parser.parse(
            {
                "name": "Bad Cond",
                "target": {"category": "lighting"},
                "condition": {"type": "smart_plug.voltage_gt", "value": 240},
                "action": "smart_plug.turn_off",
            }
        )

    # Unknown action
    with pytest.raises(SmartPlugRuleParseError, match="Unsupported smart plug action"):
        parser.parse(
            {
                "name": "Bad Action",
                "target": {"category": "lighting"},
                "condition": {"type": "smart_plug.power_gt", "value": 100},
                "action": "smart_plug.explode",
            }
        )

    # Negative power threshold
    with pytest.raises(SmartPlugRuleParseError, match="finite number >= 0"):
        parser.parse(
            {
                "name": "Negative Watts",
                "target": {"category": "lighting"},
                "condition": {"type": "smart_plug.power_gt", "threshold_watts": -5},
                "action": "smart_plug.turn_off",
            }
        )

    # Invalid state_is value
    with pytest.raises(SmartPlugRuleParseError, match="Expected 'on' or 'off'"):
        parser.parse("WHEN category:lighting smart_plug.state_is maybe THEN smart_plug.turn_on")

    # Missing target
    with pytest.raises(SmartPlugRuleParseError, match="at least one of 'category', 'room', or 'plug_ids'"):
        parser.parse("WHEN smart_plug.power_gt 500 THEN smart_plug.turn_off")

    # Missing THEN
    with pytest.raises(SmartPlugRuleParseError, match="THEN"):
        parser.parse("WHEN category:lighting smart_plug.state_is on")

    # Mixed AND / OR
    with pytest.raises(SmartPlugRuleParseError, match="Cannot mix 'AND' and 'OR'"):
        parser.parse(
            "WHEN category:lighting smart_plug.state_is on AND smart_plug.power_gt 10 "
            "OR smart_plug.online_is true THEN smart_plug.turn_off"
        )


# ---------------------------------------------------------------------------
# F20.4: Migration 0011, Rollback, SQLite Sync Persistence & FastAPI Routes
# ---------------------------------------------------------------------------


def test_f20_4_migration_0011_apply_and_rollback(tmp_path: Path):
    store = DatabaseStore(db_path=tmp_path / "smart_plugs_mig.db")

    with store.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '0011_smart_plugs'"
        ).fetchone()
        assert row is not None
        for tbl in ("smart_plugs", "smart_plug_status_history", "smart_plug_rules"):
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
            assert exists is not None

    assert store.rollback_migration("0011_smart_plugs") is True

    with store.get_connection() as conn:
        for tbl in ("smart_plugs", "smart_plug_status_history", "smart_plug_rules"):
            exists = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (tbl,),
            ).fetchone()
            assert exists is None


def test_f20_4_secret_masking():
    assert mask_secret(None) == ""
    assert mask_secret("abc") == "***"
    masked = mask_secret("super_secret_token_9876")
    assert masked.endswith("9876")
    assert "secret" not in masked


def _build_test_app(
    tmp_path: Path,
    *,
    shelly_transport=None,
    kasa_transport=None,
    shelly_probe=None,
    kasa_probe=None,
) -> FastAPI:
    app = FastAPI(title="Smart Plugs Phase 20 Test App")
    app.state.store = DatabaseStore(db_path=tmp_path / "api_smart_plugs.db")
    if shelly_transport is not None:
        app.state.shelly_http_transport = shelly_transport
    if kasa_transport is not None:
        app.state.kasa_tcp_transport = kasa_transport
    if shelly_probe is not None:
        app.state.smart_plug_shelly_probe = shelly_probe
    if kasa_probe is not None:
        app.state.smart_plug_kasa_probe = kasa_probe
    app.include_router(auth_router)
    app.include_router(smart_plugs_router, dependencies=[Depends(get_current_user)])
    return app


def test_f20_4_api_jwt_auth_enforcement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "plugs-secret-20")
    app = _build_test_app(tmp_path)
    client = TestClient(app)

    assert client.get("/api/v1/smart-plugs").status_code == 401
    assert client.post("/api/v1/smart-plugs", json={}).status_code == 401
    assert client.post("/api/v1/smart-plugs/sync", json={}).status_code == 401
    assert client.post("/api/v1/smart-plugs/discover", json={"hosts": ["192.168.1.2"]}).status_code == 401
    assert client.post("/api/v1/smart-plugs/rules/parse", json={}).status_code == 401

    login = client.post(
        "/auth/token",
        json={"username": "admin", "password": "plugs-secret-20"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    resp = client.get("/api/v1/smart-plugs", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["count"] == 0


def test_f20_4_full_rest_workflow_and_db_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nova-plugs-pass")

    shelly_state = {"output": False, "apower": 0.0}
    kasa_state = {"relay_state": 1, "power_mw": 55400}

    def mock_shelly(method, url, params, json_body, headers, timeout):
        if url.endswith("/rpc/Switch.Set"):
            shelly_state["output"] = bool(json_body["on"])
            shelly_state["apower"] = 120.5 if shelly_state["output"] else 0.0
            return 200, {"was_on": not shelly_state["output"]}
        if url.endswith("/rpc/Switch.GetStatus"):
            return 200, {
                "id": 0,
                "output": shelly_state["output"],
                "apower": shelly_state["apower"],
                "voltage": 231.0,
                "current": 0.52,
                "aenergy": {"total": 410.0},
            }
        raise TimeoutError("unreachable")

    def mock_kasa(host, port, frame, timeout):
        cmd = kasa_unpack_tcp(frame)
        if "system" in cmd and "get_sysinfo" in cmd["system"]:
            return kasa_pack_tcp(
                {
                    "system": {
                        "get_sysinfo": {
                            "err_code": 0,
                            "alias": "Monitor Strip",
                            "model": "HS110",
                            "feature": "TIM:ENE",
                            "relay_state": kasa_state["relay_state"],
                        }
                    }
                }
            )
        if "system" in cmd and "set_relay_state" in cmd["system"]:
            kasa_state["relay_state"] = int(cmd["system"]["set_relay_state"]["state"])
            kasa_state["power_mw"] = 55400 if kasa_state["relay_state"] else 0
            return kasa_pack_tcp({"system": {"set_relay_state": {"err_code": 0}}})
        if "emeter" in cmd and "get_realtime" in cmd["emeter"]:
            return kasa_pack_tcp(
                {
                    "emeter": {
                        "get_realtime": {
                            "err_code": 0,
                            "power_mw": kasa_state["power_mw"],
                            "voltage_mv": 228000,
                            "current_ma": 243,
                            "total_wh": 900,
                        }
                    }
                }
            )
        raise TimeoutError("unknown kasa cmd")

    def mock_shelly_probe(host: str, timeout: float) -> Optional[DiscoveredSmartPlug]:
        if host == "192.168.50.10":
            return DiscoveredSmartPlug(
                provider="shelly",
                host=host,
                port=80,
                protocol_version="gen1",
                name="Discovered Lamp Plug",
                model="SHPLG-S",
                mac="DE:AD:BE:EF:00:01",
            )
        return None

    def mock_kasa_probe(host: str, timeout: float) -> Optional[DiscoveredSmartPlug]:
        return None

    app = _build_test_app(
        tmp_path,
        shelly_transport=mock_shelly,
        kasa_transport=mock_kasa,
        shelly_probe=mock_shelly_probe,
        kasa_probe=mock_kasa_probe,
    )
    client = TestClient(app)

    token = client.post(
        "/auth/token",
        json={"username": "admin", "password": "nova-plugs-pass"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Register Shelly Gen2 plug with secret token (verify masking!)
    r1 = client.post(
        "/api/v1/smart-plugs",
        headers=headers,
        json={
            "name": "Espresso Machine",
            "provider": "shelly",
            "host": "192.168.1.70",
            "protocol_version": "gen2",
            "category": "appliance",
            "room": "kitchen",
            "auth_token": "shelly_supersecret_4321",
        },
    )
    assert r1.status_code == 201
    shelly_plug = r1.json()["plug"]
    shelly_id = shelly_plug["id"]
    assert shelly_plug["has_auth_token"] is True
    assert "auth_token" not in shelly_plug
    assert "supersecret" not in shelly_plug["auth_token_masked"]
    assert shelly_plug["auth_token_masked"].endswith("4321")

    # 2. Register Kasa plug
    r2 = client.post(
        "/api/v1/smart-plugs",
        headers=headers,
        json={
            "name": "Office Monitor",
            "provider": "kasa",
            "host": "192.168.1.71",
            "category": "workstation",
            "room": "office",
        },
    )
    assert r2.status_code == 201
    kasa_id = r2.json()["plug"]["id"]

    # 3. List plugs with filters
    list_all = client.get("/api/v1/smart-plugs", headers=headers)
    assert list_all.status_code == 200
    assert list_all.json()["count"] == 2

    list_office = client.get("/api/v1/smart-plugs", headers=headers, params={"room": "office"})
    assert list_office.json()["count"] == 1
    assert list_office.json()["plugs"][0]["id"] == kasa_id

    # 4. Control power via /{plug_id}/power and /power
    p_on = client.post(
        f"/api/v1/smart-plugs/{shelly_id}/power",
        headers=headers,
        json={"action": "on"},
    )
    assert p_on.status_code == 200
    assert p_on.json()["result"]["is_on"] is True

    p_toggle = client.post(
        "/api/v1/smart-plugs/power",
        headers=headers,
        json={"plug_id": kasa_id, "action": "toggle"},
    )
    assert p_toggle.status_code == 200
    assert p_toggle.json()["result"]["is_on"] is False

    # Turn Kasa back on before batch sync
    client.post(f"/api/v1/smart-plugs/{kasa_id}/power", headers=headers, json={"action": "on"})

    # 5. Live status & wattage sync persisted to SQLite (single + batch)
    sync_one = client.post(f"/api/v1/smart-plugs/{shelly_id}/sync", headers=headers)
    assert sync_one.status_code == 200
    assert sync_one.json()["snapshot"]["power_watts"] == 120.5

    sync_all = client.post("/api/v1/smart-plugs/sync", headers=headers, json={})
    assert sync_all.status_code == 200
    batch = sync_all.json()["result"]
    assert batch["total"] == 2
    assert batch["online_count"] == 2
    assert batch["offline_count"] == 0
    assert batch["total_power_watts"] == pytest.approx(175.9, rel=1e-3)

    # Verify persisted cached status and telemetry history in SQLite
    fetched = client.get(
        f"/api/v1/smart-plugs/{shelly_id}",
        headers=headers,
        params={"include_history": True},
    )
    assert fetched.status_code == 200
    assert fetched.json()["plug"]["is_on"] is True
    assert fetched.json()["plug"]["power_watts"] == 120.5
    assert len(fetched.json()["history"]) >= 2

    hist = client.get(f"/api/v1/smart-plugs/{kasa_id}/history", headers=headers)
    assert hist.status_code == 200
    assert hist.json()["count"] >= 2

    # 6. Discovery endpoint (rejects public CIDR with 400; auto-registers LAN discovery)
    bad_disc = client.post(
        "/api/v1/smart-plugs/discover",
        headers=headers,
        json={"cidr": "8.8.8.0/29"},
    )
    assert bad_disc.status_code == 400

    good_disc = client.post(
        "/api/v1/smart-plugs/discover",
        headers=headers,
        json={
            "hosts": ["192.168.50.10", "192.168.50.11"],
            "auto_register": True,
            "default_category": "lighting",
            "default_room": "bedroom",
        },
    )
    assert good_disc.status_code == 200
    disc_res = good_disc.json()["result"]
    assert len(disc_res["discovered"]) == 1
    assert len(disc_res["registered_plug_ids"]) == 1
    assert client.get("/api/v1/smart-plugs", headers=headers).json()["count"] == 3

    # 7. Rule parse endpoint (validation error -> 400; valid with save=True -> persisted)
    bad_rule = client.post(
        "/api/v1/smart-plugs/rules/parse",
        headers=headers,
        json={"expression": "WHEN category:appliance smart_plug.power_gt -10 THEN smart_plug.turn_off"},
    )
    assert bad_rule.status_code == 400

    good_rule = client.post(
        "/api/v1/smart-plugs/rules/parse",
        headers=headers,
        json={
            "name": "Kitchen Safety Shutoff",
            "expression": "WHEN category:appliance room:kitchen smart_plug.power_gt 1800 THEN smart_plug.turn_off",
            "save": True,
        },
    )
    assert good_rule.status_code == 200
    rule_id = good_rule.json()["rule"]["id"]
    assert rule_id is not None

    rules_list = client.get("/api/v1/smart-plugs/rules", headers=headers, params={"room": "kitchen"})
    assert rules_list.status_code == 200
    assert rules_list.json()["count"] == 1

    del_rule = client.delete(f"/api/v1/smart-plugs/rules/{rule_id}", headers=headers)
    assert del_rule.status_code == 200
    assert client.delete(f"/api/v1/smart-plugs/rules/{rule_id}", headers=headers).status_code == 404

    # 8. Delete plug & confirm 404 on repeat
    del_plug = client.delete(f"/api/v1/smart-plugs/{shelly_id}", headers=headers)
    assert del_plug.status_code == 200
    assert client.delete(f"/api/v1/smart-plugs/{shelly_id}", headers=headers).status_code == 404
