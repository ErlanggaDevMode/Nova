"""Comprehensive tests for Phase 17: Weather API & Conditions (F17.1–F17.5)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nova_core.api.routes_auth import router as auth_router
from nova_core.api.routes_weather import router as weather_router
from nova_core.auth import get_current_user
from nova_core.db.store import DatabaseStore
from nova_core.integrations.weather import (
    ConditionChangeEvaluator,
    LocationUpdateAdapter,
    OpenWeatherClient,
    WeatherAlertRule,
    WeatherAuthError,
    WeatherClientError,
    WeatherRuleType,
    WeatherService,
    WeatherSnapshot,
    WeatherStorage,
    WeatherThresholdEvaluator,
    WeatherTimeoutError,
    convert_temperature,
    mask_api_key,
)


def _sample_owm_payload(
    *,
    name: str = "Jakarta",
    lat: float = -6.2088,
    lon: float = 106.8456,
    temp: float = 31.5,
    humidity: float = 78.0,
    condition: str = "Clear",
    description: str = "clear sky",
) -> dict[str, Any]:
    return {
        "name": name,
        "coord": {"lat": lat, "lon": lon},
        "main": {
            "temp": temp,
            "feels_like": temp + 2.0,
            "humidity": humidity,
            "pressure": 1009,
        },
        "wind": {"speed": 4.2},
        "weather": [{"main": condition, "description": description}],
    }


# ---------------------------------------------------------------------------
# F17.1: OpenWeatherClient (TTL Cache, Timeout, Error Handling, Mock Fetcher)
# ---------------------------------------------------------------------------


def test_f17_1_client_fetch_and_ttl_caching():
    clock = {"now": 1000.0}
    calls: list[dict[str, Any]] = []

    def fake_fetcher(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        calls.append({"url": url, "params": params, "timeout": timeout})
        return _sample_owm_payload(name="Jakarta", temp=30.0 + len(calls), humidity=75.0)

    client = OpenWeatherClient(
        api_key="owm_secret_key_123456",
        units="metric",
        cache_ttl_seconds=120,
        timeout_seconds=5.0,
        fetcher=fake_fetcher,
        time_fn=lambda: clock["now"],
    )

    # First call -> live fetch
    snap1 = client.get_weather(city="Jakarta")
    assert snap1.location_name == "Jakarta"
    assert snap1.temperature == 31.0
    assert snap1.cached is False
    assert len(calls) == 1
    assert calls[0]["params"]["q"] == "Jakarta"
    assert calls[0]["timeout"] == 5.0

    # Second call within TTL -> cache hit (no HTTP call)
    clock["now"] += 60.0
    snap2 = client.get_weather(city="jakarta")
    assert snap2.cached is True
    assert snap2.temperature == 31.0
    assert len(calls) == 1

    # Force refresh -> bypasses cache
    snap3 = client.get_weather(city="Jakarta", force_refresh=True)
    assert snap3.cached is False
    assert snap3.temperature == 32.0
    assert len(calls) == 2

    # Advance past TTL -> cache expires and fetches fresh
    clock["now"] += 121.0
    snap4 = client.get_weather(city="Jakarta")
    assert snap4.cached is False
    assert snap4.temperature == 33.0
    assert len(calls) == 3


def test_f17_1_client_error_handling_and_stale_fallback():
    clock = {"now": 500.0}
    should_fail = {"mode": None}

    def flaky_fetcher(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if should_fail["mode"] == "timeout":
            raise TimeoutError("connection timed out")
        if should_fail["mode"] == "bad_payload":
            return {"unexpected": True}
        if should_fail["mode"] == "upstream_500":
            raise WeatherClientError("Upstream 500", status_code=500, retryable=True)
        return _sample_owm_payload(name="Bandung", lat=-6.9175, lon=107.6191, temp=24.0)

    # Missing API key raises WeatherAuthError
    no_key_client = OpenWeatherClient(api_key="", fetcher=flaky_fetcher)
    with pytest.raises(WeatherAuthError):
        no_key_client.get_weather(city="Bandung")

    client = OpenWeatherClient(
        api_key="valid_test_key_9999",
        cache_ttl_seconds=60,
        fetcher=flaky_fetcher,
        time_fn=lambda: clock["now"],
    )

    # Missing location raises ValueError
    with pytest.raises(ValueError, match="coordinates or 'city'"):
        client.get_weather()

    # Seed cache
    fresh = client.get_weather(lat=-6.9175, lon=107.6191)
    assert fresh.temperature == 24.0

    # Expire cache and simulate timeout
    clock["now"] += 65.0
    should_fail["mode"] = "timeout"
    with pytest.raises(WeatherTimeoutError):
        client.get_weather(lat=-6.9175, lon=107.6191)

    # With fallback_to_stale=True, returns stale cached snapshot gracefully
    stale = client.get_weather(lat=-6.9175, lon=107.6191, fallback_to_stale=True)
    assert stale.cached is True
    assert stale.temperature == 24.0

    # Malformed upstream JSON raises WeatherClientError
    should_fail["mode"] = "bad_payload"
    with pytest.raises(WeatherClientError, match="missing required"):
        client.get_weather(city="Surabaya")


def test_f17_1_api_key_masking():
    assert mask_api_key("") == ""
    assert mask_api_key(None) == ""
    assert mask_api_key("short") == "*****"
    assert mask_api_key("12345678") == "****5678"
    masked = mask_api_key("owm_live_secret_9876")
    assert masked.startswith("owm")
    assert masked.endswith("9876")
    assert "secret" not in masked
    assert "*" in masked


# ---------------------------------------------------------------------------
# F17.2: Temperature & Humidity Threshold Alert Evaluator (Metric/Imperial)
# ---------------------------------------------------------------------------


def test_f17_2_threshold_evaluator_metric_imperial_and_humidity():
    evaluator = WeatherThresholdEvaluator()
    now = datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc)

    # 38°C == 100.4°F
    assert round(convert_temperature(38.0, "metric", "imperial"), 2) == 100.4
    assert round(convert_temperature(68.0, "imperial", "metric"), 2) == 20.0

    hot_humid_snap = WeatherSnapshot(
        location_name="Jakarta",
        temperature=38.0,
        humidity=88.0,
        condition="Clear",
        units="metric",
    )

    # Metric temp_above -> triggers (38 > 35)
    rule_hot_c = WeatherAlertRule(
        id="r1",
        name="Heat Wave C",
        rule_type=WeatherRuleType.TEMP_ABOVE,
        threshold=35.0,
        units="metric",
        severity="critical",
    )
    ev1 = evaluator.evaluate_rule(rule_hot_c, hot_humid_snap, now=now)
    assert ev1.triggered is True
    assert ev1.current_value == 38.0

    # Imperial temp_above against metric snapshot -> converts 38°C -> 100.4°F > 95°F
    rule_hot_f = WeatherAlertRule(
        id="r2",
        name="Heat Wave F",
        rule_type=WeatherRuleType.TEMP_ABOVE,
        threshold=95.0,
        units="imperial",
    )
    ev2 = evaluator.evaluate_rule(rule_hot_f, hot_humid_snap, now=now)
    assert ev2.triggered is True
    assert ev2.current_value == 100.4

    # temp_below does not trigger at 38°C (threshold 15°C)
    rule_cold = WeatherAlertRule(
        id="r3",
        name="Cold Snap",
        rule_type=WeatherRuleType.TEMP_BELOW,
        threshold=15.0,
        units="metric",
    )
    assert evaluator.evaluate_rule(rule_cold, hot_humid_snap, now=now).triggered is False

    # humidity_above triggers (88 > 80)
    rule_hum_high = WeatherAlertRule(
        id="r4",
        name="High Humidity",
        rule_type=WeatherRuleType.HUMIDITY_ABOVE,
        threshold=80.0,
    )
    assert evaluator.evaluate_rule(rule_hum_high, hot_humid_snap, now=now).triggered is True

    # humidity_below triggers on dry snapshot (25 < 30)
    dry_snap = WeatherSnapshot(
        location_name="Phoenix",
        temperature=60.0,
        humidity=25.0,
        condition="Clear",
        units="imperial",
    )
    rule_hum_low = WeatherAlertRule(
        id="r5",
        name="Dry Air",
        rule_type=WeatherRuleType.HUMIDITY_BELOW,
        threshold=30.0,
    )
    assert evaluator.evaluate_rule(rule_hum_low, dry_snap, now=now).triggered is True

    # Invalid humidity threshold rejected (>100)
    with pytest.raises(ValueError, match="Humidity threshold"):
        WeatherAlertRule(
            name="Invalid Humidity",
            rule_type=WeatherRuleType.HUMIDITY_ABOVE,
            threshold=120.0,
        )


# ---------------------------------------------------------------------------
# F17.3: Dynamic Location Updates Adapter & Coordinate Validation
# ---------------------------------------------------------------------------


def test_f17_3_dynamic_location_updates_and_validation(tmp_path: Path):
    storage = WeatherStorage(tmp_path / "locations.db")
    adapter = LocationUpdateAdapter(storage=storage)

    # Update device location by coordinates
    dev_loc = adapter.update_location(
        entity_id="phone_bima",
        entity_type="device",
        lat=-6.2088,
        lon=106.8456,
        label="Office",
    )
    assert dev_loc.lat == -6.2088
    assert dev_loc.lon == 106.8456
    assert adapter.get_location("phone_bima") is not None

    # Update user location by city name
    user_loc = adapter.update_location(
        entity_id="user_erlangga",
        entity_type="user",
        city="Yogyakarta",
    )
    assert user_loc.city == "Yogyakarta"
    assert len(adapter.list_locations(entity_type="user")) == 1
    assert len(adapter.list_locations()) == 2

    # Boundary coordinates (-90..90, -180..180) are accepted
    pole = adapter.update_location(entity_id="pole_sensor", lat=-90.0, lon=180.0)
    assert pole.lat == -90.0 and pole.lon == 180.0

    # Invalid latitude > 90 or < -90 rejected
    with pytest.raises(ValueError, match="Latitude"):
        adapter.update_location(entity_id="bad1", lat=90.1, lon=0.0)
    with pytest.raises(ValueError, match="Latitude"):
        adapter.update_location(entity_id="bad2", lat=-91.0, lon=0.0)

    # Invalid longitude > 180 or < -180 rejected
    with pytest.raises(ValueError, match="Longitude"):
        adapter.update_location(entity_id="bad3", lat=0.0, lon=180.5)
    with pytest.raises(ValueError, match="Longitude"):
        adapter.update_location(entity_id="bad4", lat=0.0, lon=-181.0)

    # Partial coordinates (lat without lon) rejected
    with pytest.raises(ValueError, match="provided together"):
        adapter.update_location(entity_id="bad5", lat=10.0)

    # Empty location (neither coords nor city) rejected
    with pytest.raises(ValueError, match="coordinates or 'city'"):
        adapter.update_location(entity_id="bad6")


# ---------------------------------------------------------------------------
# F17.4: Weather Condition Change Trigger Evaluator + Cooldown
# ---------------------------------------------------------------------------


def test_f17_4_condition_change_and_cooldown():
    evaluator = ConditionChangeEvaluator()
    t0 = datetime(2026, 11, 21, 14, 0, tzinfo=timezone.utc)

    rule = WeatherAlertRule(
        id="cond_1",
        name="Storm Incoming",
        rule_type=WeatherRuleType.CONDITION_CHANGE,
        from_conditions=["Clear", "Clouds"],
        to_conditions=["Rain", "Storm"],
        cooldown_seconds=600,
        severity="critical",
    )

    # Initial observation (no previous condition) -> does not trigger
    ev_init = evaluator.evaluate_rule(
        rule, previous_condition=None, current_condition="Clear", location_name="Jakarta", now=t0
    )
    assert ev_init.triggered is False

    # Same condition -> does not trigger
    ev_same = evaluator.evaluate_rule(
        rule, previous_condition="Clear", current_condition="Clear", location_name="Jakarta", now=t0
    )
    assert ev_same.triggered is False

    # Clear -> Thunderstorm matches "Storm" alias -> triggers!
    ev_storm = evaluator.evaluate_rule(
        rule,
        previous_condition="Clear",
        current_condition="Thunderstorm",
        location_name="Jakarta",
        now=t0,
    )
    assert ev_storm.triggered is True
    assert ev_storm.suppressed_by_cooldown is False
    assert "Clear -> Thunderstorm" in ev_storm.message

    # Record last_triggered_at and evaluate again within cooldown (5 mins later) -> suppressed!
    rule_with_ts = rule.model_copy(update={"last_triggered_at": t0.isoformat()})
    ev_cooldown = evaluator.evaluate_rule(
        rule_with_ts,
        previous_condition="Clouds",
        current_condition="Rain",
        location_name="Jakarta",
        now=t0 + timedelta(seconds=300),
    )
    assert ev_cooldown.triggered is False
    assert ev_cooldown.suppressed_by_cooldown is True

    # After cooldown expires (601s later) -> triggers again!
    ev_after = evaluator.evaluate_rule(
        rule_with_ts,
        previous_condition="Clouds",
        current_condition="Rain",
        location_name="Jakarta",
        now=t0 + timedelta(seconds=601),
    )
    assert ev_after.triggered is True
    assert ev_after.suppressed_by_cooldown is False


# ---------------------------------------------------------------------------
# F17.5: Migration 0009, Rollback & REST API with JWT Auth
# ---------------------------------------------------------------------------


def test_f17_5_migration_0009_apply_and_rollback(tmp_path: Path):
    db_file = tmp_path / "weather_mig.db"
    store = DatabaseStore(db_path=db_file)

    with store.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '0009_weather_config'"
        ).fetchone()
        assert row is not None
        # Verify tables exist
        for table in (
            "weather_config",
            "weather_locations",
            "weather_alert_rules",
            "weather_condition_state",
        ):
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert tbl is not None

    # Rollback 0009 cleanly
    assert store.rollback_migration("0009_weather_config") is True

    with store.get_connection() as conn:
        for table in (
            "weather_config",
            "weather_locations",
            "weather_alert_rules",
            "weather_condition_state",
        ):
            tbl = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                (table,),
            ).fetchone()
            assert tbl is None


def _build_test_app(tmp_path: Path, fetcher=None) -> FastAPI:
    app = FastAPI(title="Weather Phase 17 Test App")
    app.state.store = DatabaseStore(db_path=tmp_path / "api_weather.db")
    if fetcher is not None:
        app.state.weather_http_fetcher = fetcher
    app.include_router(auth_router)
    app.include_router(weather_router, dependencies=[Depends(get_current_user)])
    return app


def test_f17_5_api_jwt_auth_enforcement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "weather-secret-17")
    app = _build_test_app(tmp_path)
    client = TestClient(app)

    # Protected endpoints must reject unauthenticated calls with 401
    assert client.get("/api/v1/weather/config").status_code == 401
    assert client.put("/api/v1/weather/config", json={"units": "metric"}).status_code == 401
    assert client.put("/api/v1/weather/location", json={"city": "Jakarta"}).status_code == 401
    assert client.get("/api/v1/weather/rules").status_code == 401
    assert client.post("/api/v1/weather/evaluate", json={"city": "Jakarta"}).status_code == 401

    # Obtain JWT token
    login = client.post(
        "/auth/token",
        json={"username": "admin", "password": "weather-secret-17"},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    cfg_resp = client.get("/api/v1/weather/config", headers=headers)
    assert cfg_resp.status_code == 200
    assert cfg_resp.json()["success"] is True


def test_f17_5_full_rest_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nova-weather-pass")

    weather_state = {"temp": 30.0, "humidity": 65.0, "condition": "Clear"}

    def mock_owm_fetcher(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        return _sample_owm_payload(
            name="Jakarta",
            lat=float(params.get("lat", -6.2088)),
            lon=float(params.get("lon", 106.8456)),
            temp=weather_state["temp"],
            humidity=weather_state["humidity"],
            condition=weather_state["condition"],
        )

    app = _build_test_app(tmp_path, fetcher=mock_owm_fetcher)
    client = TestClient(app)

    token = client.post(
        "/auth/token",
        json={"username": "admin", "password": "nova-weather-pass"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. PUT /config with secret API key -> response masks API key
    raw_secret = "owm_live_supersecret_7788"
    put_cfg = client.put(
        "/api/v1/weather/config",
        headers=headers,
        json={
            "api_key": raw_secret,
            "units": "metric",
            "cache_ttl_seconds": 60,
            "timeout_seconds": 8.0,
            "default_city": "Jakarta",
        },
    )
    assert put_cfg.status_code == 200
    cfg_body = put_cfg.json()["config"]
    assert cfg_body["has_api_key"] is True
    assert "api_key" not in cfg_body
    assert raw_secret not in cfg_body["api_key_masked"]
    assert cfg_body["api_key_masked"].endswith("7788")

    # 2. GET /config -> still masked; partial update preserves stored key
    get_cfg = client.get("/api/v1/weather/config", headers=headers)
    assert get_cfg.status_code == 200
    assert get_cfg.json()["config"]["api_key_masked"] == cfg_body["api_key_masked"]

    patch_cfg = client.put(
        "/api/v1/weather/config",
        headers=headers,
        json={"cache_ttl_seconds": 120},
    )
    assert patch_cfg.status_code == 200
    assert patch_cfg.json()["config"]["has_api_key"] is True
    assert patch_cfg.json()["config"]["cache_ttl_seconds"] == 120

    # 3. PUT /location -> valid coordinates & invalid range rejection
    bad_loc = client.put(
        "/api/v1/weather/location",
        headers=headers,
        json={"entity_id": "phone_01", "lat": 95.0, "lon": 106.8},
    )
    assert bad_loc.status_code == 400

    good_loc = client.put(
        "/api/v1/weather/location",
        headers=headers,
        json={
            "entity_id": "phone_01",
            "entity_type": "device",
            "lat": -6.2088,
            "lon": 106.8456,
            "label": "Jakarta HQ",
        },
    )
    assert good_loc.status_code == 200
    assert good_loc.json()["location"]["entity_id"] == "phone_01"

    get_loc = client.get("/api/v1/weather/location", headers=headers, params={"entity_id": "phone_01"})
    assert get_loc.status_code == 200
    assert get_loc.json()["location"]["label"] == "Jakarta HQ"

    # 4. POST /rules -> create threshold & condition_change rules
    r_temp = client.post(
        "/api/v1/weather/rules",
        headers=headers,
        json={
            "name": "High Temp Alert",
            "rule_type": "temp_above",
            "threshold": 35.0,
            "units": "metric",
            "cooldown_seconds": 600,
            "severity": "warning",
        },
    )
    assert r_temp.status_code == 201
    temp_rule_id = r_temp.json()["rule"]["id"]

    r_cond = client.post(
        "/api/v1/weather/rules",
        headers=headers,
        json={
            "name": "Clear to Storm/Rain",
            "rule_type": "condition_change",
            "from_conditions": ["Clear"],
            "to_conditions": ["Rain", "Storm"],
            "cooldown_seconds": 600,
            "severity": "critical",
        },
    )
    assert r_cond.status_code == 201
    cond_rule_id = r_cond.json()["rule"]["id"]

    # Invalid threshold rule (missing threshold) returns 400
    bad_rule = client.post(
        "/api/v1/weather/rules",
        headers=headers,
        json={"name": "Broken Temp", "rule_type": "temp_above"},
    )
    assert bad_rule.status_code == 400

    list_rules = client.get("/api/v1/weather/rules", headers=headers)
    assert list_rules.status_code == 200
    assert list_rules.json()["count"] == 2

    # 5. POST /evaluate — 1st observation (Clear, 30°C) seeds condition state, no alerts
    t0 = datetime(2026, 11, 21, 15, 0, tzinfo=timezone.utc)
    eval1 = client.post(
        "/api/v1/weather/evaluate",
        headers=headers,
        json={"entity_id": "phone_01", "evaluated_at": t0.isoformat()},
    )
    assert eval1.status_code == 200
    res1 = eval1.json()["result"]
    assert res1["snapshot"]["condition"] == "Clear"
    assert res1["triggered_alerts"] == []

    # 6. POST /evaluate — 2nd observation (Thunderstorm, 37°C, force_refresh) triggers BOTH rules!
    weather_state.update({"temp": 37.0, "humidity": 90.0, "condition": "Thunderstorm"})
    eval2 = client.post(
        "/api/v1/weather/evaluate",
        headers=headers,
        json={
            "entity_id": "phone_01",
            "force_refresh": True,
            "evaluated_at": (t0 + timedelta(minutes=2)).isoformat(),
        },
    )
    assert eval2.status_code == 200
    res2 = eval2.json()["result"]
    assert res2["previous_condition"] == "Clear"
    assert res2["condition_changed"] is True
    triggered_ids = {a["rule_id"] for a in res2["triggered_alerts"]}
    assert triggered_ids == {temp_rule_id, cond_rule_id}

    # 7. POST /evaluate — 3rd observation within cooldown (1 min later) -> suppressed!
    eval3 = client.post(
        "/api/v1/weather/evaluate",
        headers=headers,
        json={
            "entity_id": "phone_01",
            "previous_condition": "Clear",
            "snapshot": {
                "location_name": "Jakarta",
                "temperature": 38.0,
                "humidity": 92.0,
                "condition": "Rain",
                "units": "metric",
            },
            "evaluated_at": (t0 + timedelta(minutes=3)).isoformat(),
        },
    )
    assert eval3.status_code == 200
    res3 = eval3.json()["result"]
    assert res3["triggered_alerts"] == []
    suppressed_ids = {a["rule_id"] for a in res3["suppressed_alerts"]}
    assert suppressed_ids == {temp_rule_id, cond_rule_id}

    # 8. DELETE /rules/{rule_id} & verify 404 on second delete
    del1 = client.delete(f"/api/v1/weather/rules/{temp_rule_id}", headers=headers)
    assert del1.status_code == 200
    assert del1.json()["success"] is True

    del2 = client.delete(f"/api/v1/weather/rules/{temp_rule_id}", headers=headers)
    assert del2.status_code == 404
