"""
QA Audit & Edge-Case Regression Test Suite — Phase 15 & Phase 16.
Author: Dina (QA Engineer & Code Reviewer)

Focus areas:
- Phase 15 (Cron Scheduler):
  1. Invalid cron syntax (out-of-bounds, invalid step, reversed ranges, invalid macros, non-string, malformed fields)
  2. Leap Year 29 February evaluation (scheduling on 29 Feb jumps to next leap year 2028, skips non-leap years 2025/2026/2027)
  3. DST spring-forward gap (non-existent wall-clock times shifted forward or rejected) & fall-back fold (ambiguous hours fold 0/1)
  4. Execution lock manager: overlap prevention, lock TTL expiration, and stolen-token release protection
  5. JWT 401 Unauthorized enforcement across all /api/v1/scheduler/* endpoints (unauthenticated & tampered token)

- Phase 16 (Notification Rules):
  6. ReDoS regex rejection (nested quantifiers, adjacent wildcards, length limit)
  7. App package source whitelisting: strict mode vs blocked packages, precedence of blocklist over whitelist
  8. Overnight DND schedule boundary evaluation (21:59 outside, 22:00 inside, 23:59 inside, 00:00 inside, 00:01 inside, 06:59 inside, 07:00 outside)
  9. Critical alert bypass during active DND windows, score bypass threshold, and rule-level bypass toggle
  10. JWT 401 Unauthorized enforcement across all /api/v1/notifications/* endpoints (unauthenticated & tampered token)
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from nova_core.automation.cron_scheduler import (
    CronExpression,
    CronParseError,
    CronScheduler,
    ExecutionLockManager,
    TimezoneError,
    from_utc,
    get_timezone_offset_info,
    next_run_at,
    parse_cron,
    resolve_local_datetime,
    to_utc,
    validate_timezone,
)
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
from nova_core.main import app


# ===========================================================================
# 1. Phase 15: Invalid Cron Syntax Edge Cases
# ===========================================================================


@pytest.mark.parametrize(
    "invalid_expr, expected_error_substr",
    [
        # Out-of-bounds minute
        ("60 * * * *", "out of bounds"),
        ("-1 * * * *", "Invalid range"),
        ("99 * * * *", "out of bounds"),
        # Out-of-bounds hour
        ("* 24 * * *", "out of bounds"),
        ("* -1 * * *", "Invalid range"),
        # Out-of-bounds day of month
        ("* * 0 * *", "out of bounds"),
        ("* * 32 * *", "out of bounds"),
        # Out-of-bounds month
        ("* * * 0 *", "out of bounds"),
        ("* * * 13 *", "out of bounds"),
        ("* * * INVALID_MONTH *", "Invalid cron value"),
        # Out-of-bounds day of week
        ("* * * * 8", "out of bounds"),
        ("* * * * -1", "Invalid range"),
        ("* * * * FOO_DAY", "Invalid cron value"),
        # Invalid step syntax
        ("*/0 * * * *", "Step must be > 0"),
        ("*/-1 * * * *", "Step must be > 0"),
        ("1-10/0 * * * *", "Step must be > 0"),
        ("5/ * * * *", "Invalid step syntax"),
        # Reversed ranges
        ("10-5 * * * *", "Reversed range"),
        ("23-1 * * * *", "Reversed range"),
        ("20-10 * * * *", "Reversed range"),
        ("* * * * FRI-MON", "Reversed range"),
        ("* * * DEC-JAN *", "Reversed range"),
        # Malformed list elements
        ("1,,2 * * * *", "Empty list element"),
        (",1 * * * *", "Empty list element"),
        ("1, * * * *", "Empty list element"),
        # Field count errors
        ("", "Cron expression must be a non-empty string"),
        ("   ", "Cron expression must be a non-empty string"),
        ("* * *", "Expected 5 cron fields"),
        ("* * * *", "Expected 5 cron fields"),
        ("* * * * * *", "Expected 5 cron fields"),
        # Invalid macros
        ("@invalid_macro", "Expected 5 cron fields"),
        ("@every_minute", "Expected 5 cron fields"),
    ],
)
def test_qa_phase15_cron_invalid_syntax_variations(invalid_expr: str, expected_error_substr: str):
    """Ensure invalid cron expressions fail fast with clear CronParseError."""
    with pytest.raises(CronParseError) as exc_info:
        parse_cron(invalid_expr)
    assert expected_error_substr.lower() in str(exc_info.value).lower()


def test_qa_phase15_cron_non_string_type_rejection():
    """Ensure parse_cron rejects non-string inputs safely."""
    for bad_val in (None, 12345, 3.14, ["*", "*", "*", "*", "*"]):
        with pytest.raises(CronParseError):
            parse_cron(bad_val)  # type: ignore[arg-type]


# ===========================================================================
# 2. Phase 15: Leap Year (29 February) Evaluation
# ===========================================================================


def test_qa_phase15_cron_leap_year_february_29():
    """
    Ensure cron expression targeting February 29 ('0 12 29 2 *'):
    1. Correctly identifies leap year 2028 (skipping non-leap years 2025, 2026, 2027).
    2. Correctly matches on 2028-02-29 12:00 UTC and rejects non-leap years.
    3. Computes successive leap runs (2028 then 2032).
    """
    cron = parse_cron("0 12 29 2 *")

    # Evaluated from non-leap year (e.g. November 2026)
    base_2026 = datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    nxt_2028 = cron.next_run_at(after=base_2026, tz="UTC")
    assert nxt_2028 == datetime(2028, 2, 29, 12, 0, 0, tzinfo=timezone.utc)

    # Evaluated immediately after a leap day (2024-03-01) -> must skip to 2028
    base_2024 = datetime(2024, 3, 1, 0, 0, 0, tzinfo=timezone.utc)
    nxt_after_leap = cron.next_run_at(after=base_2024, tz="UTC")
    assert nxt_after_leap == datetime(2028, 2, 29, 12, 0, 0, tzinfo=timezone.utc)

    # Next 2 runs should be 2028 and 2032
    two_runs = cron.next_n_runs(2, after=base_2026, tz="UTC")
    assert two_runs == [
        datetime(2028, 2, 29, 12, 0, 0, tzinfo=timezone.utc),
        datetime(2032, 2, 29, 12, 0, 0, tzinfo=timezone.utc),
    ]

    # Verify matches logic
    assert cron.matches(datetime(2028, 2, 29, 12, 0, 0, tzinfo=timezone.utc)) is True
    # Non-leap year 2027 does not match on Feb 28
    assert cron.matches(datetime(2027, 2, 28, 12, 0, 0, tzinfo=timezone.utc)) is False
    # Non-leap year 2026 does not match on Feb 28
    assert cron.matches(datetime(2026, 2, 28, 12, 0, 0, tzinfo=timezone.utc)) is False
    # Leap year 2028 does not match on Feb 28
    assert cron.matches(datetime(2028, 2, 28, 12, 0, 0, tzinfo=timezone.utc)) is False


# ===========================================================================
# 3. Phase 15: DST Spring-Forward Gap & Fall-Back Fold Handling
# ===========================================================================


def test_qa_phase15_dst_spring_forward_gap():
    """
    Ensure DST spring-forward (gap / non-existent local wall clock time) is handled correctly:
    In America/New_York, 2026-03-08 jumps from 01:59:59 EST (-05:00) to 03:00:00 EDT (-04:00).
    Local time 02:30:00 does not exist.
    """
    gap_time = datetime(2026, 3, 8, 2, 30, 0)

    # shift_forward resolves to the shifted post-gap local instant (03:30 EDT)
    shifted = resolve_local_datetime(gap_time, "America/New_York", non_existent_strategy="shift_forward")
    assert shifted.transition_type == "non_existent"
    assert shifted.adjusted is True
    assert shifted.local_dt.hour == 3
    assert shifted.local_dt.minute == 30
    assert shifted.is_dst is True
    assert shifted.utc_offset_seconds == -4 * 3600
    assert shifted.utc_dt == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)

    # raise strategy properly raises TimezoneError
    with pytest.raises(TimezoneError) as exc_info:
        resolve_local_datetime(gap_time, "America/New_York", non_existent_strategy="raise")
    assert "does not exist" in str(exc_info.value)

    # next_run_at with a cron scheduled during the gap shifts cleanly
    before_gap = datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)  # 01:00 EST
    nxt_run = next_run_at("30 2 * * *", after=before_gap, tz="America/New_York")
    assert nxt_run.hour == 3 and nxt_run.minute == 30
    assert nxt_run.astimezone(timezone.utc) == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)


def test_qa_phase15_dst_fall_back_ambiguous_fold():
    """
    Ensure DST fall-back (ambiguous fold / repeated local wall clock) is handled properly:
    In America/New_York, 2026-11-01 01:30:00 occurs twice:
    - First pass: EDT (-04:00, fold=0, 05:30 UTC)
    - Second pass: EST (-05:00, fold=1, 06:30 UTC)
    """
    ambig_time = datetime(2026, 11, 1, 1, 30, 0)

    # prefer_first selects earlier UTC instant (fold=0, EDT)
    first = resolve_local_datetime(ambig_time, "America/New_York", ambiguous_strategy="prefer_first")
    assert first.transition_type == "ambiguous"
    assert first.fold == 0
    assert first.is_dst is True
    assert first.utc_offset_seconds == -4 * 3600
    assert first.utc_dt == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)

    # prefer_second selects later UTC instant (fold=1, EST)
    second = resolve_local_datetime(ambig_time, "America/New_York", ambiguous_strategy="prefer_second")
    assert second.transition_type == "ambiguous"
    assert second.fold == 1
    assert second.is_dst is False
    assert second.utc_offset_seconds == -5 * 3600
    assert second.utc_dt == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)

    # raise strategy raises TimezoneError
    with pytest.raises(TimezoneError) as exc_info:
        resolve_local_datetime(ambig_time, "America/New_York", ambiguous_strategy="raise")
    assert "is ambiguous" in str(exc_info.value)


# ===========================================================================
# 4. Phase 15: Lock Expiry & Stolen-Token Release Protection
# ===========================================================================


def test_qa_phase15_lock_manager_expiry_and_stolen_token_protection():
    """
    Ensure ExecutionLockManager:
    1. Prevents concurrent overlap for the same rule.
    2. Protects against stolen/tampered tokens attempting release_lock or extend_lock.
    3. Automatically evicts expired locks when TTL elapses.
    """
    simulated_clock = [5000.0]
    locks = ExecutionLockManager(clock=lambda: simulated_clock[0])

    # 1. Worker Alpha acquires lock
    res_a = locks.acquire_lock("rule_critical_sync", owner_id="worker_alpha", ttl_seconds=30.0)
    assert res_a.acquired is True
    token_a = res_a.token
    assert token_a is not None
    assert locks.is_locked("rule_critical_sync") is True

    # 2. Worker Beta tries to acquire -> rejected
    res_b = locks.acquire_lock("rule_critical_sync", owner_id="worker_beta", ttl_seconds=30.0)
    assert res_b.acquired is False
    assert "already locked" in (res_b.reason or "")

    # 3. Stolen-token release protection: Worker Beta tries to release using a stolen/fake token
    assert locks.release_lock("rule_critical_sync", token="fake_stolen_token_xyz") is False
    # Lock MUST remain held by Worker Alpha
    assert locks.is_locked("rule_critical_sync") is True
    info = locks.get_lock_info("rule_critical_sync")
    assert info is not None
    assert info["owner_id"] == "worker_alpha"

    # 4. Stolen-token extend protection: Worker Beta tries to extend using a fake token
    assert locks.extend_lock("rule_critical_sync", token="fake_stolen_token_xyz", ttl_seconds=60.0) is False

    # 5. Legitimate extend by Worker Alpha
    assert locks.extend_lock("rule_critical_sync", token=token_a, ttl_seconds=45.0) is True
    info_ext = locks.get_lock_info("rule_critical_sync")
    assert info_ext is not None
    assert info_ext["remaining_seconds"] == 45.0

    # 6. Lock TTL Expiry: advance clock past expiration
    simulated_clock[0] += 46.0
    assert locks.is_locked("rule_critical_sync") is False
    assert locks.get_lock_info("rule_critical_sync") is None

    # 7. Worker Alpha's old token can no longer release expired lock
    assert locks.release_lock("rule_critical_sync", token=token_a) is False

    # 8. Worker Beta can now acquire the freed lock
    res_b2 = locks.acquire_lock("rule_critical_sync", owner_id="worker_beta", ttl_seconds=20.0)
    assert res_b2.acquired is True
    assert locks.release_lock("rule_critical_sync", token=res_b2.token) is True
    assert locks.is_locked("rule_critical_sync") is False


# ===========================================================================
# 5. Phase 15: JWT 401 Unauthorized Security on /api/v1/scheduler/*
# ===========================================================================


def test_qa_phase15_api_scheduler_endpoints_jwt_401_enforcement(tmp_path, monkeypatch):
    """
    CRITICAL SECURITY AUDIT:
    When ADMIN_PASSWORD is set, every endpoint under /api/v1/scheduler/*
    MUST return HTTP 401 Unauthorized if requested without a valid Bearer JWT.
    """
    temp_store = DatabaseStore(db_path=tmp_path / "qa_sched_auth.db")
    monkeypatch.setitem(app.state._state, "store", temp_store)
    monkeypatch.setitem(app.state._state, "cron_scheduler", CronScheduler(temp_store))
    monkeypatch.setenv("ADMIN_PASSWORD", "qa_secure_password_sched_999")

    client = TestClient(app)

    # 1. Unauthenticated endpoints
    endpoints_to_test = [
        ("GET", "/api/v1/scheduler/rules"),
        ("POST", "/api/v1/scheduler/rules"),
        ("GET", "/api/v1/scheduler/rules/test-rule-id"),
        ("PATCH", "/api/v1/scheduler/rules/test-rule-id"),
        ("DELETE", "/api/v1/scheduler/rules/test-rule-id"),
        ("GET", "/api/v1/scheduler/executions"),
        ("POST", "/api/v1/scheduler/evaluate"),
    ]

    for http_method, path in endpoints_to_test:
        if http_method == "GET":
            res = client.get(path)
        elif http_method == "POST":
            res = client.post(path, json={})
        elif http_method == "PATCH":
            res = client.patch(path, json={})
        elif http_method == "DELETE":
            res = client.delete(path)
        else:
            raise ValueError(f"Unsupported method {http_method}")

        assert res.status_code == 401, f"{http_method} {path} expected 401, got {res.status_code}"
        assert "Authentication credentials missing" in res.json().get("detail", "")

    # 2. Forged / Tampered Bearer token
    bad_headers = {"Authorization": "Bearer forged.invalid.token.signature"}
    res_tampered = client.get("/api/v1/scheduler/rules", headers=bad_headers)
    assert res_tampered.status_code == 401
    assert "Could not validate credentials" in res_tampered.json().get("detail", "")


# ===========================================================================
# 6. Phase 16: ReDoS Regex Rejection & Length Protection
# ===========================================================================


def test_qa_phase16_redos_regex_rejection_and_pattern_limits():
    """
    Ensure validate_regex_pattern:
    1. Rejects catastrophic backtracking (ReDoS) patterns (nested quantifiers, overlapping wildcards).
    2. Rejects patterns exceeding MAX_REGEX_PATTERN_LENGTH (512 chars).
    3. Rejects invalid regular expression syntax.
    """
    redos_patterns = [
        r"(a+)+$",
        r"([a-z]+)*$",
        r"(foo+)+",
        r"(.+?){2,}",
        r"(a*)*",
        r"(x+)+",
        r".*.*foo",
        r"prefix.*.*",
    ]

    for pattern in redos_patterns:
        with pytest.raises(ValueError) as exc_info:
            validate_regex_pattern(pattern)
        msg = str(exc_info.value).lower()
        assert ("nested quantifiers" in msg) or ("wildcards" in msg)

    # Exceeding maximum pattern length limit (512 characters)
    oversized_pattern = "a" * (MAX_REGEX_PATTERN_LENGTH + 1)
    with pytest.raises(ValueError, match="maximum allowed length"):
        validate_regex_pattern(oversized_pattern)

    # Malformed regex syntax
    with pytest.raises(ValueError, match="Invalid regular expression"):
        validate_regex_pattern(r"[unclosed_bracket")

    # NotificationRule model validation also catches ReDoS
    with pytest.raises(ValueError, match="nested quantifiers"):
        NotificationRule(
            name="Unsafe Rule",
            body_regex=r"(a+)+$",
        )


# ===========================================================================
# 7. Phase 16: Strict Whitelist vs Blocked Packages
# ===========================================================================


def test_qa_phase16_strict_whitelist_vs_blocked_packages():
    """
    Ensure PackageWhitelistChecker:
    1. In 'strict' mode, denies any package not explicitly whitelisted.
    2. Explicit 'blocked_packages' ALWAYS overrides whitelist (precedence).
    3. Non-whitelisted package in NotificationRuleEngine results in IGNORED and deliver=False.
    """
    checker = PackageWhitelistChecker(
        PackageWhitelistConfig(
            mode="strict",
            allowed_packages=["com.whatsapp", "org.telegram.*", "com.google.android.gm"],
            blocked_packages=["com.spam.*", "com.google.android.gm"],  # gm is in BOTH allowed and blocked
        )
    )

    # 1. Allowed packages in whitelist
    assert checker.check("com.whatsapp").allowed is True
    assert checker.check("COM.WHATSAPP").allowed is True  # case insensitive
    assert checker.check("org.telegram.messenger").allowed is True
    assert checker.check("org.telegram.plus").allowed is True

    # 2. Blocklist takes precedence over whitelist
    blocked_gm = checker.check("com.google.android.gm")
    assert blocked_gm.allowed is False
    assert "blocked pattern" in blocked_gm.reason.lower()

    # 3. Blocklist pattern match
    blocked_spam = checker.check("com.spam.adware")
    assert blocked_spam.allowed is False

    # 4. Unwhitelisted package rejected in strict mode
    unwhitelisted = checker.check("com.random.untrusted")
    assert unwhitelisted.allowed is False
    assert "not in strict whitelist" in unwhitelisted.reason.lower()

    # 5. Integration with NotificationRuleEngine
    engine = NotificationRuleEngine(
        whitelist_config=checker.config,
        use_default_heuristics=True,
    )
    result = engine.evaluate(
        NotificationPayload(
            package_name="com.random.untrusted",
            title="CRITICAL SECURITY BREACH",
            body="Door opened",
        ),
        record_history=False,
    )
    assert result.whitelisted is False
    assert result.alert_level == AlertLevel.IGNORED
    assert result.deliver is False
    assert result.priority_score == 0


# ===========================================================================
# 8. Phase 16: Overnight DND Schedule Boundary (23:59 & 00:01)
# ===========================================================================


def test_qa_phase16_overnight_dnd_boundary_evaluation():
    """
    Ensure SilentScheduleEvaluator handles exact overnight wrap-around window boundaries:
    Schedule: 22:00 - 07:00 Asia/Jakarta (UTC+7), every day.
    Checks:
    - 21:59 WIB: Outside DND (silenced=False)
    - 22:00 WIB: Inside DND (silenced=True)
    - 23:59 WIB: Inside DND (silenced=True)
    - 00:00 WIB: Inside DND (silenced=True)
    - 00:01 WIB: Inside DND (silenced=True)
    - 06:59 WIB: Inside DND (silenced=True)
    - 07:00 WIB: Outside DND (silenced=False)
    """
    evaluator = SilentScheduleEvaluator()
    config = SilentScheduleConfig(
        enabled=True,
        start_time="22:00",
        end_time="07:00",
        days_of_week=["mon", "tue", "wed", "thu", "fri", "sat", "sun"],
        timezone="Asia/Jakarta",
        allow_critical_bypass=True,
    )

    # 21:59 WIB (14:59 UTC) — 1 minute before start
    t_2159 = datetime(2026, 11, 23, 14, 59, 0, tzinfo=timezone.utc)
    res_2159 = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=t_2159)
    assert res_2159.in_silent_window is False
    assert res_2159.silenced is False

    # 22:00 WIB (15:00 UTC) — Exact start boundary
    t_2200 = datetime(2026, 11, 23, 15, 0, 0, tzinfo=timezone.utc)
    res_2200 = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=t_2200)
    assert res_2200.in_silent_window is True
    assert res_2200.silenced is True

    # 23:59 WIB (16:59 UTC) — 1 minute before midnight
    t_2359 = datetime(2026, 11, 23, 16, 59, 0, tzinfo=timezone.utc)
    res_2359 = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=t_2359)
    assert res_2359.in_silent_window is True
    assert res_2359.silenced is True

    # 00:00 WIB (17:00 UTC) — Midnight rollover
    t_0000 = datetime(2026, 11, 23, 17, 0, 0, tzinfo=timezone.utc)
    res_0000 = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=t_0000)
    assert res_0000.in_silent_window is True
    assert res_0000.silenced is True

    # 00:01 WIB (17:01 UTC) — 1 minute past midnight
    t_0001 = datetime(2026, 11, 23, 17, 1, 0, tzinfo=timezone.utc)
    res_0001 = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=t_0001)
    assert res_0001.in_silent_window is True
    assert res_0001.silenced is True

    # 06:59 WIB (23:59 UTC) — 1 minute before end
    t_0659 = datetime(2026, 11, 23, 23, 59, 0, tzinfo=timezone.utc)
    res_0659 = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=t_0659)
    assert res_0659.in_silent_window is True
    assert res_0659.silenced is True

    # 07:00 WIB (00:00 UTC next day) — Exact end boundary
    t_0700 = datetime(2026, 11, 24, 0, 0, 0, tzinfo=timezone.utc)
    res_0700 = evaluator.evaluate(config, alert_level=AlertLevel.NORMAL, at_time=t_0700)
    assert res_0700.in_silent_window is False
    assert res_0700.silenced is False


# ===========================================================================
# 9. Phase 16: Critical Alert Bypass During DND Windows
# ===========================================================================


def test_qa_phase16_critical_alert_bypass_and_overrides():
    """
    Ensure that during active DND windows:
    1. Critical alerts bypass silent mode when allow_critical_bypass=True.
    2. Critical alerts do NOT bypass when allow_critical_bypass=False.
    3. Rules with bypass_silent=True bypass silent mode.
    4. Priority scores >= min_bypass_score bypass silent mode.
    """
    evaluator = SilentScheduleEvaluator()
    config_bypass_enabled = SilentScheduleConfig(
        enabled=True,
        start_time="22:00",
        end_time="07:00",
        timezone="Asia/Jakarta",
        allow_critical_bypass=True,
        min_bypass_score=200,
    )
    config_bypass_disabled = SilentScheduleConfig(
        enabled=True,
        start_time="22:00",
        end_time="07:00",
        timezone="Asia/Jakarta",
        allow_critical_bypass=False,
    )

    # 23:59 WIB (inside DND)
    at_2359 = datetime(2026, 11, 23, 16, 59, 0, tzinfo=timezone.utc)

    # 1. Normal alert inside DND -> Silenced
    res_norm = evaluator.evaluate(config_bypass_enabled, alert_level=AlertLevel.NORMAL, at_time=at_2359)
    assert res_norm.in_silent_window is True
    assert res_norm.silenced is True
    assert res_norm.bypassed is False

    # 2. Critical alert with allow_critical_bypass=True -> Bypassed
    res_crit = evaluator.evaluate(config_bypass_enabled, alert_level=AlertLevel.CRITICAL, at_time=at_2359)
    assert res_crit.in_silent_window is True
    assert res_crit.silenced is False
    assert res_crit.bypassed is True
    assert "critical alert bypassed" in res_crit.reason.lower()

    # 3. Critical alert with allow_critical_bypass=False -> Silenced
    res_crit_blocked = evaluator.evaluate(config_bypass_disabled, alert_level=AlertLevel.CRITICAL, at_time=at_2359)
    assert res_crit_blocked.in_silent_window is True
    assert res_crit_blocked.silenced is True
    assert res_crit_blocked.bypassed is False

    # 4. Rule-level bypass_silent=True -> Bypassed
    res_rule_bypass = evaluator.evaluate(
        config_bypass_disabled,
        alert_level=AlertLevel.NORMAL,
        rule_bypass_silent=True,
        at_time=at_2359,
    )
    assert res_rule_bypass.silenced is False
    assert res_rule_bypass.bypassed is True

    # 5. Priority score >= min_bypass_score (250 >= 200) -> Bypassed
    res_score_high = evaluator.evaluate(
        config_bypass_enabled,
        alert_level=AlertLevel.HIGH,
        priority_score=250,
        at_time=at_2359,
    )
    assert res_score_high.silenced is False
    assert res_score_high.bypassed is True


# ===========================================================================
# 10. Phase 16: JWT 401 Unauthorized Security on /api/v1/notifications/*
# ===========================================================================


def test_qa_phase16_api_notifications_endpoints_jwt_401_enforcement(tmp_path, monkeypatch):
    """
    CRITICAL SECURITY AUDIT:
    When ADMIN_PASSWORD is set, every endpoint under /api/v1/notifications/*
    MUST return HTTP 401 Unauthorized if requested without a valid Bearer JWT.
    """
    temp_store = DatabaseStore(db_path=tmp_path / "qa_notif_auth.db")
    monkeypatch.setitem(app.state._state, "store", temp_store)
    monkeypatch.setenv("ADMIN_PASSWORD", "qa_secure_password_notif_777")

    client = TestClient(app)

    # 1. Unauthenticated endpoints
    endpoints_to_test = [
        ("POST", "/api/v1/notifications/ingest", {"package_name": "com.test", "title": "Test"}),
        ("GET", "/api/v1/notifications/history", None),
        ("POST", "/api/v1/notifications/rules", {"name": "Test Rule"}),
        ("GET", "/api/v1/notifications/rules", None),
        ("DELETE", "/api/v1/notifications/rules/test-rule-id", None),
        ("GET", "/api/v1/notifications/silent-schedule", None),
        ("PUT", "/api/v1/notifications/silent-schedule", {"enabled": True}),
        ("GET", "/api/v1/notifications/whitelist", None),
        ("PUT", "/api/v1/notifications/whitelist", {"mode": "allow_all"}),
    ]

    for http_method, path, body in endpoints_to_test:
        if http_method == "GET":
            res = client.get(path)
        elif http_method == "POST":
            res = client.post(path, json=body or {})
        elif http_method == "PUT":
            res = client.put(path, json=body or {})
        elif http_method == "DELETE":
            res = client.delete(path)
        else:
            raise ValueError(f"Unsupported method {http_method}")

        assert res.status_code == 401, f"{http_method} {path} expected 401, got {res.status_code}"
        assert "Authentication credentials missing" in res.json().get("detail", "")

    # 2. Forged / Tampered Bearer token
    bad_headers = {"Authorization": "Bearer forged.invalid.token.signature"}
    res_tampered = client.get("/api/v1/notifications/rules", headers=bad_headers)
    assert res_tampered.status_code == 401
    assert "Could not validate credentials" in res_tampered.json().get("detail", "")
