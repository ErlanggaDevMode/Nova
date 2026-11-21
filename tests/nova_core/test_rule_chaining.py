"""Comprehensive tests for Phase 19: Rule Chain & Loop Protections (F19.1–F19.5)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from nova_core.api.routes_rule_chains import router as rule_chains_router
from nova_core.automation.rule_chaining import (
    ChainStep,
    CircularReferenceDetector,
    CircularReferenceError,
    MaxDepthExceededError,
    RuleChain,
    RuleChainService,
    RuntimeLoopGuard,
)
from nova_core.automation.rules_store import RulesStore
from nova_core.db.store import DatabaseStore


# ---------------------------------------------------------------------------
# Helpers & Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def temp_store(tmp_path: Path) -> DatabaseStore:
    db_file = tmp_path / "test_rule_chains.db"
    store = DatabaseStore(db_path=db_file)
    yield store
    if store.sqlite_pool:
        store.sqlite_pool.close_all()


def _seed_device(
    store: DatabaseStore,
    device_id: str = "dev_1",
    capabilities: dict[str, Any] | None = None,
) -> str:
    store.register_device(
        device_id=device_id,
        name=f"Device {device_id}",
        platform="desktop",
        capabilities=capabilities if capabilities is not None else {"notify": True, "lights": True},
    )
    return device_id


def _seed_rule(
    store: DatabaseStore,
    name: str = "Sample Rule",
    *,
    condition: dict[str, Any] | None = None,
    action_template: dict[str, Any] | None = None,
    enabled: int = 1,
) -> str:
    rules_store = RulesStore(store)
    return rules_store.create_rule(
        name=name,
        condition=condition if condition is not None else {"type": "time", "time_of_day": "08:00"},
        action_template=(
            action_template
            if action_template is not None
            else {
                "action_type": "send_notification",
                "category": "notification",
                "source_device_id": "dev_1",
                "params": {"message": "Hello {context.user}"},
            }
        ),
        enabled=enabled,
    )


# ---------------------------------------------------------------------------
# Migration & Rollback Verification
# ---------------------------------------------------------------------------


def test_migration_0010_and_rollback(temp_store: DatabaseStore):
    expected_tables = {
        "rule_chains",
        "rule_chain_links",
        "rule_trigger_policies",
        "rule_trigger_events",
        "rule_chain_executions",
        "loop_breaker_states",
        "loop_breaker_alerts",
    }
    with temp_store.get_connection() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        existing = {r["name"] for r in rows}
    assert expected_tables.issubset(existing)

    assert temp_store.rollback_migration("0010_rule_chains") is True
    with temp_store.get_connection() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        after_rollback = {r["name"] for r in rows}
    assert expected_tables.isdisjoint(after_rollback)

    # Re-apply cleanly
    temp_store.init_db()
    with temp_store.get_connection() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        reapplied = {r["name"] for r in rows}
    assert expected_tables.issubset(reapplied)


# ---------------------------------------------------------------------------
# F19.1: Sequential Rule Execution Chaining, Context Passing & Branching
# ---------------------------------------------------------------------------


def test_f19_1_sequential_chaining_context_passing_and_history(temp_store: DatabaseStore):
    _seed_device(temp_store, "dev_1")
    r1 = _seed_rule(temp_store, "Step 1 Rule")
    r2 = _seed_rule(temp_store, "Step 2 Rule")
    r3 = _seed_rule(temp_store, "Step 3 Rule")

    execution_order: list[str] = []

    async def custom_executor(rule: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        execution_order.append(rule["id"])
        if rule["id"] == r1:
            return {"success": True, "computed_temp": 27.5, "context_updates": {"stage": "after_r1"}}
        if rule["id"] == r2:
            assert ctx["temp_c"] == 27.5
            assert ctx["stage"] == "after_r1"
            return {"success": True, "fan_speed": "high"}
        if rule["id"] == r3:
            assert ctx["fan_level"] == "high"
            assert ctx["step_outputs"]["s1"]["computed_temp"] == 27.5
            return {"success": True, "notified": True}
        return {"success": True}

    service = RuleChainService(temp_store, step_executor=custom_executor)
    chain = service.create_chain(
        RuleChain(
            name="Climate Pipeline",
            steps=[
                ChainStep(step_id="s1", rule_id=r1, context_mapping={"computed_temp": "temp_c"}),
                ChainStep(step_id="s2", rule_id=r2, context_mapping={"fan_speed": "fan_level"}),
                ChainStep(step_id="s3", rule_id=r3),
            ],
        )
    )

    result = asyncio.run(service.execute_chain(chain.id, initial_context={"room": "office"}))
    assert result.status == "completed"
    assert execution_order == [r1, r2, r3]
    assert len(result.step_results) == 3
    assert [sr.step_id for sr in result.step_results] == ["s1", "s2", "s3"]
    assert all(sr.status == "success" for sr in result.step_results)
    assert result.final_context["room"] == "office"
    assert result.final_context["temp_c"] == 27.5
    assert result.final_context["fan_level"] == "high"

    # Verify persisted execution history
    persisted = service.get_execution(result.execution_id)
    assert persisted is not None
    assert persisted.status == "completed"
    assert len(persisted.step_results) == 3


def test_f19_1_on_success_stop_and_on_failure_behaviors(temp_store: DatabaseStore):
    _seed_device(temp_store, "dev_1")
    r_ok = _seed_rule(temp_store, "OK Rule")
    r_fail = _seed_rule(temp_store, "Fail Rule")
    r_fallback = _seed_rule(temp_store, "Fallback Rule")
    r_after = _seed_rule(temp_store, "After Rule")

    def executor(rule: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        if rule["id"] == r_fail:
            return {"success": False, "error": "boom"}
        return {"success": True, "ran": rule["name"]}

    service = RuleChainService(temp_store, step_executor=executor)

    # 1. on_success="stop" stops early cleanly
    c_early = service.create_chain(
        RuleChain(
            name="Early Stop",
            steps=[
                ChainStep(step_id="s1", rule_id=r_ok, on_success="stop"),
                ChainStep(step_id="s2", rule_id=r_after),
            ],
        )
    )
    res1 = asyncio.run(service.execute_chain(c_early.id))
    assert res1.status == "completed"
    assert [sr.step_id for sr in res1.step_results] == ["s1"]

    # 2. stop_on_error=True halts chain as failed
    c_halt = service.create_chain(
        RuleChain(
            name="Halt On Failure",
            stop_on_error=True,
            steps=[
                ChainStep(step_id="s1", rule_id=r_fail, on_failure="stop"),
                ChainStep(step_id="s2", rule_id=r_after),
            ],
        )
    )
    res2 = asyncio.run(service.execute_chain(c_halt.id))
    assert res2.status == "failed"
    assert res2.error_message == "boom"
    assert [sr.step_id for sr in res2.step_results] == ["s1"]

    # 3. on_failure="continue" with stop_on_error=False continues to next step
    c_cont = service.create_chain(
        RuleChain(
            name="Continue On Failure",
            stop_on_error=False,
            steps=[
                ChainStep(step_id="s1", rule_id=r_fail, on_failure="continue"),
                ChainStep(step_id="s2", rule_id=r_after),
            ],
        )
    )
    res3 = asyncio.run(service.execute_chain(c_cont.id))
    assert res3.status == "completed"
    assert [(sr.step_id, sr.status) for sr in res3.step_results] == [("s1", "failed"), ("s2", "success")]

    # 4. on_failure="<fallback_step>" jumps directly to fallback step
    c_jump = service.create_chain(
        RuleChain(
            name="Fallback Jump",
            steps=[
                ChainStep(step_id="s1", rule_id=r_fail, on_failure="s_fb"),
                ChainStep(step_id="s_normal", rule_id=r_after),
                ChainStep(step_id="s_fb", rule_id=r_fallback, on_success="stop"),
            ],
        )
    )
    res4 = asyncio.run(service.execute_chain(c_jump.id))
    assert res4.status == "completed"
    assert [sr.step_id for sr in res4.step_results] == ["s1", "s_fb"]


def test_f19_1_nested_subchain_and_dry_run(temp_store: DatabaseStore):
    _seed_device(temp_store, "dev_1")
    r1 = _seed_rule(temp_store, "Sub Rule")
    r2 = _seed_rule(temp_store, "Parent Rule")

    calls: list[str] = []

    def executor(rule: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        calls.append(rule["id"])
        return {"success": True, "context_updates": {f"ran_{rule['name']}": True}}

    service = RuleChainService(temp_store, step_executor=executor)
    sub_chain = service.create_chain(
        RuleChain(name="Sub Chain", steps=[ChainStep(step_id="sub_s1", rule_id=r1)])
    )
    parent_chain = service.create_chain(
        RuleChain(
            name="Parent Chain",
            steps=[
                ChainStep(step_id="p_sub", sub_chain_id=sub_chain.id),
                ChainStep(step_id="p_r2", rule_id=r2),
            ],
        )
    )

    # Dry-run should not call executor
    dry = asyncio.run(service.execute_chain(parent_chain.id, dry_run=True))
    assert dry.status == "dry_run"
    assert calls == []
    assert [sr.status for sr in dry.step_results] == ["dry_run", "dry_run"]

    # Live run executes sub-chain then parent rule
    live = asyncio.run(service.execute_chain(parent_chain.id))
    assert live.status == "completed"
    assert calls == [r1, r2]
    assert live.final_context["ran_Sub Rule"] is True
    assert live.final_context["ran_Parent Rule"] is True


# ---------------------------------------------------------------------------
# F19.2: Circular Reference Loop Checks (Static DFS & Runtime Protection)
# ---------------------------------------------------------------------------


def test_f19_2_static_dfs_direct_and_indirect_rule_link_cycles(temp_store: DatabaseStore):
    service = RuleChainService(temp_store)

    # Direct self-loop A -> A
    with pytest.raises(CircularReferenceError) as exc_self:
        service.create_rule_link("rule_A", "rule_A")
    assert exc_self.value.cycle_path == ["rule_A", "rule_A"]

    # Indirect cycle A -> B -> C -> A
    service.create_rule_link("rule_A", "rule_B")
    service.create_rule_link("rule_B", "rule_C")
    with pytest.raises(CircularReferenceError) as exc_indirect:
        service.create_rule_link("rule_C", "rule_A")
    assert exc_indirect.value.cycle_path == ["rule_A", "rule_B", "rule_C", "rule_A"]


def test_f19_2_static_dfs_step_jump_and_subchain_cycles(temp_store: DatabaseStore):
    service = RuleChainService(temp_store)

    # Backward step jump cycle: s1 -> s2 -> s1
    with pytest.raises(CircularReferenceError) as exc_step:
        service.create_chain(
            RuleChain(
                name="Cyclic Steps",
                steps=[
                    ChainStep(step_id="s1", rule_id="r1", on_success="s2"),
                    ChainStep(step_id="s2", rule_id="r2", on_success="s1"),
                ],
            )
        )
    assert exc_step.value.cycle_path == ["s1", "s2", "s1"]

    # Cross-chain cycle: chain_A -> chain_B -> chain_C -> chain_A
    c_c = service.create_chain(
        RuleChain(id="chain_C", name="Chain C", steps=[ChainStep(step_id="c1", rule_id="r1")])
    )
    c_b = service.create_chain(
        RuleChain(id="chain_B", name="Chain B", steps=[ChainStep(step_id="b1", sub_chain_id=c_c.id)])
    )
    c_a = service.create_chain(
        RuleChain(id="chain_A", name="Chain A", steps=[ChainStep(step_id="a1", sub_chain_id=c_b.id)])
    )

    # Updating chain_C to reference chain_A closes the loop
    with pytest.raises(CircularReferenceError) as exc_chain:
        service.update_chain(
            c_c.model_copy(update={"steps": [ChainStep(step_id="c1", sub_chain_id=c_a.id)]})
        )
    assert set(exc_chain.value.cycle_path) == {"chain_A", "chain_B", "chain_C"}
    assert exc_chain.value.cycle_path[0] == exc_chain.value.cycle_path[-1]


def test_f19_2_runtime_visited_set_and_max_depth_guard():
    guard = RuntimeLoopGuard(max_depth=3)
    guard.enter("node_1")
    guard.enter("node_2")

    # Re-entering visited node raises CircularReferenceError
    with pytest.raises(CircularReferenceError) as exc_loop:
        guard.enter("node_1")
    assert exc_loop.value.cycle_path == ["node_1", "node_2", "node_1"]

    # Exceeding max_depth raises MaxDepthExceededError
    guard.enter("node_3")
    with pytest.raises(MaxDepthExceededError) as exc_depth:
        guard.enter("node_4")
    assert exc_depth.value.max_depth == 3


def test_f19_2_runtime_max_depth_trips_breaker_during_execution(temp_store: DatabaseStore):
    _seed_device(temp_store, "dev_1")
    r1 = _seed_rule(temp_store, "R1")
    r2 = _seed_rule(temp_store, "R2")

    service = RuleChainService(temp_store)
    sub = service.create_chain(
        RuleChain(name="Deep Sub", steps=[ChainStep(step_id="s1", rule_id=r1)])
    )
    # max_depth=2 allows entering parent chain (depth 1) + parent step (depth 2),
    # then entering sub-chain (depth 3) exceeds max_depth=2!
    parent = service.create_chain(
        RuleChain(
            name="Shallow Parent",
            max_depth=2,
            steps=[
                ChainStep(step_id="p1", sub_chain_id=sub.id),
                ChainStep(step_id="p2", rule_id=r2),
            ],
        )
    )

    res = asyncio.run(service.execute_chain(parent.id))
    assert res.status == "loop_detected"
    assert res.breaker_tripped is True
    assert "max depth" in (res.error_message or "").lower()

    breaker = service.breaker.get_state(parent.id)
    assert breaker.state == "open"
    alerts = service.breaker.list_alerts(target_id=parent.id)
    assert len(alerts) >= 1
    assert alerts[0].severity == "critical"


# ---------------------------------------------------------------------------
# F19.3: Trigger Rate Limits & Cooldown Schedules
# ---------------------------------------------------------------------------


def test_f19_3_sliding_window_rate_limit_and_cooldown(temp_store: DatabaseStore):
    clock = {"now": datetime(2026, 11, 21, 10, 0, 0, tzinfo=timezone.utc)}
    service = RuleChainService(temp_store, now_fn=lambda: clock["now"])

    # Configure: max 2 triggers per 60s window, 10s cooldown
    service.rate_limiter.set_policy(
        "rule_rate",
        target_type="rule",
        max_triggers=2,
        window_seconds=60,
        cooldown_seconds=10,
    )

    # t=0s: 1st trigger allowed
    c1 = service.rate_limiter.check_and_record("rule_rate")
    assert c1.allowed is True
    assert c1.triggers_in_window == 1

    # t=5s: suppressed by cooldown (5s remaining)
    clock["now"] += timedelta(seconds=5)
    c2 = service.rate_limiter.check_and_record("rule_rate")
    assert c2.allowed is False
    assert c2.reason == "cooldown_active"
    assert c2.cooldown_remaining_seconds == pytest.approx(5.0, abs=0.01)

    # t=11s: cooldown passed, 2nd trigger allowed
    clock["now"] += timedelta(seconds=6)
    c3 = service.rate_limiter.check_and_record("rule_rate")
    assert c3.allowed is True
    assert c3.triggers_in_window == 2

    # t=25s: cooldown passed, but 2 triggers already in 60s window -> rate limit exceeded
    clock["now"] += timedelta(seconds=14)
    c4 = service.rate_limiter.check_and_record("rule_rate")
    assert c4.allowed is False
    assert c4.reason == "rate_limit_exceeded"
    assert c4.retry_after_seconds == pytest.approx(35.0, abs=0.01)

    # t=61s: 1st trigger (t=0) slid out of 60s window -> allowed again!
    clock["now"] += timedelta(seconds=36)
    c5 = service.rate_limiter.check_and_record("rule_rate")
    assert c5.allowed is True


def test_f19_3_chain_execution_respects_cooldown_and_rate_limit(temp_store: DatabaseStore):
    clock = {"now": datetime(2026, 11, 21, 12, 0, 0, tzinfo=timezone.utc)}
    _seed_device(temp_store, "dev_1")
    r1 = _seed_rule(temp_store, "Fast Rule")

    service = RuleChainService(temp_store, now_fn=lambda: clock["now"])
    chain = service.create_chain(
        RuleChain(
            name="Throttled Chain",
            trigger_limit=2,
            trigger_window_seconds=60,
            cooldown_seconds=15,
            steps=[ChainStep(step_id="s1", rule_id=r1)],
        )
    )

    res1 = asyncio.run(service.execute_chain(chain.id))
    assert res1.status == "completed"

    # 5s later -> suppressed by cooldown
    clock["now"] += timedelta(seconds=5)
    res2 = asyncio.run(service.execute_chain(chain.id))
    assert res2.status == "suppressed"
    assert "cooldown_active" in (res2.error_message or "")

    # 16s from start -> allowed
    clock["now"] += timedelta(seconds=11)
    res3 = asyncio.run(service.execute_chain(chain.id))
    assert res3.status == "completed"

    # 35s from start -> suppressed by rate limit (2 per 60s)
    clock["now"] += timedelta(seconds=19)
    res4 = asyncio.run(service.execute_chain(chain.id))
    assert res4.status == "suppressed"
    assert "rate_limit_exceeded" in (res4.error_message or "")


# ---------------------------------------------------------------------------
# F19.4: Pre-Execution Dependency Validations
# ---------------------------------------------------------------------------


def test_f19_4_dependency_validation_passes_and_fails_with_structured_issues(
    temp_store: DatabaseStore,
):
    _seed_device(temp_store, "dev_ok", {"notify": True, "lights": False})
    r_valid = _seed_rule(
        temp_store,
        "Valid Rule",
        condition={"type": "weather", "temp_above": 25.0},
        action_template={
            "action_type": "notify",
            "category": "notification",
            "source_device_id": "dev_ok",
            "required_capabilities": ["notify"],
            "params": {},
        },
        enabled=1,
    )
    r_disabled = _seed_rule(temp_store, "Disabled Rule", enabled=0)
    r_bad_cond = _seed_rule(
        temp_store,
        "Bad Condition Rule",
        condition={"type": "unknown_type"},
        action_template={"action_type": "noop", "category": "system", "source_device_id": "dev_ok", "params": {}},
    )
    r_missing_dev = _seed_rule(
        temp_store,
        "Missing Device Rule",
        action_template={"action_type": "noop", "category": "system", "source_device_id": "ghost_dev", "params": {}},
    )
    r_bad_cap = _seed_rule(
        temp_store,
        "Bad Cap Rule",
        action_template={
            "action_type": "lights_on",
            "category": "smart_home",
            "source_device_id": "dev_ok",
            "required_capabilities": ["lights", "thermostat"],
            "params": {},
        },
    )

    service = RuleChainService(temp_store)

    # 1. Valid chain passes
    ok_chain = service.create_chain(
        RuleChain(name="Valid Chain", steps=[ChainStep(step_id="s1", rule_id=r_valid)])
    )
    ok_report = service.validate_chain(ok_chain.id)
    assert ok_report.valid is True
    assert ok_report.issues == []
    assert ok_report.checked_rules == 1
    assert ok_report.checked_devices == 1
    assert ok_report.checked_capabilities == 1
    assert ok_report.checked_conditions == 1

    # 2. Broken chain collects all structured errors
    broken_chain = service.create_chain(
        RuleChain(
            name="Broken Chain",
            enabled=False,
            steps=[
                ChainStep(step_id="s_missing", rule_id="nonexistent_rule"),
                ChainStep(step_id="s_disabled", rule_id=r_disabled),
                ChainStep(step_id="s_bad_cond", rule_id=r_bad_cond),
                ChainStep(step_id="s_missing_dev", rule_id=r_missing_dev),
                ChainStep(step_id="s_bad_cap", rule_id=r_bad_cap, on_failure="ghost_step"),
            ],
        )
    )
    bad_report = service.validate_chain(broken_chain.id)
    assert bad_report.valid is False
    codes = {issue.code for issue in bad_report.issues}
    assert {
        "CHAIN_DISABLED",
        "RULE_NOT_FOUND",
        "RULE_DISABLED",
        "INVALID_CONDITION",
        "DEVICE_NOT_FOUND",
        "CAPABILITY_DISABLED",
        "CAPABILITY_MISSING",
        "INVALID_STEP_TARGET",
    }.issubset(codes)

    # Execution refuses to start and returns validation_failed
    exec_res = asyncio.run(service.execute_chain(broken_chain.id))
    assert exec_res.status == "validation_failed"
    assert exec_res.validation is not None
    assert exec_res.validation.valid is False


# ---------------------------------------------------------------------------
# F19.5: Automation Loop Breaker (closed -> open -> half_open -> closed) & Alerts
# ---------------------------------------------------------------------------


def test_f19_5_circuit_breaker_lifecycle_alerts_and_reset(temp_store: DatabaseStore):
    clock = {"now": datetime(2026, 11, 21, 14, 0, 0, tzinfo=timezone.utc)}
    _seed_device(temp_store, "dev_1")
    r1 = _seed_rule(temp_store, "Flaky Rule")

    should_fail = {"flag": True}

    def flaky_executor(rule: dict[str, Any], ctx: dict[str, Any]) -> dict[str, Any]:
        if should_fail["flag"]:
            return {"success": False, "error": "downstream failure"}
        return {"success": True}

    service = RuleChainService(
        temp_store,
        step_executor=flaky_executor,
        now_fn=lambda: clock["now"],
    )
    chain = service.create_chain(
        RuleChain(
            name="Breaker Chain",
            breaker_failure_threshold=2,
            breaker_recovery_seconds=30,
            steps=[ChainStep(step_id="s1", rule_id=r1)],
        )
    )

    # Failure 1 -> breaker still closed (count=1)
    r_f1 = asyncio.run(service.execute_chain(chain.id))
    assert r_f1.status == "failed"
    assert r_f1.breaker_tripped is False
    assert service.breaker.get_state(chain.id).state == "closed"

    # Failure 2 -> trips breaker to open + persists alert
    r_f2 = asyncio.run(service.execute_chain(chain.id))
    assert r_f2.status == "failed"
    assert r_f2.breaker_tripped is True
    b_open = service.breaker.get_state(chain.id)
    assert b_open.state == "open"
    assert b_open.failure_count == 2

    alerts = service.breaker.list_alerts(target_id=chain.id, acknowledged=False)
    assert len(alerts) == 1
    alert_id = alerts[0].id
    assert alerts[0].breaker_state == "open"

    # Acknowledge alert
    acked = service.breaker.acknowledge_alert(alert_id, acknowledged_by="bima")
    assert acked is not None
    assert acked.acknowledged is True
    assert acked.acknowledged_by == "bima"

    # Immediate re-execution is blocked while open
    clock["now"] += timedelta(seconds=10)
    r_blocked = asyncio.run(service.execute_chain(chain.id))
    assert r_blocked.status == "blocked"

    # Advance past recovery_timeout (30s) -> transitions open -> half_open -> probe succeeds -> closed
    clock["now"] += timedelta(seconds=25)
    should_fail["flag"] = False
    r_probe = asyncio.run(service.execute_chain(chain.id))
    assert r_probe.status == "completed"
    b_closed = service.breaker.get_state(chain.id)
    assert b_closed.state == "closed"
    assert b_closed.failure_count == 0

    # Trip again, then test manual reset resolves alerts
    should_fail["flag"] = True
    asyncio.run(service.execute_chain(chain.id))
    asyncio.run(service.execute_chain(chain.id))
    assert service.breaker.get_state(chain.id).state == "open"

    reset_state = service.breaker.reset(chain.id)
    assert reset_state.state == "closed"
    assert reset_state.last_reset_at is not None
    unresolved = service.breaker.list_alerts(target_id=chain.id, resolved=False)
    assert unresolved == []


# ---------------------------------------------------------------------------
# FastAPI REST Endpoints via TestClient
# ---------------------------------------------------------------------------


def test_rule_chains_rest_api_end_to_end(temp_store: DatabaseStore):
    _seed_device(temp_store, "dev_api", {"notify": True})
    r1 = _seed_rule(
        temp_store,
        "API Rule 1",
        action_template={
            "action_type": "notify",
            "category": "notification",
            "source_device_id": "dev_api",
            "required_capabilities": ["notify"],
            "params": {"msg": "step1"},
        },
    )
    r2 = _seed_rule(
        temp_store,
        "API Rule 2",
        action_template={
            "action_type": "notify",
            "category": "notification",
            "source_device_id": "dev_api",
            "required_capabilities": ["notify"],
            "params": {"msg": "step2"},
        },
    )

    app = FastAPI()
    app.state.store = temp_store
    app.include_router(rule_chains_router)

    client = TestClient(app)

    # 1. Create chain
    create_resp = client.post(
        "/api/v1/rule-chains",
        json={
            "name": "API Chain",
            "steps": [
                {"step_id": "s1", "rule_id": r1},
                {"step_id": "s2", "rule_id": r2},
            ],
            "breaker_failure_threshold": 1,
        },
    )
    assert create_resp.status_code == 201
    chain_id = create_resp.json()["chain"]["id"]

    # 2. List & Get
    list_resp = client.get("/api/v1/rule-chains")
    assert list_resp.status_code == 200
    assert list_resp.json()["count"] == 1

    get_resp = client.get(f"/api/v1/rule-chains/{chain_id}")
    assert get_resp.status_code == 200
    assert get_resp.json()["breaker"]["state"] == "closed"

    # 3. Reject cyclic chain creation (409 Conflict)
    cyc_resp = client.post(
        "/api/v1/rule-chains",
        json={
            "name": "Cyclic Chain",
            "steps": [
                {"step_id": "a", "rule_id": r1, "on_success": "b"},
                {"step_id": "b", "rule_id": r2, "on_success": "a"},
            ],
        },
    )
    assert cyc_resp.status_code == 409
    assert cyc_resp.json()["detail"]["cycle_path"] == ["a", "b", "a"]

    # 4. Rule links A -> B -> C -> A cycle check via API
    assert client.post("/api/v1/rule-chains/links", json={"source_rule_id": "A", "target_rule_id": "B"}).status_code == 201
    assert client.post("/api/v1/rule-chains/links", json={"source_rule_id": "B", "target_rule_id": "C"}).status_code == 201
    link_cyc = client.post("/api/v1/rule-chains/links", json={"source_rule_id": "C", "target_rule_id": "A"})
    assert link_cyc.status_code == 409
    assert link_cyc.json()["detail"]["cycle_path"] == ["A", "B", "C", "A"]

    check_resp = client.post("/api/v1/rule-chains/check-cycles", json={})
    assert check_resp.status_code == 200
    assert check_resp.json()["has_cycle"] is False

    # 5. Validate chain
    val_resp = client.post(f"/api/v1/rule-chains/{chain_id}/validate")
    assert val_resp.status_code == 200
    assert val_resp.json()["validation"]["valid"] is True

    # 6. Dry-run & Execute chain
    dry_resp = client.post(f"/api/v1/rule-chains/{chain_id}/execute", json={"dry_run": True})
    assert dry_resp.status_code == 200
    assert dry_resp.json()["execution"]["status"] == "dry_run"

    exec_resp = client.post(
        f"/api/v1/rule-chains/{chain_id}/execute",
        json={"initial_context": {"user": "bima"}},
    )
    assert exec_resp.status_code == 200
    assert exec_resp.json()["success"] is True
    exec_id = exec_resp.json()["execution"]["execution_id"]
    assert len(exec_resp.json()["execution"]["step_results"]) == 2

    hist_resp = client.get(f"/api/v1/rule-chains/executions?chain_id={chain_id}")
    assert hist_resp.status_code == 200
    assert hist_resp.json()["count"] == 1

    single_exec = client.get(f"/api/v1/rule-chains/executions/{exec_id}")
    assert single_exec.status_code == 200
    assert single_exec.json()["execution"]["execution_id"] == exec_id

    # 7. Configure & check trigger limits
    lim_put = client.put(
        f"/api/v1/rule-chains/limits/{r1}",
        json={"target_type": "rule", "max_triggers": 1, "window_seconds": 60, "cooldown_seconds": 30},
    )
    assert lim_put.status_code == 200
    assert lim_put.json()["policy"]["max_triggers"] == 1

    lim_check1 = client.post(
        f"/api/v1/rule-chains/limits/{r1}/check",
        json={"target_type": "rule", "record": True},
    )
    assert lim_check1.status_code == 200
    assert lim_check1.json()["result"]["allowed"] is True

    lim_check2 = client.post(
        f"/api/v1/rule-chains/limits/{r1}/check",
        json={"target_type": "rule", "record": True},
    )
    assert lim_check2.status_code == 200
    assert lim_check2.json()["result"]["allowed"] is False

    # 8. Trip breaker directly via service, inspect/acknowledge alert, and reset via API
    service = app.state.rule_chain_service
    _, alert = service.breaker.record_failure(
        chain_id,
        "Simulated runaway loop",
        is_loop=True,
        failure_threshold=1,
    )
    assert alert is not None

    breakers_resp = client.get("/api/v1/rule-chains/breakers?state=open")
    assert breakers_resp.status_code == 200
    assert breakers_resp.json()["count"] == 1

    alerts_resp = client.get(f"/api/v1/rule-chains/alerts?target_id={chain_id}&acknowledged=false")
    assert alerts_resp.status_code == 200
    assert alerts_resp.json()["count"] == 1

    ack_resp = client.post(
        f"/api/v1/rule-chains/alerts/{alert.id}/acknowledge",
        json={"acknowledged_by": "qa_engineer"},
    )
    assert ack_resp.status_code == 200
    assert ack_resp.json()["alert"]["acknowledged"] is True

    reset_resp = client.post(f"/api/v1/rule-chains/breakers/{chain_id}/reset")
    assert reset_resp.status_code == 200
    assert reset_resp.json()["breaker"]["state"] == "closed"

    # 9. Update & Delete chain
    patch_resp = client.patch(f"/api/v1/rule-chains/{chain_id}", json={"name": "API Chain v2"})
    assert patch_resp.status_code == 200
    assert patch_resp.json()["chain"]["name"] == "API Chain v2"

    del_resp = client.delete(f"/api/v1/rule-chains/{chain_id}")
    assert del_resp.status_code == 200
    assert client.get(f"/api/v1/rule-chains/{chain_id}").status_code == 404
