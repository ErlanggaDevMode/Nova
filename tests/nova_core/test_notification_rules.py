"""Comprehensive tests for Phase 16: Advanced Notification Rules (F16.1–F16.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

from nova_core.api.routes_auth import router as auth_router
from nova_core.api.routes_notifications import router as notifications_router
from nova_core.auth import get_current_user
from nova_core.automation.notification_rules import (
    MAX_INPUT_TEXT_LENGTH,
    MAX_REGEX_PATTERN_LENGTH,
    AlertLevel,
    NotificationPayload,
    NotificationRule,
    NotificationRuleEngine,
    NotificationStorage,
    PackageWhitelistChecker,
    PackageWhitelistConfig,
    SilentScheduleConfig,
    SilentScheduleEvaluator,
    SilentScheduleWindow,
    package_matches_pattern,
    safe_regex_search,
    validate_regex_pattern,
)
from nova_core.db.store import DatabaseStore


# ---------------------------------------------------------------------------
# F16.1: RegEx Matching, Validation & Input Length Guardrails
# ---------------------------------------------------------------------------


def test_f16_1_regex_matching_title_body_sender():
    engine = NotificationRuleEngine(use_default_heuristics=False)
    engine.add_rule(
        NotificationRule(
            name="Bank OTP Regex",
            title_regex=r"bank\s+alert",
            body_regex=r"\b\d{6}\b",
            sender_regex=r"^(BCA|Mandiri)$",
            alert_level=AlertLevel.HIGH,
            priority_score=90,
        )
    )

    matched = engine.evaluate(
        NotificationPayload(
            package_name="com.bank.mobile",
            title="Bank Alert - Login",
            body="Your verification code is 482910. Do not share.",
            sender="BCA",
        ),
        record_history=False,
    )
    assert matched.alert_level == AlertLevel.HIGH
    assert matched.priority_score == 90
    assert matched.matched_rule_names == ["Bank OTP Regex"]
    assert {m.field for m in matched.regex_matches} == {"title", "body", "sender"}
    assert any(m.field == "body" and m.matched_text == "482910" for m in matched.regex_matches)

    # Sender mismatch -> should not match rule
    unmatched = engine.evaluate(
        NotificationPayload(
            package_name="com.bank.mobile",
            title="Bank Alert - Login",
            body="Your verification code is 482910.",
            sender="UnknownSender",
        ),
        record_history=False,
    )
    assert unmatched.matched_rule_names == []
    assert unmatched.alert_level == AlertLevel.NORMAL


def test_f16_1_regex_case_sensitivity_and_validation():
    # Invalid syntax
    with pytest.raises(ValueError, match="Invalid regular expression"):
        validate_regex_pattern(r"[unclosed")

    # Exceeds max pattern length
    with pytest.raises(ValueError, match="maximum allowed length"):
        validate_regex_pattern("a" * (MAX_REGEX_PATTERN_LENGTH + 1))

    # ReDoS nested quantifier rejected
    with pytest.raises(ValueError, match="nested quantifiers"):
        validate_regex_pattern(r"(a+)+$")

    # ReDoS overlapping wildcards rejected
    with pytest.raises(ValueError, match="wildcards"):
        validate_regex_pattern(r".*.*foo")

    # Case sensitivity flag
    res_insensitive = safe_regex_search(r"URGENT", "urgent task", case_sensitive=False)
    assert res_insensitive.matched is True
    res_sensitive = safe_regex_search(r"URGENT", "urgent task", case_sensitive=True)
    assert res_sensitive.matched is False


def test_f16_1_input_length_protection():
    oversized_body = ("x" * (MAX_INPUT_TEXT_LENGTH + 500)) + "SECRET_TOKEN_999"
    outcome = safe_regex_search(r"SECRET_TOKEN_999", oversized_body, field="body")
    assert outcome.input_truncated is True
    assert outcome.matched is False

    # Token within safe prefix matches cleanly while still flagging truncation
    prefix_match_body = "SAFE_TOKEN_123 " + ("y" * (MAX_INPUT_TEXT_LENGTH + 200))
    outcome2 = safe_regex_search(r"SAFE_TOKEN_123", prefix_match_body, field="body")
    assert outcome2.input_truncated is True
    assert outcome2.matched is True
    assert outcome2.matched_text == "SAFE_TOKEN_123"


# ---------------------------------------------------------------------------
# F16.2: App Package Source Whitelisting
# ---------------------------------------------------------------------------


def test_f16_2_package_pattern_matching_modes():
    assert package_matches_pattern("com.whatsapp", "com.whatsapp")
    assert package_matches_pattern("COM.WHATSAPP", "com.whatsapp")
    assert package_matches_pattern("org.telegram.messenger", "org.telegram.*")
    assert package_matches_pattern("com.google.android.gm", "com.google.android.")
    assert package_matches_pattern("com.slack.enterprise", "prefix:com.slack")
    assert not package_matches_pattern("com.tiktok.android", "com.whatsapp")


def test_f16_2_strict_vs_allow_all_and_blocklist():
    strict_checker = PackageWhitelistChecker(
        PackageWhitelistConfig(
            mode="strict",
            allowed_packages=["com.whatsapp", "org.telegram.*", "com.google.android."],
            blocked_packages=["com.google.android.youtube"],
        )
    )
    assert strict_checker.check("com.whatsapp").allowed is True
    assert strict_checker.check("org.telegram.messenger").allowed is True
    assert strict_checker.check("com.google.android.gm").allowed is True
    # Explicit blocklist overrides whitelist
    blocked = strict_checker.check("com.google.android.youtube")
    assert blocked.allowed is False
    assert "blocked" in blocked.reason.lower()
    # Non-whitelisted rejected in strict mode
    denied = strict_checker.check("com.random.spamapp")
    assert denied.allowed is False

    # Allow-all mode allows unknown apps unless explicitly blocked
    allow_all_checker = PackageWhitelistChecker(
        PackageWhitelistConfig(
            mode="allow_all",
            blocked_packages=["com.spam.*"],
        )
    )
    assert allow_all_checker.check("com.any.app").allowed is True
    assert allow_all_checker.check("com.spam.ads").allowed is False


def test_f16_2_engine_blocks_non_whitelisted_package():
    engine = NotificationRuleEngine(
        whitelist_config=PackageWhitelistConfig(
            mode="strict",
            allowed_packages=["com.whatsapp"],
        )
    )
    result = engine.evaluate(
        NotificationPayload(
            package_name="com.untrusted.promo",
            title="EMERGENCY FIRE",
            body="Fake urgent alert",
        ),
        record_history=False,
    )
    assert result.whitelisted is False
    assert result.alert_level == AlertLevel.IGNORED
    assert result.priority_score == 0
    assert result.deliver is False


# ---------------------------------------------------------------------------
# F16.3: Notification Alert Classification & Priority Scoring
# ---------------------------------------------------------------------------


def test_f16_3_alert_classification_priority_and_stop_on_match():
    engine = NotificationRuleEngine(use_default_heuristics=False)
    engine.add_rule(
        NotificationRule(
            name="WhatsApp Normal",
            package_patterns=["com.whatsapp"],
            alert_level=AlertLevel.NORMAL,
            priority_score=40,
        )
    )
    engine.add_rule(
        NotificationRule(
            name="VIP Boss Critical",
            package_patterns=["com.whatsapp"],
            sender_regex=r"CEO|CTO",
            keywords=["production", "outage"],
            alert_level=AlertLevel.CRITICAL,
            priority_score=120,
            stop_on_match=True,
        )
    )
    engine.add_rule(
        NotificationRule(
            name="Mute Group Noise",
            package_patterns=["com.whatsapp"],
            title_regex=r"Random Meme Group",
            alert_level=AlertLevel.IGNORED,
            priority_score=80,
        )
    )

    # Highest priority rule matches and stops evaluation
    res_vip = engine.evaluate(
        NotificationPayload(
            package_name="com.whatsapp",
            sender="CTO",
            title="Server",
            body="Production outage on eu-west cluster!",
        ),
        record_history=False,
    )
    assert res_vip.alert_level == AlertLevel.CRITICAL
    assert res_vip.priority_score == 120
    assert res_vip.matched_rule_names == ["VIP Boss Critical"]
    assert res_vip.deliver is True

    # Higher-priority ignored rule overrides lower-priority normal rule
    res_muted = engine.evaluate(
        NotificationPayload(
            package_name="com.whatsapp",
            sender="Friend",
            title="Random Meme Group",
            body="Check out this meme",
        ),
        record_history=False,
    )
    assert res_muted.alert_level == AlertLevel.IGNORED
    assert res_muted.deliver is False
    assert "Mute Group Noise" in res_muted.matched_rule_names


def test_f16_3_default_heuristics_classification():
    engine = NotificationRuleEngine(use_default_heuristics=True)

    crit = engine.evaluate(
        NotificationPayload(
            package_name="com.home.security",
            title="Security Breach",
            body="Front door sensor triggered",
        ),
        record_history=False,
    )
    assert crit.alert_level == AlertLevel.CRITICAL

    high = engine.evaluate(
        NotificationPayload(
            package_name="com.app.auth",
            title="Login",
            body="Your OTP is 123456",
        ),
        record_history=False,
    )
    assert high.alert_level == AlertLevel.HIGH

    low = engine.evaluate(
        NotificationPayload(
            package_name="com.shop.app",
            title="Flash Sale 90% Diskon!",
            body="Promo spesial hari ini",
        ),
        record_history=False,
    )
    assert low.alert_level == AlertLevel.LOW

    normal = engine.evaluate(
        NotificationPayload(
            package_name="com.whatsapp",
            title="Budi",
            body="Lunch at 12?",
        ),
        record_history=False,
    )
    assert normal.alert_level == AlertLevel.NORMAL


# ---------------------------------------------------------------------------
# F16.4: Silent Notification / Do Not Disturb Schedule
# ---------------------------------------------------------------------------


def test_f16_4_overnight_dnd_timezone_and_day_rollover():
    evaluator = SilentScheduleEvaluator()
    # Weeknights 22:00 - 07:00 Asia/Jakarta (UTC+7)
    config = SilentScheduleConfig(
        enabled=True,
        start_time="22:00",
        end_time="07:00",
        days_of_week=["mon", "tue", "wed", "thu", "fri"],
        timezone="Asia/Jakarta",
        allow_critical_bypass=True,
    )

    # Monday 2026-11-23 16:30 UTC == Monday 23:30 WIB -> inside overnight window
    mon_night = datetime(2026, 11, 23, 16, 30, tzinfo=timezone.utc)
    res_mon = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=mon_night)
    assert res_mon.in_silent_window is True
    assert res_mon.silenced is True
    assert res_mon.bypassed is False

    # Critical alert during Monday 23:30 WIB -> bypasses silent mode
    res_crit = evaluator.evaluate(config, alert_level=AlertLevel.CRITICAL, at_time=mon_night)
    assert res_crit.in_silent_window is True
    assert res_crit.silenced is False
    assert res_crit.bypassed is True

    # Saturday 2026-11-28 03:00 WIB == Friday 2026-11-27 20:00 UTC
    # Overnight window started Friday 22:00 (Friday is in days_of_week) -> SILENCED!
    sat_early_morning = datetime(2026, 11, 27, 20, 0, tzinfo=timezone.utc)
    res_sat = evaluator.evaluate(config, alert_level=AlertLevel.HIGH, at_time=sat_early_morning)
    assert res_sat.in_silent_window is True
    assert res_sat.silenced is True

    # Sunday 2026-11-29 03:00 WIB == Saturday 2026-11-28 20:00 UTC
    # Started Saturday 22:00 (Saturday NOT in days_of_week) -> NOT silenced!
    sun_early_morning = datetime(2026, 11, 28, 20, 0, tzinfo=timezone.utc)
    res_sun = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=sun_early_morning)
    assert res_sun.in_silent_window is False
    assert res_sun.silenced is False

    # Tuesday 2026-11-24 10:00 WIB == Tuesday 03:00 UTC -> daytime outside DND
    tue_day = datetime(2026, 11, 24, 3, 0, tzinfo=timezone.utc)
    res_day = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=tue_day)
    assert res_day.in_silent_window is False
    assert res_day.silenced is False


def test_f16_4_extra_windows_bypass_options_and_validation():
    evaluator = SilentScheduleEvaluator()
    config = SilentScheduleConfig(
        enabled=True,
        start_time="23:00",
        end_time="06:00",
        days_of_week=[0, 1, 2, 3, 4],  # integer day indices supported
        timezone="UTC",
        allow_critical_bypass=False,
        min_bypass_score=200,
        extra_windows=[
            SilentScheduleWindow(
                start_time="13:00",
                end_time="14:00",
                days_of_week=["wed"],
                label="Focus Hour",
            )
        ],
    )

    # Wednesday 13:30 UTC hits extra window; critical does NOT bypass because allow_critical_bypass=False
    wed_focus = datetime(2026, 11, 25, 13, 30, tzinfo=timezone.utc)
    res_no_bypass = evaluator.evaluate(
        config, alert_level=AlertLevel.CRITICAL, priority_score=100, at_time=wed_focus
    )
    assert res_no_bypass.silenced is True

    # Score >= min_bypass_score bypasses
    res_score_bypass = evaluator.evaluate(
        config, alert_level=AlertLevel.HIGH, priority_score=250, at_time=wed_focus
    )
    assert res_score_bypass.silenced is False
    assert res_score_bypass.bypassed is True

    # Explicit rule bypasses
    res_rule_bypass = evaluator.evaluate(
        config, alert_level=AlertLevel.NORMAL, priority_score=50, rule_bypass_silent=True, at_time=wed_focus
    )
    assert res_rule_bypass.silenced is False
    assert res_rule_bypass.bypassed is True

    # Validation errors
    with pytest.raises(ValueError, match="Unknown IANA timezone"):
        SilentScheduleConfig(timezone="Mars/Olympus_Mons")
    with pytest.raises(ValueError, match="out of range"):
        SilentScheduleConfig(start_time="25:99")
    with pytest.raises(ValueError, match="Invalid time format"):
        SilentScheduleConfig(start_time="9am")


# ---------------------------------------------------------------------------
# F16.5: Migration 0008, Rollback, Standalone Storage & REST API + Auth
# ---------------------------------------------------------------------------


def test_f16_5_migration_0008_apply_and_rollback(tmp_path: Path):
    db_file = tmp_path / "test_mig_0008.db"
    store = DatabaseStore(db_path=db_file)

    # Verify migration 0008 is applied and recorded in schema_migrations
    with store.get_connection() as conn:
        mig_row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = '0008_notification_history'"
        ).fetchone()
        assert mig_row is not None

        tables = {
            dict(r)["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {"notification_history", "notification_rules", "notification_settings"} <= tables

    # Rollback migration 0008 cleanly
    assert store.rollback_migration("0008_notification_history") is True

    with store.get_connection() as conn:
        mig_row = conn.execute(
            "SELECT version FROM schema_migrations WHERE version = '0008_notification_history'"
        ).fetchone()
        assert mig_row is None

        tables = {
            dict(r)["name"]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert "notification_history" not in tables
        assert "notification_rules" not in tables
        assert "notification_settings" not in tables


def test_f16_5_standalone_sqlite_storage(tmp_path: Path):
    raw_sqlite_path = tmp_path / "standalone_notifications.db"
    storage = NotificationStorage(raw_sqlite_path)
    engine = NotificationRuleEngine(storage=storage)

    rule = engine.add_rule(
        NotificationRule(
            name="Telegram Alerts",
            package_patterns=["org.telegram.*"],
            body_regex=r"deploy\s+failed",
            alert_level=AlertLevel.CRITICAL,
            priority_score=110,
        )
    )
    assert rule.id is not None

    eval_res = engine.evaluate(
        NotificationPayload(
            source_device_id="android_pixel",
            package_name="org.telegram.messenger",
            sender="CI Bot",
            title="Pipeline",
            body="Deploy failed on main branch",
        )
    )
    assert eval_res.history_id is not None
    assert eval_res.alert_level == AlertLevel.CRITICAL

    history = storage.list_history(package_name="org.telegram.messenger")
    assert len(history) == 1
    assert history[0]["alert_level"] == "critical"
    assert history[0]["matched_rule_names"] == ["Telegram Alerts"]


def _build_test_app(tmp_path: Path) -> FastAPI:
    app = FastAPI(title="Notification Rules Test App")
    app.state.store = DatabaseStore(db_path=tmp_path / "api_notifications.db")
    app.include_router(auth_router)
    app.include_router(notifications_router, dependencies=[Depends(get_current_user)])
    return app


def test_f16_5_api_auth_enforcement(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "supersecret-phase16")
    app = _build_test_app(tmp_path)
    client = TestClient(app)

    # All protected notification endpoints require JWT auth
    assert client.get("/api/v1/notifications/history").status_code == 401
    assert client.get("/api/v1/notifications/rules").status_code == 401
    assert client.get("/api/v1/notifications/silent-schedule").status_code == 401
    assert (
        client.post(
            "/api/v1/notifications/ingest",
            json={"package_name": "com.whatsapp", "title": "Hi", "body": "Test"},
        ).status_code
        == 401
    )

    # Obtain valid JWT via /auth/token
    login = client.post(
        "/auth/token",
        json={"username": "admin", "password": "supersecret-phase16"},
    )
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    res = client.get("/api/v1/notifications/rules", headers=headers)
    assert res.status_code == 200
    assert res.json()["success"] is True


def test_f16_5_full_rest_workflow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nova-test-pass")
    app = _build_test_app(tmp_path)
    client = TestClient(app)

    token = client.post(
        "/auth/token",
        json={"username": "admin", "password": "nova-test-pass"},
    ).json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # 1. Configure strict package whitelist
    wl_resp = client.put(
        "/api/v1/notifications/whitelist",
        headers=headers,
        json={
            "mode": "strict",
            "allowed_packages": ["com.whatsapp", "org.telegram.*"],
            "blocked_packages": ["com.ads.*"],
        },
    )
    assert wl_resp.status_code == 200
    assert wl_resp.json()["whitelist"]["mode"] == "strict"

    wl_get = client.get("/api/v1/notifications/whitelist", headers=headers)
    assert wl_get.status_code == 200
    assert "com.whatsapp" in wl_get.json()["whitelist"]["allowed_packages"]

    # 2. Configure silent schedule (22:00-07:00 Asia/Jakarta with critical bypass)
    dnd_resp = client.put(
        "/api/v1/notifications/silent-schedule",
        headers=headers,
        json={
            "enabled": True,
            "start_time": "22:00",
            "end_time": "07:00",
            "days_of_week": ["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
            "timezone": "Asia/Jakarta",
            "allow_critical_bypass": True,
        },
    )
    assert dnd_resp.status_code == 200
    assert dnd_resp.json()["schedule"]["timezone"] == "Asia/Jakarta"

    # Invalid timezone returns 400
    bad_tz = client.put(
        "/api/v1/notifications/silent-schedule",
        headers=headers,
        json={"enabled": True, "timezone": "Invalid/Zone"},
    )
    assert bad_tz.status_code == 400

    dnd_get = client.get("/api/v1/notifications/silent-schedule", headers=headers)
    assert dnd_get.status_code == 200
    assert dnd_get.json()["schedule"]["enabled"] is True

    # 3. Create classification rules (plus verify invalid regex returns 400)
    bad_rule = client.post(
        "/api/v1/notifications/rules",
        headers=headers,
        json={"name": "Bad ReDoS", "body_regex": "(a+)+$"},
    )
    assert bad_rule.status_code == 400

    rule_crit = client.post(
        "/api/v1/notifications/rules",
        headers=headers,
        json={
            "name": "Server Down Telegram",
            "package_patterns": ["org.telegram.*"],
            "body_regex": r"prod-\d+\s+down",
            "alert_level": "critical",
            "priority_score": 100,
        },
    )
    assert rule_crit.status_code == 201
    rule_id = rule_crit.json()["rule"]["id"]

    rules_list = client.get("/api/v1/notifications/rules", headers=headers)
    assert rules_list.status_code == 200
    assert rules_list.json()["count"] == 1

    # 4. Ingest notifications at 23:15 Asia/Jakarta (16:15 UTC) — inside DND window!
    night_ts = "2026-11-23T16:15:00+00:00"

    # 4a. Normal WhatsApp message during DND -> silenced
    ingest_normal = client.post(
        "/api/v1/notifications/ingest",
        headers=headers,
        json={
            "source_device_id": "phone_01",
            "package_name": "com.whatsapp",
            "sender": "Rina",
            "title": "Rina",
            "body": "Besok jadi meeting jam 10?",
            "received_at": night_ts,
        },
    )
    assert ingest_normal.status_code == 200
    r1 = ingest_normal.json()["result"]
    assert r1["whitelisted"] is True
    assert r1["alert_level"] == "normal"
    assert r1["silenced"] is True
    assert r1["deliver"] is False
    assert r1["history_id"] is not None

    # 4b. Critical Telegram regex alert during DND -> bypasses silent mode!
    ingest_crit = client.post(
        "/api/v1/notifications/ingest",
        headers=headers,
        json={
            "source_device_id": "phone_01",
            "package_name": "org.telegram.messenger",
            "sender": "MonitorBot",
            "title": "Alert",
            "body": "Cluster prod-01 down immediately!",
            "received_at": night_ts,
        },
    )
    assert ingest_crit.status_code == 200
    r2 = ingest_crit.json()["result"]
    assert r2["alert_level"] == "critical"
    assert r2["silenced"] is False
    assert r2["bypassed_silent"] is True
    assert r2["deliver"] is True
    assert r2["matched_rule_ids"] == [rule_id]

    # 4c. Non-whitelisted package -> ignored
    ingest_blocked = client.post(
        "/api/v1/notifications/ingest",
        headers=headers,
        json={
            "source_device_id": "phone_01",
            "package_name": "com.random.game",
            "title": "Free coins!",
            "body": "Play now!",
            "received_at": night_ts,
        },
    )
    assert ingest_blocked.status_code == 200
    r3 = ingest_blocked.json()["result"]
    assert r3["whitelisted"] is False
    assert r3["alert_level"] == "ignored"
    assert r3["deliver"] is False

    # 5. Query history with filters & stats
    hist_all = client.get("/api/v1/notifications/history", headers=headers)
    assert hist_all.status_code == 200
    hist_data = hist_all.json()
    assert hist_data["count"] == 3
    assert hist_data["stats"]["total"] == 3
    assert hist_data["stats"]["silenced_count"] == 1
    assert hist_data["stats"]["bypassed_silent_count"] == 1
    assert hist_data["stats"]["blocked_whitelist_count"] == 1
    assert hist_data["stats"]["delivered_count"] == 1

    hist_crit = client.get(
        "/api/v1/notifications/history",
        headers=headers,
        params={"alert_level": "critical"},
    )
    assert hist_crit.status_code == 200
    assert hist_crit.json()["count"] == 1
    assert hist_crit.json()["items"][0]["package_name"] == "org.telegram.messenger"

    hist_silenced = client.get(
        "/api/v1/notifications/history",
        headers=headers,
        params={"silenced": "true"},
    )
    assert hist_silenced.status_code == 200
    assert hist_silenced.json()["count"] == 1
    assert hist_silenced.json()["items"][0]["package_name"] == "com.whatsapp"

    # 6. Delete rule (and verify 404 on second delete)
    del_resp = client.delete(f"/api/v1/notifications/rules/{rule_id}", headers=headers)
    assert del_resp.status_code == 200
    assert del_resp.json()["success"] is True

    del_missing = client.delete(f"/api/v1/notifications/rules/{rule_id}", headers=headers)
    assert del_missing.status_code == 404
