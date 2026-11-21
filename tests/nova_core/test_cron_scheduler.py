"""Comprehensive tests for Phase 15: Time & Cron Scheduler Trigger (F15.1–F15.5)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
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
from nova_core.db.store import DatabaseStore
from nova_core.main import app


# ===========================================================================
# F15.1: 5-Field Cron Expression Parser & Evaluator
# ===========================================================================


def test_cron_parser_wildcards_steps_ranges_lists_and_names():
    expr = parse_cron("*/15 9-17 1,15 JAN-MAR MON-FRI")
    assert expr.minutes == frozenset({0, 15, 30, 45})
    assert expr.hours == frozenset(range(9, 18))
    assert expr.days_of_month == frozenset({1, 15})
    assert expr.months == frozenset({1, 2, 3})
    assert expr.days_of_week == frozenset({1, 2, 3, 4, 5})

    # Sunday as both 0 and 7
    sun0 = parse_cron("0 8 * * 0")
    sun7 = parse_cron("0 8 * * 7")
    assert sun0.days_of_week == frozenset({0})
    assert sun7.days_of_week == frozenset({0})

    # Step on range and offset step
    range_step = parse_cron("10-30/10 5/6 * * *")
    assert range_step.minutes == frozenset({10, 20, 30})
    assert range_step.hours == frozenset({5, 11, 17, 23})

    # Macros
    daily = parse_cron("@daily")
    assert daily.minutes == frozenset({0})
    assert daily.hours == frozenset({0})


@pytest.mark.parametrize(
    "bad_expr",
    [
        "",
        "   ",
        "* * *",
        "* * * * * *",
        "60 * * * *",
        "* 24 * * *",
        "* * 0 * *",
        "* * 32 * *",
        "* * * 13 *",
        "* * * * 8",
        "*/0 * * * *",
        "*/-5 * * * *",
        "10-5 * * * *",
        "1,,2 * * * *",
        "abc * * * *",
    ],
)
def test_cron_parser_rejects_invalid_expressions(bad_expr: str):
    with pytest.raises(CronParseError):
        parse_cron(bad_expr)


def test_cron_matches_and_vixie_dom_dow_union():
    # 2026-11-21 is a Saturday (cron dow=6)
    sat = datetime(2026, 11, 21, 14, 30, tzinfo=timezone.utc)
    assert parse_cron("30 14 21 11 6").matches(sat) is True
    assert parse_cron("30 14 * * SAT").matches(sat) is True
    assert parse_cron("30 14 * * MON").matches(sat) is False

    # Vixie cron: when both DOM and DOW are restricted, match if EITHER matches
    both_restricted = parse_cron("30 14 1 * SAT")
    assert both_restricted.matches(sat) is True  # matches SAT even though day is 21
    first_day = datetime(2026, 11, 1, 14, 30, tzinfo=timezone.utc)  # Sunday (dow=0)
    assert both_restricted.matches(first_day) is True  # matches day=1 even though Sunday
    neither = datetime(2026, 11, 2, 14, 30, tzinfo=timezone.utc)  # Monday day=2
    assert both_restricted.matches(neither) is False


def test_cron_next_run_at_and_next_n_runs():
    base = datetime(2026, 11, 21, 10, 7, 35, tzinfo=timezone.utc)
    nxt = next_run_at("*/15 * * * *", after=base, tz="UTC")
    assert nxt == datetime(2026, 11, 21, 10, 15, 0, tzinfo=timezone.utc)

    # Strictly after exact matching minute
    exact = datetime(2026, 11, 21, 10, 15, 0, tzinfo=timezone.utc)
    nxt2 = next_run_at("*/15 * * * *", after=exact, tz="UTC")
    assert nxt2 == datetime(2026, 11, 21, 10, 30, 0, tzinfo=timezone.utc)

    # Rollover across year boundary
    dec31 = datetime(2026, 12, 31, 23, 55, 0, tzinfo=timezone.utc)
    ny = next_run_at("0 0 1 1 *", after=dec31, tz="UTC")
    assert ny == datetime(2027, 1, 1, 0, 0, 0, tzinfo=timezone.utc)

    # Leap day (2028-02-29)
    leap_next = next_run_at("0 12 29 2 *", after=datetime(2026, 3, 1, 0, 0, tzinfo=timezone.utc))
    assert leap_next == datetime(2028, 2, 29, 12, 0, 0, tzinfo=timezone.utc)

    # next_n_runs
    runs = CronExpression("0 * * * *").next_n_runs(3, after=base, tz="UTC")
    assert runs == [
        datetime(2026, 11, 21, 11, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc),
        datetime(2026, 11, 21, 13, 0, tzinfo=timezone.utc),
    ]


# ===========================================================================
# F15.2: DST & Timezone Offset Calculator
# ===========================================================================


def test_timezone_validation_and_offset_info():
    assert validate_timezone("Asia/Jakarta") == ZoneInfo("Asia/Jakarta")
    assert validate_timezone("UTC") == ZoneInfo("UTC")

    with pytest.raises(TimezoneError):
        validate_timezone("Mars/Olympus_Mons")
    with pytest.raises(TimezoneError):
        validate_timezone("")

    # Asia/Jakarta: +07:00, no DST
    jkt_info = get_timezone_offset_info(
        datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc), "Asia/Jakarta"
    )
    assert jkt_info["utc_offset_seconds"] == 7 * 3600
    assert jkt_info["utc_offset_str"] == "+07:00"
    assert jkt_info["is_dst"] is False

    # America/New_York: winter EST (-05:00) vs summer EDT (-04:00)
    ny_winter = get_timezone_offset_info(
        datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc), "America/New_York"
    )
    assert ny_winter["utc_offset_str"] == "-05:00"
    assert ny_winter["is_dst"] is False

    ny_summer = get_timezone_offset_info(
        datetime(2026, 7, 15, 12, 0, tzinfo=timezone.utc), "America/New_York"
    )
    assert ny_summer["utc_offset_str"] == "-04:00"
    assert ny_summer["is_dst"] is True
    assert ny_summer["dst_offset_seconds"] == 3600


def test_dst_non_existent_spring_forward_gap():
    # In America/New_York, 2026-03-08 jumps from 01:59:59 EST (-05:00) to 03:00:00 EDT (-04:00).
    gap_naive = datetime(2026, 3, 8, 2, 30, 0)
    resolved = resolve_local_datetime(
        gap_naive, "America/New_York", non_existent_strategy="shift_forward"
    )
    assert resolved.transition_type == "non_existent"
    assert resolved.adjusted is True
    assert resolved.local_dt.hour == 3
    assert resolved.local_dt.minute == 30
    assert resolved.is_dst is True
    assert resolved.utc_dt == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)

    with pytest.raises(TimezoneError):
        resolve_local_datetime(gap_naive, "America/New_York", non_existent_strategy="raise")


def test_dst_ambiguous_fall_back_fold():
    # In America/New_York, 2026-11-01 01:30 occurs twice: EDT (-04:00, fold=0) and EST (-05:00, fold=1).
    ambig_naive = datetime(2026, 11, 1, 1, 30, 0)

    first = resolve_local_datetime(
        ambig_naive, "America/New_York", ambiguous_strategy="prefer_first"
    )
    assert first.transition_type == "ambiguous"
    assert first.fold == 0
    assert first.is_dst is True
    assert first.utc_dt == datetime(2026, 11, 1, 5, 30, tzinfo=timezone.utc)

    second = resolve_local_datetime(
        ambig_naive, "America/New_York", ambiguous_strategy="prefer_second"
    )
    assert second.transition_type == "ambiguous"
    assert second.fold == 1
    assert second.is_dst is False
    assert second.utc_dt == datetime(2026, 11, 1, 6, 30, tzinfo=timezone.utc)

    with pytest.raises(TimezoneError):
        resolve_local_datetime(ambig_naive, "America/New_York", ambiguous_strategy="raise")


def test_to_utc_from_utc_and_cron_across_dst():
    jkt_naive = datetime(2026, 11, 21, 19, 0, 0)
    utc_dt = to_utc(jkt_naive, "Asia/Jakarta")
    assert utc_dt == datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)
    back = from_utc(utc_dt, "Asia/Jakarta")
    assert back.hour == 19 and back.minute == 0

    # Cron scheduled for 02:30 in America/New_York on spring-forward day shifts cleanly to 03:30 EDT
    before_gap = datetime(2026, 3, 8, 6, 0, tzinfo=timezone.utc)  # 01:00 EST
    nxt_gap = next_run_at("30 2 * * *", after=before_gap, tz="America/New_York")
    assert nxt_gap.astimezone(timezone.utc) == datetime(2026, 3, 8, 7, 30, tzinfo=timezone.utc)
    assert nxt_gap.hour == 3 and nxt_gap.minute == 30


# ===========================================================================
# F15.3: Overlap Check & Execution Lock Manager
# ===========================================================================


def test_execution_lock_manager_acquire_release_ttl_and_extend():
    fake_now = [1000.0]
    locks = ExecutionLockManager(clock=lambda: fake_now[0])

    res1 = locks.acquire_lock("rule_a", owner_id="worker-1", ttl_seconds=60.0)
    assert res1.acquired is True
    assert res1.token is not None
    assert locks.is_locked("rule_a") is True

    # Overlap attempt fails while lock is active
    res2 = locks.acquire_lock("rule_a", owner_id="worker-2", ttl_seconds=60.0)
    assert res2.acquired is False
    assert "already locked" in (res2.reason or "")

    # Release with wrong token fails
    assert locks.release_lock("rule_a", token="wrong-token") is False
    assert locks.is_locked("rule_a") is True

    # Extend lock by 120s
    assert locks.extend_lock("rule_a", token=res1.token, ttl_seconds=120.0) is True
    info = locks.get_lock_info("rule_a")
    assert info is not None
    assert info["remaining_seconds"] == 120.0

    # Advance past TTL -> lock expires and new worker can acquire
    fake_now[0] += 121.0
    assert locks.is_locked("rule_a") is False
    res3 = locks.acquire_lock("rule_a", owner_id="worker-2", ttl_seconds=30.0)
    assert res3.acquired is True
    assert locks.release_lock("rule_a", token=res3.token) is True
    assert locks.is_locked("rule_a") is False


def test_cron_scheduler_evaluate_overlap_and_failure_handling(tmp_path):
    store = DatabaseStore(db_path=tmp_path / "sched.db")
    locks = ExecutionLockManager()
    scheduler = CronScheduler(store=store, lock_manager=locks)

    rule = store.create_scheduled_rule(
        name="Every 15m Backup",
        cron_expression="*/15 * * * *",
        timezone_name="UTC",
        action_template={"action_type": "system.backup", "params": {}},
        enabled=True,
        prevent_overlap=True,
        lock_ttl_seconds=120,
    )
    due_time = datetime(2026, 11, 21, 12, 15, 0, tzinfo=timezone.utc)

    # 1. Dry run does not execute or log
    dry_summary = asyncio.run(scheduler.evaluate_due_rules(now=due_time, dry_run=True))
    assert dry_summary["due_count"] == 1
    assert dry_summary["executed_count"] == 0
    assert store.get_scheduler_executions(rule_id=rule["id"]) == []

    # 2. Normal execution succeeds and updates last_run_at / next_run_at
    ran = []
    summary = asyncio.run(
        scheduler.evaluate_due_rules(
            now=due_time,
            executor=lambda r, dt: (ran.append(r["id"]), {"ok": True})[1],
        )
    )
    assert summary["executed_count"] == 1
    assert summary["failed_count"] == 0
    assert ran == [rule["id"]]
    assert locks.is_locked(rule["id"]) is False  # lock released after run

    updated_rule = store.get_scheduled_rule(rule["id"])
    assert updated_rule["last_run_at"] is not None
    assert updated_rule["next_run_at"] == "2026-11-21T12:30:00+00:00"

    # 3. Pre-held lock causes overlap skip
    held = locks.acquire_lock(rule["id"], owner_id="long-running-job", ttl_seconds=60.0)
    assert held.acquired is True
    overlap_summary = asyncio.run(scheduler.evaluate_due_rules(now=due_time))
    assert overlap_summary["skipped_overlap_count"] == 1
    assert overlap_summary["executed_count"] == 0
    locks.release_lock(rule["id"], token=held.token)

    # 4. Failing executor logs failed status and still releases lock
    def failing_exec(r, dt):
        raise RuntimeError("boom")

    fail_summary = asyncio.run(
        scheduler.evaluate_due_rules(now=due_time, executor=failing_exec)
    )
    assert fail_summary["failed_count"] == 1
    assert locks.is_locked(rule["id"]) is False

    stats = store.get_scheduler_execution_stats(rule_id=rule["id"])
    assert stats["total"] == 3
    assert stats["success_count"] == 1
    assert stats["skipped_overlap_count"] == 1
    assert stats["failed_count"] == 1


# ===========================================================================
# F15.4: Migration 0007 & Rollback
# ===========================================================================


def test_migration_0007_and_rollback(tmp_path):
    store = DatabaseStore(db_path=tmp_path / "mig0007.db")

    # Verify migration 0007 is recorded and tables exist
    with store.get_connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM schema_migrations WHERE version = '0007_scheduler_executions'"
        ).fetchone()
        assert row is not None

    rule = store.create_scheduled_rule(
        name="Daily Ping",
        cron_expression="0 9 * * *",
        timezone_name="Asia/Jakarta",
        action_template={"action_type": "notify"},
    )
    assert store.get_scheduled_rule(rule["id"]) is not None

    # Rollback migration 0007
    assert store.rollback_migration("0007_scheduler_executions") is True
    with pytest.raises(Exception):
        with store.get_connection() as conn:
            conn.execute("SELECT * FROM scheduled_rules")
    with pytest.raises(Exception):
        with store.get_connection() as conn:
            conn.execute("SELECT * FROM scheduler_executions")

    # Re-apply via init_db
    store.init_db()
    assert store.list_scheduled_rules() == []


# ===========================================================================
# F15.5: REST API Endpoints with JWT Auth
# ===========================================================================


def test_scheduler_rest_endpoints_crud_evaluate_and_auth(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nayakacode")
    isolated_store = DatabaseStore(db_path=tmp_path / "api_sched.db")
    orig_store = app.state.store
    orig_scheduler = getattr(app.state, "cron_scheduler", None)
    app.state.store = isolated_store
    app.state.cron_scheduler = CronScheduler(isolated_store)

    try:
        client = TestClient(app)

        # 1. Unauthenticated request blocked with 401
        unauth = client.get("/api/v1/scheduler/rules")
        assert unauth.status_code == 401

        # 2. Obtain JWT token
        tok_res = client.post("/auth/token", json={"username": "admin", "password": "nayakacode"})
        assert tok_res.status_code == 200
        headers = {"Authorization": f"Bearer {tok_res.json()['access_token']}"}

        # 3. Create rule - validation errors return 400
        bad_cron = client.post(
            "/api/v1/scheduler/rules",
            json={"name": "Bad", "cron_expression": "99 * * * *", "timezone": "UTC"},
            headers=headers,
        )
        assert bad_cron.status_code == 400

        bad_tz = client.post(
            "/api/v1/scheduler/rules",
            json={"name": "BadTZ", "cron_expression": "0 9 * * *", "timezone": "Invalid/Zone"},
            headers=headers,
        )
        assert bad_tz.status_code == 400

        # 4. Create valid scheduled rule
        create_res = client.post(
            "/api/v1/scheduler/rules",
            json={
                "name": "Morning Lights",
                "cron_expression": "30 7 * * *",
                "timezone": "Asia/Jakarta",
                "action_template": {"action_type": "smart_home.turn_on", "params": {"device": "lamp"}},
                "enabled": True,
                "prevent_overlap": True,
                "lock_ttl_seconds": 180,
            },
            headers=headers,
        )
        assert create_res.status_code == 201
        rule = create_res.json()["rule"]
        rule_id = rule["id"]
        assert rule["name"] == "Morning Lights"
        assert rule["timezone"] == "Asia/Jakarta"
        assert rule["next_run_at"] is not None

        # 5. List & Get rule
        list_res = client.get("/api/v1/scheduler/rules?enabled=true", headers=headers)
        assert list_res.status_code == 200
        assert list_res.json()["count"] == 1

        get_res = client.get(f"/api/v1/scheduler/rules/{rule_id}", headers=headers)
        assert get_res.status_code == 200
        assert get_res.json()["rule"]["id"] == rule_id

        # 6. Patch rule
        patch_res = client.patch(
            f"/api/v1/scheduler/rules/{rule_id}",
            json={"cron_expression": "0 8 * * *", "lock_ttl_seconds": 240},
            headers=headers,
        )
        assert patch_res.status_code == 200
        assert patch_res.json()["rule"]["cron_expression"] == "0 8 * * *"
        assert patch_res.json()["rule"]["lock_ttl_seconds"] == 240

        # 7. Evaluate ad-hoc expression (preview mode)
        eval_expr = client.post(
            "/api/v1/scheduler/evaluate",
            json={
                "cron_expression": "0 8 * * *",
                "timezone": "Asia/Jakarta",
                "at": "2026-11-21T01:00:00Z",  # 08:00 WIB (+07:00)
                "count": 2,
            },
            headers=headers,
        )
        assert eval_expr.status_code == 200
        expr_body = eval_expr.json()
        assert expr_body["mode"] == "expression"
        assert expr_body["matches"] is True
        assert expr_body["timezone_info"]["utc_offset_str"] == "+07:00"
        assert len(expr_body["upcoming_runs"]) == 2

        # 8. Evaluate stored due rules at matching UTC time (01:00Z == 08:00 Asia/Jakarta)
        eval_rules = client.post(
            "/api/v1/scheduler/evaluate",
            json={"at": "2026-11-21T01:00:00Z", "dry_run": False},
            headers=headers,
        )
        assert eval_rules.status_code == 200
        rules_body = eval_rules.json()
        assert rules_body["mode"] == "rules"
        assert rules_body["due_count"] == 1
        assert rules_body["executed_count"] == 1

        # 9. Query execution logs & stats
        exec_res = client.get(
            f"/api/v1/scheduler/executions?rule_id={rule_id}&status=success",
            headers=headers,
        )
        assert exec_res.status_code == 200
        exec_body = exec_res.json()
        assert exec_body["count"] == 1
        assert exec_body["stats"]["success_count"] == 1

        # 10. Delete rule & verify 404 thereafter
        del_res = client.delete(f"/api/v1/scheduler/rules/{rule_id}", headers=headers)
        assert del_res.status_code == 200
        assert client.get(f"/api/v1/scheduler/rules/{rule_id}", headers=headers).status_code == 404
    finally:
        app.state.store = orig_store
        if orig_scheduler is None:
            app.state._state.pop("cron_scheduler", None)
        else:
            app.state.cron_scheduler = orig_scheduler
