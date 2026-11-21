"""Phase 19 (F19.1–F19.5): Rule Chain & Loop Protections.

Implements:
- F19.1: Sequential rule execution chaining with context passing, branching
  (`on_success`, `on_failure`, `stop_on_error`), and persisted step execution history.
- F19.2: Circular reference loop checks (static 3-color DFS cycle detection for
  chains, step transitions, sub-chains, and rule links + runtime visited-set and
  max-depth guards).
- F19.3: Rule/chain trigger rate limits (sliding window) and cooldown schedules
  with clean suppression.
- F19.4: Pre-execution dependency validation for chains, rules, conditions,
  devices, and device capabilities.
- F19.5: Automation loop breaker (per-target circuit breaker with `closed`,
  `open`, and `half_open` states) and persisted loop-breaker alert records with
  inspect, acknowledge, and reset operations.
"""

from __future__ import annotations

import copy
import inspect
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Literal, Optional

from pydantic import BaseModel, Field, model_validator

from nova_core.automation.conditions import TimeCondition, parse_condition
from nova_core.db.store import DatabaseStore

logger = logging.getLogger("nova.automation.rule_chaining")

TargetType = Literal["chain", "rule"]
BreakerState = Literal["closed", "open", "half_open"]
AlertSeverity = Literal["warning", "critical"]
LinkCondition = Literal["on_success", "on_failure", "always"]
ResourceType = Literal["chain", "rule", "device", "capability", "condition", "step"]

_TIME_HHMM_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_CONTEXT_PLACEHOLDER_RE = re.compile(r"^\{context\.([a-zA-Z0-9_.-]+)\}$")
_STEP_PLACEHOLDER_RE = re.compile(r"^\{step\.([a-zA-Z0-9_-]+)\.([a-zA-Z0-9_.-]+)\}$")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class RuleChainError(Exception):
    """Base exception for rule chaining and loop protection errors."""


class CircularReferenceError(RuleChainError):
    """Raised when a static or runtime circular reference is detected."""

    def __init__(self, cycle_path: list[str], message: str | None = None):
        self.cycle_path = list(cycle_path)
        cycle_str = " -> ".join(self.cycle_path)
        super().__init__(message or f"Circular reference detected: {cycle_str}")


class MaxDepthExceededError(RuleChainError):
    """Raised when runtime chain execution exceeds configured max_depth."""

    def __init__(self, max_depth: int, visited: list[str]):
        self.max_depth = max_depth
        self.visited = list(visited)
        super().__init__(
            f"Rule chain execution exceeded max depth ({max_depth}). Path: {' -> '.join(self.visited)}"
        )


class DependencyValidationError(RuleChainError):
    """Raised when pre-execution dependency validation fails."""

    def __init__(self, result: "DependencyValidationResult"):
        self.result = result
        summary = "; ".join(issue.message for issue in result.issues)
        super().__init__(f"Dependency validation failed: {summary}")


class TriggerSuppressedError(RuleChainError):
    """Raised when a rule/chain trigger is suppressed by rate limit or cooldown."""

    def __init__(self, reason: str, retry_after_seconds: float):
        self.reason = reason
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Trigger suppressed ({reason}); retry after {retry_after_seconds:.2f}s"
        )


class CircuitBreakerOpenError(RuleChainError):
    """Raised when execution is blocked by an open circuit breaker."""

    def __init__(self, target_id: str, state: str, retry_after_seconds: float):
        self.target_id = target_id
        self.state = state
        self.retry_after_seconds = retry_after_seconds
        super().__init__(
            f"Circuit breaker for '{target_id}' is {state}; retry after {retry_after_seconds:.2f}s"
        )


# ---------------------------------------------------------------------------
# Pydantic Models
# ---------------------------------------------------------------------------


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_utc(dt: datetime | str | None) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        raw = dt.strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        parsed = datetime.fromisoformat(raw)
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


class ChainStep(BaseModel):
    """A single ordered step inside a rule chain."""

    step_id: str = Field(default_factory=lambda: f"step_{os.urandom(4).hex()}", min_length=1, max_length=128)
    rule_id: Optional[str] = Field(default=None, max_length=128)
    sub_chain_id: Optional[str] = Field(default=None, max_length=128)
    on_success: str = Field(default="continue", min_length=1, max_length=128)
    on_failure: str = Field(default="stop", min_length=1, max_length=128)
    stop_on_error: Optional[bool] = Field(default=None)
    context_mapping: dict[str, str] = Field(default_factory=dict)
    required_device_id: Optional[str] = Field(default=None, max_length=128)
    required_capabilities: list[str] = Field(default_factory=list)
    enabled: bool = Field(default=True)

    @model_validator(mode="after")
    def _validate_target(self) -> "ChainStep":
        if not self.rule_id and not self.sub_chain_id:
            raise ValueError("Each ChainStep must specify either 'rule_id' or 'sub_chain_id'.")
        if self.rule_id and self.sub_chain_id:
            raise ValueError("ChainStep cannot specify both 'rule_id' and 'sub_chain_id'.")
        return self


class RuleChain(BaseModel):
    """Persistent definition of an ordered multi-step rule chain."""

    id: str = Field(default_factory=lambda: f"chain_{os.urandom(6).hex()}", min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    enabled: bool = Field(default=True)
    steps: list[ChainStep] = Field(default_factory=list)
    max_depth: int = Field(default=10, ge=1, le=100)
    stop_on_error: bool = Field(default=True)
    trigger_limit: Optional[int] = Field(default=None, ge=1)
    trigger_window_seconds: int = Field(default=60, ge=1)
    cooldown_seconds: int = Field(default=0, ge=0)
    breaker_failure_threshold: int = Field(default=3, ge=1)
    breaker_recovery_seconds: int = Field(default=60, ge=1)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class RuleLink(BaseModel):
    """Directed link between two automation rules (used for cycle detection & chaining)."""

    id: str = Field(default_factory=lambda: f"link_{os.urandom(6).hex()}")
    source_rule_id: str = Field(..., min_length=1, max_length=128)
    target_rule_id: str = Field(..., min_length=1, max_length=128)
    condition: LinkCondition = Field(default="on_success")
    created_at: Optional[str] = None


class TriggerPolicy(BaseModel):
    """Per-rule or per-chain trigger rate limit and cooldown schedule."""

    target_id: str = Field(..., min_length=1, max_length=128)
    target_type: TargetType = Field(default="rule")
    max_triggers: int = Field(default=10, ge=1)
    window_seconds: int = Field(default=60, ge=1)
    cooldown_seconds: int = Field(default=0, ge=0)
    last_triggered_at: Optional[str] = None
    updated_at: Optional[str] = None


class TriggerCheckResult(BaseModel):
    """Result of evaluating trigger rate limits and cooldown state."""

    allowed: bool
    target_id: str
    target_type: TargetType = "rule"
    reason: Optional[str] = None
    retry_after_seconds: float = 0.0
    triggers_in_window: int = 0
    max_triggers: Optional[int] = None
    window_seconds: Optional[int] = None
    cooldown_seconds: int = 0
    cooldown_remaining_seconds: float = 0.0


class DependencyIssue(BaseModel):
    """Structured pre-execution dependency validation issue."""

    code: str
    severity: Literal["error", "warning"] = "error"
    step_id: Optional[str] = None
    resource_type: ResourceType
    resource_id: Optional[str] = None
    message: str


class DependencyValidationResult(BaseModel):
    """Aggregate pre-execution dependency validation report."""

    valid: bool
    chain_id: Optional[str] = None
    issues: list[DependencyIssue] = Field(default_factory=list)
    checked_rules: int = 0
    checked_devices: int = 0
    checked_capabilities: int = 0
    checked_conditions: int = 0


class StepExecutionRecord(BaseModel):
    """Execution telemetry for a single step in a rule chain run."""

    step_id: str
    rule_id: Optional[str] = None
    sub_chain_id: Optional[str] = None
    status: str
    started_at: str
    finished_at: str
    duration_ms: float = 0.0
    output: dict[str, Any] = Field(default_factory=dict)
    error: Optional[str] = None
    next_action: str = "continue"


class ChainExecutionResult(BaseModel):
    """Full execution result of a rule chain run."""

    execution_id: str
    chain_id: str
    chain_name: str
    status: str
    initial_context: dict[str, Any] = Field(default_factory=dict)
    final_context: dict[str, Any] = Field(default_factory=dict)
    step_results: list[StepExecutionRecord] = Field(default_factory=list)
    error_message: Optional[str] = None
    started_at: str
    finished_at: str
    duration_ms: float = 0.0
    validation: Optional[DependencyValidationResult] = None
    breaker_tripped: bool = False


class CircuitBreakerState(BaseModel):
    """Persisted circuit breaker state for a chain or rule."""

    target_id: str
    target_type: TargetType = "chain"
    state: BreakerState = "closed"
    failure_count: int = 0
    loop_count: int = 0
    failure_threshold: int = 3
    recovery_timeout_seconds: int = 60
    last_failure_at: Optional[str] = None
    opened_at: Optional[str] = None
    half_open_at: Optional[str] = None
    last_reset_at: Optional[str] = None
    last_reason: Optional[str] = None
    updated_at: Optional[str] = None


class LoopBreakerAlert(BaseModel):
    """Persisted alert generated when a loop breaker trips."""

    id: str = Field(default_factory=lambda: f"alert_{os.urandom(6).hex()}")
    target_id: str
    target_type: TargetType = "chain"
    severity: AlertSeverity = "critical"
    breaker_state: BreakerState = "open"
    reason: str
    details: dict[str, Any] = Field(default_factory=dict)
    acknowledged: bool = False
    acknowledged_by: Optional[str] = None
    acknowledged_at: Optional[str] = None
    resolved: bool = False
    resolved_at: Optional[str] = None
    created_at: Optional[str] = None


# ---------------------------------------------------------------------------
# Storage Wrapper (uses DatabaseStore without modifying store.py)
# ---------------------------------------------------------------------------


class RuleChainStorage:
    """SQLite/PostgreSQL persistence layer for Phase 19 tables via DatabaseStore."""

    def __init__(self, store: DatabaseStore):
        self.store = store

    # --- Chains ---

    @staticmethod
    def _row_to_chain(row) -> RuleChain:
        item = dict(row)
        raw_steps = item.get("steps", "[]")
        steps_data = json.loads(raw_steps) if isinstance(raw_steps, str) else raw_steps
        return RuleChain(
            id=item["id"],
            name=item["name"],
            description=item.get("description"),
            enabled=bool(item.get("enabled", 1)),
            steps=[ChainStep(**s) for s in steps_data],
            max_depth=int(item.get("max_depth", 10)),
            stop_on_error=bool(item.get("stop_on_error", 1)),
            trigger_limit=item.get("trigger_limit"),
            trigger_window_seconds=int(item.get("trigger_window_seconds", 60)),
            cooldown_seconds=int(item.get("cooldown_seconds", 0)),
            breaker_failure_threshold=int(item.get("breaker_failure_threshold", 3)),
            breaker_recovery_seconds=int(item.get("breaker_recovery_seconds", 60)),
            created_at=item.get("created_at"),
            updated_at=item.get("updated_at"),
        )

    def create_chain(self, chain: RuleChain) -> RuleChain:
        now = _utc_now().isoformat()
        created_at = chain.created_at or now
        updated_at = now
        sql = """
        INSERT INTO rule_chains (
            id, name, description, enabled, steps, max_depth, stop_on_error,
            trigger_limit, trigger_window_seconds, cooldown_seconds,
            breaker_failure_threshold, breaker_recovery_seconds,
            created_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (
                    chain.id,
                    chain.name,
                    chain.description,
                    1 if chain.enabled else 0,
                    json.dumps([s.model_dump() for s in chain.steps]),
                    int(chain.max_depth),
                    1 if chain.stop_on_error else 0,
                    chain.trigger_limit,
                    int(chain.trigger_window_seconds),
                    int(chain.cooldown_seconds),
                    int(chain.breaker_failure_threshold),
                    int(chain.breaker_recovery_seconds),
                    created_at,
                    updated_at,
                ),
            )
        return self.get_chain(chain.id)  # type: ignore[return-value]

    def get_chain(self, chain_id: str) -> RuleChain | None:
        sql = "SELECT * FROM rule_chains WHERE id = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (chain_id,)).fetchone()
            return self._row_to_chain(row) if row else None

    def list_chains(self, enabled_only: bool = False) -> list[RuleChain]:
        if enabled_only:
            sql = "SELECT * FROM rule_chains WHERE enabled = 1 ORDER BY created_at DESC"
        else:
            sql = "SELECT * FROM rule_chains ORDER BY created_at DESC"
        with self.store.get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            return [self._row_to_chain(r) for r in rows]

    def update_chain(self, chain: RuleChain) -> RuleChain | None:
        existing = self.get_chain(chain.id)
        if existing is None:
            return None
        now = _utc_now().isoformat()
        sql = """
        UPDATE rule_chains
        SET name = ?, description = ?, enabled = ?, steps = ?, max_depth = ?,
            stop_on_error = ?, trigger_limit = ?, trigger_window_seconds = ?,
            cooldown_seconds = ?, breaker_failure_threshold = ?,
            breaker_recovery_seconds = ?, updated_at = ?
        WHERE id = ?
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (
                    chain.name,
                    chain.description,
                    1 if chain.enabled else 0,
                    json.dumps([s.model_dump() for s in chain.steps]),
                    int(chain.max_depth),
                    1 if chain.stop_on_error else 0,
                    chain.trigger_limit,
                    int(chain.trigger_window_seconds),
                    int(chain.cooldown_seconds),
                    int(chain.breaker_failure_threshold),
                    int(chain.breaker_recovery_seconds),
                    now,
                    chain.id,
                ),
            )
        return self.get_chain(chain.id)

    def delete_chain(self, chain_id: str) -> bool:
        existing = self.get_chain(chain_id)
        if existing is None:
            return False
        with self.store.get_connection() as conn:
            conn.execute("DELETE FROM rule_chains WHERE id = ?", (chain_id,))
        return True

    # --- Rule Links ---

    def create_link(self, link: RuleLink) -> RuleLink:
        now = _utc_now().isoformat()
        sql = """
        INSERT INTO rule_chain_links (id, source_rule_id, target_rule_id, condition, created_at)
        VALUES (?, ?, ?, ?, ?)
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (link.id, link.source_rule_id, link.target_rule_id, link.condition, now),
            )
        return link.model_copy(update={"created_at": now})

    def list_links(self) -> list[RuleLink]:
        sql = "SELECT * FROM rule_chain_links ORDER BY created_at ASC"
        with self.store.get_connection() as conn:
            rows = conn.execute(sql).fetchall()
            return [RuleLink(**dict(r)) for r in rows]

    def delete_link(self, link_id: str) -> bool:
        with self.store.get_connection() as conn:
            row = conn.execute("SELECT id FROM rule_chain_links WHERE id = ?", (link_id,)).fetchone()
            if not row:
                return False
            conn.execute("DELETE FROM rule_chain_links WHERE id = ?", (link_id,))
            return True

    # --- Trigger Policies & Events ---

    def upsert_trigger_policy(self, policy: TriggerPolicy) -> TriggerPolicy:
        now = _utc_now().isoformat()
        sql = """
        INSERT INTO rule_trigger_policies (
            target_id, target_type, max_triggers, window_seconds,
            cooldown_seconds, last_triggered_at, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_id) DO UPDATE SET
            target_type = excluded.target_type,
            max_triggers = excluded.max_triggers,
            window_seconds = excluded.window_seconds,
            cooldown_seconds = excluded.cooldown_seconds,
            last_triggered_at = COALESCE(excluded.last_triggered_at, rule_trigger_policies.last_triggered_at),
            updated_at = excluded.updated_at
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (
                    policy.target_id,
                    policy.target_type,
                    int(policy.max_triggers),
                    int(policy.window_seconds),
                    int(policy.cooldown_seconds),
                    policy.last_triggered_at,
                    now,
                ),
            )
        return self.get_trigger_policy(policy.target_id)  # type: ignore[return-value]

    def get_trigger_policy(self, target_id: str) -> TriggerPolicy | None:
        sql = "SELECT * FROM rule_trigger_policies WHERE target_id = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (target_id,)).fetchone()
            return TriggerPolicy(**dict(row)) if row else None

    def update_policy_last_triggered(self, target_id: str, triggered_at_iso: str) -> None:
        with self.store.get_connection() as conn:
            conn.execute(
                "UPDATE rule_trigger_policies SET last_triggered_at = ?, updated_at = ? WHERE target_id = ?",
                (triggered_at_iso, _utc_now().isoformat(), target_id),
            )

    def record_trigger_event(
        self,
        target_id: str,
        target_type: TargetType,
        triggered_at_iso: str,
        *,
        suppressed: bool = False,
        reason: str | None = None,
    ) -> None:
        sql = """
        INSERT INTO rule_trigger_events (target_id, target_type, triggered_at, suppressed, reason)
        VALUES (?, ?, ?, ?, ?)
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (target_id, target_type, triggered_at_iso, 1 if suppressed else 0, reason),
            )

    def get_recent_trigger_events(
        self,
        target_id: str,
        since_iso: str,
        *,
        include_suppressed: bool = False,
    ) -> list[dict[str, Any]]:
        if include_suppressed:
            sql = """
            SELECT * FROM rule_trigger_events
            WHERE target_id = ? AND triggered_at >= ?
            ORDER BY triggered_at ASC
            """
        else:
            sql = """
            SELECT * FROM rule_trigger_events
            WHERE target_id = ? AND triggered_at >= ? AND suppressed = 0
            ORDER BY triggered_at ASC
            """
        with self.store.get_connection() as conn:
            rows = conn.execute(sql, (target_id, since_iso)).fetchall()
            return [dict(r) for r in rows]

    def get_last_allowed_trigger_time(self, target_id: str) -> str | None:
        sql = """
        SELECT triggered_at FROM rule_trigger_events
        WHERE target_id = ? AND suppressed = 0
        ORDER BY triggered_at DESC
        LIMIT 1
        """
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (target_id,)).fetchone()
            return row["triggered_at"] if row else None

    # --- Chain Executions ---

    def save_execution(self, result: ChainExecutionResult) -> ChainExecutionResult:
        now = _utc_now().isoformat()
        sql = """
        INSERT INTO rule_chain_executions (
            id, chain_id, chain_name, status, initial_context, final_context,
            step_results, error_message, started_at, finished_at, duration_ms, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (
                    result.execution_id,
                    result.chain_id,
                    result.chain_name,
                    result.status,
                    json.dumps(result.initial_context),
                    json.dumps(result.final_context),
                    json.dumps([sr.model_dump() for sr in result.step_results]),
                    result.error_message,
                    result.started_at,
                    result.finished_at,
                    float(result.duration_ms),
                    now,
                ),
            )
        return result

    @staticmethod
    def _row_to_execution(row) -> ChainExecutionResult:
        item = dict(row)
        initial_ctx = item.get("initial_context", "{}")
        final_ctx = item.get("final_context", "{}")
        steps_raw = item.get("step_results", "[]")
        return ChainExecutionResult(
            execution_id=item["id"],
            chain_id=item["chain_id"],
            chain_name=item["chain_name"],
            status=item["status"],
            initial_context=json.loads(initial_ctx) if isinstance(initial_ctx, str) else initial_ctx,
            final_context=json.loads(final_ctx) if isinstance(final_ctx, str) else final_ctx,
            step_results=[
                StepExecutionRecord(**sr)
                for sr in (json.loads(steps_raw) if isinstance(steps_raw, str) else steps_raw)
            ],
            error_message=item.get("error_message"),
            started_at=item["started_at"],
            finished_at=item.get("finished_at") or item["started_at"],
            duration_ms=float(item.get("duration_ms", 0.0)),
        )

    def get_execution(self, execution_id: str) -> ChainExecutionResult | None:
        sql = "SELECT * FROM rule_chain_executions WHERE id = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (execution_id,)).fetchone()
            return self._row_to_execution(row) if row else None

    def list_executions(
        self,
        *,
        chain_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ChainExecutionResult]:
        clauses: list[str] = []
        params: list[Any] = []
        if chain_id is not None:
            clauses.append("chain_id = ?")
            params.append(chain_id)
        if status is not None:
            clauses.append("status = ?")
            params.append(status)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT * FROM rule_chain_executions
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ? OFFSET ?
        """
        params.extend([limit, offset])
        with self.store.get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_execution(r) for r in rows]

    # --- Circuit Breakers & Alerts ---

    def get_breaker_state(self, target_id: str) -> CircuitBreakerState | None:
        sql = "SELECT * FROM loop_breaker_states WHERE target_id = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (target_id,)).fetchone()
            return CircuitBreakerState(**dict(row)) if row else None

    def list_breaker_states(self, state: BreakerState | None = None) -> list[CircuitBreakerState]:
        if state is not None:
            sql = "SELECT * FROM loop_breaker_states WHERE state = ? ORDER BY updated_at DESC"
            params: tuple = (state,)
        else:
            sql = "SELECT * FROM loop_breaker_states ORDER BY updated_at DESC"
            params = ()
        with self.store.get_connection() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [CircuitBreakerState(**dict(r)) for r in rows]

    def upsert_breaker_state(self, breaker: CircuitBreakerState) -> CircuitBreakerState:
        now = breaker.updated_at or _utc_now().isoformat()
        sql = """
        INSERT INTO loop_breaker_states (
            target_id, target_type, state, failure_count, loop_count,
            failure_threshold, recovery_timeout_seconds, last_failure_at,
            opened_at, half_open_at, last_reset_at, last_reason, updated_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_id) DO UPDATE SET
            target_type = excluded.target_type,
            state = excluded.state,
            failure_count = excluded.failure_count,
            loop_count = excluded.loop_count,
            failure_threshold = excluded.failure_threshold,
            recovery_timeout_seconds = excluded.recovery_timeout_seconds,
            last_failure_at = excluded.last_failure_at,
            opened_at = excluded.opened_at,
            half_open_at = excluded.half_open_at,
            last_reset_at = excluded.last_reset_at,
            last_reason = excluded.last_reason,
            updated_at = excluded.updated_at
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (
                    breaker.target_id,
                    breaker.target_type,
                    breaker.state,
                    int(breaker.failure_count),
                    int(breaker.loop_count),
                    int(breaker.failure_threshold),
                    int(breaker.recovery_timeout_seconds),
                    breaker.last_failure_at,
                    breaker.opened_at,
                    breaker.half_open_at,
                    breaker.last_reset_at,
                    breaker.last_reason,
                    now,
                ),
            )
        return self.get_breaker_state(breaker.target_id)  # type: ignore[return-value]

    @staticmethod
    def _row_to_alert(row) -> LoopBreakerAlert:
        item = dict(row)
        raw_details = item.get("details", "{}")
        item["details"] = json.loads(raw_details) if isinstance(raw_details, str) else raw_details
        item["acknowledged"] = bool(item.get("acknowledged", 0))
        item["resolved"] = bool(item.get("resolved", 0))
        return LoopBreakerAlert(**item)

    def create_alert(self, alert: LoopBreakerAlert) -> LoopBreakerAlert:
        now = alert.created_at or _utc_now().isoformat()
        sql = """
        INSERT INTO loop_breaker_alerts (
            id, target_id, target_type, severity, breaker_state, reason,
            details, acknowledged, acknowledged_by, acknowledged_at,
            resolved, resolved_at, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self.store.get_connection() as conn:
            conn.execute(
                sql,
                (
                    alert.id,
                    alert.target_id,
                    alert.target_type,
                    alert.severity,
                    alert.breaker_state,
                    alert.reason,
                    json.dumps(alert.details),
                    1 if alert.acknowledged else 0,
                    alert.acknowledged_by,
                    alert.acknowledged_at,
                    1 if alert.resolved else 0,
                    alert.resolved_at,
                    now,
                ),
            )
        return self.get_alert(alert.id)  # type: ignore[return-value]

    def get_alert(self, alert_id: str) -> LoopBreakerAlert | None:
        sql = "SELECT * FROM loop_breaker_alerts WHERE id = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (alert_id,)).fetchone()
            return self._row_to_alert(row) if row else None

    def list_alerts(
        self,
        *,
        target_id: str | None = None,
        acknowledged: bool | None = None,
        resolved: bool | None = None,
        limit: int = 50,
    ) -> list[LoopBreakerAlert]:
        clauses: list[str] = []
        params: list[Any] = []
        if target_id is not None:
            clauses.append("target_id = ?")
            params.append(target_id)
        if acknowledged is not None:
            clauses.append("acknowledged = ?")
            params.append(1 if acknowledged else 0)
        if resolved is not None:
            clauses.append("resolved = ?")
            params.append(1 if resolved else 0)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
        SELECT * FROM loop_breaker_alerts
        {where_sql}
        ORDER BY created_at DESC
        LIMIT ?
        """
        params.append(limit)
        with self.store.get_connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_alert(r) for r in rows]

    def acknowledge_alert(
        self, alert_id: str, acknowledged_by: str = "operator", now_iso: str | None = None
    ) -> LoopBreakerAlert | None:
        existing = self.get_alert(alert_id)
        if existing is None:
            return None
        ts = now_iso or _utc_now().isoformat()
        with self.store.get_connection() as conn:
            conn.execute(
                """
                UPDATE loop_breaker_alerts
                SET acknowledged = 1, acknowledged_by = ?, acknowledged_at = ?
                WHERE id = ?
                """,
                (acknowledged_by, ts, alert_id),
            )
        return self.get_alert(alert_id)

    def resolve_alerts_for_target(self, target_id: str, now_iso: str | None = None) -> int:
        ts = now_iso or _utc_now().isoformat()
        with self.store.get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE loop_breaker_alerts
                SET resolved = 1, resolved_at = ?
                WHERE target_id = ? AND resolved = 0
                """,
                (ts, target_id),
            )
            return getattr(cursor, "rowcount", 0) or 0

    # --- Read-only lookups on existing Nova tables (for F19.4) ---

    def get_automation_rule(self, rule_id: str) -> dict[str, Any] | None:
        sql = "SELECT * FROM automation_rules WHERE id = ?"
        with self.store.get_connection() as conn:
            row = conn.execute(sql, (rule_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            if isinstance(res.get("condition"), str):
                res["condition"] = json.loads(res["condition"])
            if isinstance(res.get("action_template"), str):
                res["action_template"] = json.loads(res["action_template"])
            return res

    def get_device(self, device_id: str) -> dict[str, Any] | None:
        return self.store.get_device(device_id)


# ---------------------------------------------------------------------------
# F19.2: Circular Reference Detection & Runtime Loop Guard
# ---------------------------------------------------------------------------


class CircularReferenceDetector:
    """Static 3-color DFS cycle detector for chains, step jumps, and rule links."""

    @staticmethod
    def detect_cycle(adjacency: dict[str, list[str]]) -> list[str] | None:
        """Returns the first cycle path [u, ..., u] if a directed cycle exists, else None."""
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {}
        stack: list[str] = []
        index_in_stack: dict[str, int] = {}

        nodes = list(adjacency.keys())
        for neighbors in adjacency.values():
            for n in neighbors:
                if n not in color:
                    nodes.append(n)

        # Deduplicate while preserving deterministic order
        seen_nodes: list[str] = []
        seen_set: set[str] = set()
        for n in nodes:
            if n not in seen_set:
                seen_set.add(n)
                seen_nodes.append(n)

        def dfs(u: str) -> list[str] | None:
            color[u] = GRAY
            index_in_stack[u] = len(stack)
            stack.append(u)

            for v in adjacency.get(u, []):
                state = color.get(v, WHITE)
                if state == GRAY:
                    start_idx = index_in_stack[v]
                    return stack[start_idx:] + [v]
                if state == WHITE:
                    cycle = dfs(v)
                    if cycle is not None:
                        return cycle

            stack.pop()
            del index_in_stack[u]
            color[u] = BLACK
            return None

        for node in seen_nodes:
            if color.get(node, WHITE) == WHITE:
                cycle = dfs(node)
                if cycle is not None:
                    return cycle
        return None

    @classmethod
    def check_step_transitions(cls, chain: RuleChain) -> list[str] | None:
        """Detects cyclic step transitions (self-loops or backward jumps) within a chain."""
        adj: dict[str, list[str]] = {}
        step_ids = [s.step_id for s in chain.steps]
        step_id_set = set(step_ids)

        for idx, step in enumerate(chain.steps):
            edges: list[str] = []
            next_seq = step_ids[idx + 1] if idx + 1 < len(step_ids) else None

            for directive in (step.on_success, step.on_failure):
                if directive == "continue":
                    if next_seq is not None and next_seq not in edges:
                        edges.append(next_seq)
                elif directive == "stop":
                    continue
                elif directive in step_id_set:
                    if directive not in edges:
                        edges.append(directive)

            adj[step.step_id] = edges

        return cls.detect_cycle(adj)

    @classmethod
    def check_chain_graph(
        cls,
        candidate: RuleChain,
        existing_chains: list[RuleChain],
        rule_links: list[RuleLink] | None = None,
    ) -> list[str] | None:
        """Checks for cycles in:
        1. Intra-chain step transitions (`on_success` / `on_failure` jumps).
        2. Inter-chain sub-chain references (`chain:A -> chain:B -> ... -> chain:A`).
        3. Rule-link graph (if `rule_links` provided).
        """
        step_cycle = cls.check_step_transitions(candidate)
        if step_cycle is not None:
            return step_cycle

        # Build chain -> sub_chain adjacency
        chains_by_id: dict[str, RuleChain] = {c.id: c for c in existing_chains}
        chains_by_id[candidate.id] = candidate

        chain_adj: dict[str, list[str]] = {}
        for cid, ch in chains_by_id.items():
            targets: list[str] = []
            for step in ch.steps:
                if step.sub_chain_id and step.sub_chain_id not in targets:
                    targets.append(step.sub_chain_id)
            chain_adj[cid] = targets

        chain_cycle = cls.detect_cycle(chain_adj)
        if chain_cycle is not None:
            return chain_cycle

        if rule_links:
            link_cycle = cls.check_rule_links(rule_links)
            if link_cycle is not None:
                return link_cycle

        return None

    @classmethod
    def check_rule_links(cls, links: list[RuleLink]) -> list[str] | None:
        """Detects direct (A -> A) or indirect (A -> B -> C -> A) cycles in rule links."""
        adj: dict[str, list[str]] = {}
        for link in links:
            adj.setdefault(link.source_rule_id, [])
            if link.target_rule_id not in adj[link.source_rule_id]:
                adj[link.source_rule_id].append(link.target_rule_id)
        return cls.detect_cycle(adj)

    @classmethod
    def validate_new_link(
        cls,
        source_rule_id: str,
        target_rule_id: str,
        existing_links: list[RuleLink],
    ) -> None:
        """Raises CircularReferenceError if adding source -> target creates a cycle."""
        if source_rule_id == target_rule_id:
            raise CircularReferenceError([source_rule_id, target_rule_id])
        proposed = list(existing_links) + [
            RuleLink(source_rule_id=source_rule_id, target_rule_id=target_rule_id)
        ]
        cycle = cls.check_rule_links(proposed)
        if cycle is not None:
            raise CircularReferenceError(cycle)


class RuntimeLoopGuard:
    """Runtime visited-set and max-depth protection during chain execution."""

    def __init__(self, max_depth: int = 10):
        self.max_depth = max_depth
        self.visited: set[str] = set()
        self.call_stack: list[str] = []

    def enter(self, node_id: str) -> None:
        if node_id in self.visited:
            if node_id in self.call_stack:
                idx = self.call_stack.index(node_id)
                cycle = self.call_stack[idx:] + [node_id]
            else:
                cycle = self.call_stack + [node_id]
            raise CircularReferenceError(cycle, f"Runtime loop detected at '{node_id}': {' -> '.join(cycle)}")

        if len(self.call_stack) >= self.max_depth:
            raise MaxDepthExceededError(self.max_depth, self.call_stack + [node_id])

        self.visited.add(node_id)
        self.call_stack.append(node_id)

    def exit(self, node_id: str) -> None:
        if self.call_stack and self.call_stack[-1] == node_id:
            self.call_stack.pop()


# ---------------------------------------------------------------------------
# F19.3: Trigger Rate Limits & Cooldown Guard
# ---------------------------------------------------------------------------


class TriggerRateLimitGuard:
    """Sliding-window trigger rate limiter and cooldown enforcer."""

    def __init__(
        self,
        storage: RuleChainStorage,
        now_fn: Callable[[], datetime] = _utc_now,
    ):
        self.storage = storage
        self.now_fn = now_fn

    def set_policy(
        self,
        target_id: str,
        *,
        target_type: TargetType = "rule",
        max_triggers: int = 10,
        window_seconds: int = 60,
        cooldown_seconds: int = 0,
    ) -> TriggerPolicy:
        policy = TriggerPolicy(
            target_id=target_id,
            target_type=target_type,
            max_triggers=max_triggers,
            window_seconds=window_seconds,
            cooldown_seconds=cooldown_seconds,
        )
        return self.storage.upsert_trigger_policy(policy)

    def get_policy(self, target_id: str) -> TriggerPolicy | None:
        return self.storage.get_trigger_policy(target_id)

    def check_and_record(
        self,
        target_id: str,
        *,
        target_type: TargetType = "rule",
        max_triggers: int | None = None,
        window_seconds: int | None = None,
        cooldown_seconds: int | None = None,
        now: datetime | None = None,
        record: bool = True,
    ) -> TriggerCheckResult:
        """Evaluates cooldown & sliding-window rate limits, optionally recording the event."""
        current = _to_utc(now) or self.now_fn()
        policy = self.storage.get_trigger_policy(target_id)

        eff_max = max_triggers if max_triggers is not None else (policy.max_triggers if policy else None)
        eff_window = (
            window_seconds
            if window_seconds is not None
            else (policy.window_seconds if policy else 60)
        )
        eff_cooldown = (
            cooldown_seconds
            if cooldown_seconds is not None
            else (policy.cooldown_seconds if policy else 0)
        )

        # If neither limit nor cooldown is active, allow immediately
        if eff_max is None and eff_cooldown <= 0:
            if record:
                self.storage.record_trigger_event(
                    target_id, target_type, current.isoformat(), suppressed=False
                )
            return TriggerCheckResult(
                allowed=True,
                target_id=target_id,
                target_type=target_type,
                triggers_in_window=0,
                max_triggers=None,
                window_seconds=eff_window,
                cooldown_seconds=0,
            )

        # 1. Cooldown check
        last_iso = (
            policy.last_triggered_at if (policy and policy.last_triggered_at) else None
        ) or self.storage.get_last_allowed_trigger_time(target_id)

        if eff_cooldown > 0 and last_iso is not None:
            last_dt = _to_utc(last_iso)
            if last_dt is not None:
                elapsed = (current - last_dt).total_seconds()
                if elapsed < eff_cooldown:
                    remaining = max(0.0, eff_cooldown - elapsed)
                    if record:
                        self.storage.record_trigger_event(
                            target_id,
                            target_type,
                            current.isoformat(),
                            suppressed=True,
                            reason="cooldown_active",
                        )
                    return TriggerCheckResult(
                        allowed=False,
                        target_id=target_id,
                        target_type=target_type,
                        reason="cooldown_active",
                        retry_after_seconds=round(remaining, 3),
                        max_triggers=eff_max,
                        window_seconds=eff_window,
                        cooldown_seconds=eff_cooldown,
                        cooldown_remaining_seconds=round(remaining, 3),
                    )

        # 2. Sliding time window check
        window_start = current - timedelta(seconds=eff_window)
        recent = self.storage.get_recent_trigger_events(
            target_id, window_start.isoformat(), include_suppressed=False
        )
        # Filter strictly within (window_start, current] to avoid floating/boundary issues
        in_window: list[datetime] = []
        for evt in recent:
            evt_dt = _to_utc(evt["triggered_at"])
            if evt_dt is not None and evt_dt > window_start and evt_dt <= current:
                in_window.append(evt_dt)

        if eff_max is not None and len(in_window) >= eff_max:
            oldest = min(in_window)
            retry_after = max(0.0, (oldest + timedelta(seconds=eff_window) - current).total_seconds())
            if record:
                self.storage.record_trigger_event(
                    target_id,
                    target_type,
                    current.isoformat(),
                    suppressed=True,
                    reason="rate_limit_exceeded",
                )
            return TriggerCheckResult(
                allowed=False,
                target_id=target_id,
                target_type=target_type,
                reason="rate_limit_exceeded",
                retry_after_seconds=round(retry_after, 3),
                triggers_in_window=len(in_window),
                max_triggers=eff_max,
                window_seconds=eff_window,
                cooldown_seconds=eff_cooldown,
                cooldown_remaining_seconds=0.0,
            )

        # 3. Allowed — record event & update last_triggered_at
        if record:
            now_iso = current.isoformat()
            self.storage.record_trigger_event(
                target_id, target_type, now_iso, suppressed=False
            )
            if policy is not None:
                self.storage.update_policy_last_triggered(target_id, now_iso)

        return TriggerCheckResult(
            allowed=True,
            target_id=target_id,
            target_type=target_type,
            triggers_in_window=len(in_window) + (1 if record else 0),
            max_triggers=eff_max,
            window_seconds=eff_window,
            cooldown_seconds=eff_cooldown,
            cooldown_remaining_seconds=0.0,
        )


# ---------------------------------------------------------------------------
# F19.4: Pre-Execution Dependency Validator
# ---------------------------------------------------------------------------


class DependencyValidator:
    """Validates rules, conditions, devices, capabilities, and sub-chains before execution."""

    def __init__(self, storage: RuleChainStorage):
        self.storage = storage

    def validate_chain(
        self,
        chain: RuleChain,
        *,
        check_chain_enabled: bool = True,
    ) -> DependencyValidationResult:
        issues: list[DependencyIssue] = []
        checked_rules = 0
        checked_devices = 0
        checked_capabilities = 0
        checked_conditions = 0

        if check_chain_enabled and not chain.enabled:
            issues.append(
                DependencyIssue(
                    code="CHAIN_DISABLED",
                    resource_type="chain",
                    resource_id=chain.id,
                    message=f"Rule chain '{chain.id}' is disabled.",
                )
            )

        if not chain.steps:
            issues.append(
                DependencyIssue(
                    code="EMPTY_CHAIN",
                    resource_type="chain",
                    resource_id=chain.id,
                    message=f"Rule chain '{chain.id}' has no steps configured.",
                )
            )

        step_ids = {s.step_id for s in chain.steps}

        for step in chain.steps:
            if not step.enabled:
                continue

            # Validate branching targets
            for branch_name, target in (("on_success", step.on_success), ("on_failure", step.on_failure)):
                if target not in ("continue", "stop") and target not in step_ids:
                    issues.append(
                        DependencyIssue(
                            code="INVALID_STEP_TARGET",
                            step_id=step.step_id,
                            resource_type="step",
                            resource_id=target,
                            message=(
                                f"Step '{step.step_id}' {branch_name} references unknown step '{target}'."
                            ),
                        )
                    )

            # Sub-chain dependency
            if step.sub_chain_id:
                sub = self.storage.get_chain(step.sub_chain_id)
                if sub is None:
                    issues.append(
                        DependencyIssue(
                            code="SUB_CHAIN_NOT_FOUND",
                            step_id=step.step_id,
                            resource_type="chain",
                            resource_id=step.sub_chain_id,
                            message=f"Referenced sub-chain '{step.sub_chain_id}' does not exist.",
                        )
                    )
                elif not sub.enabled:
                    issues.append(
                        DependencyIssue(
                            code="SUB_CHAIN_DISABLED",
                            step_id=step.step_id,
                            resource_type="chain",
                            resource_id=step.sub_chain_id,
                            message=f"Referenced sub-chain '{step.sub_chain_id}' is disabled.",
                        )
                    )
                continue

            # Rule dependency
            if step.rule_id:
                checked_rules += 1
                rule = self.storage.get_automation_rule(step.rule_id)
                if rule is None:
                    issues.append(
                        DependencyIssue(
                            code="RULE_NOT_FOUND",
                            step_id=step.step_id,
                            resource_type="rule",
                            resource_id=step.rule_id,
                            message=f"Referenced automation rule '{step.rule_id}' does not exist.",
                        )
                    )
                    continue

                if int(rule.get("enabled", 0)) != 1:
                    issues.append(
                        DependencyIssue(
                            code="RULE_DISABLED",
                            step_id=step.step_id,
                            resource_type="rule",
                            resource_id=step.rule_id,
                            message=f"Referenced automation rule '{step.rule_id}' is disabled.",
                        )
                    )

                # Condition validation
                checked_conditions += 1
                cond_data = rule.get("condition")
                if not isinstance(cond_data, dict):
                    issues.append(
                        DependencyIssue(
                            code="INVALID_CONDITION",
                            step_id=step.step_id,
                            resource_type="condition",
                            resource_id=step.rule_id,
                            message=f"Rule '{step.rule_id}' condition must be a JSON object.",
                        )
                    )
                else:
                    try:
                        parsed_cond = parse_condition(cond_data)
                        if isinstance(parsed_cond, TimeCondition):
                            if parsed_cond.time_of_day is not None and not _TIME_HHMM_RE.match(
                                parsed_cond.time_of_day
                            ):
                                issues.append(
                                    DependencyIssue(
                                        code="INVALID_CONDITION",
                                        step_id=step.step_id,
                                        resource_type="condition",
                                        resource_id=step.rule_id,
                                        message=(
                                            f"Rule '{step.rule_id}' has invalid time_of_day "
                                            f"'{parsed_cond.time_of_day}' (expected HH:MM)."
                                        ),
                                    )
                                )
                            if (
                                parsed_cond.interval_minutes is not None
                                and parsed_cond.interval_minutes <= 0
                            ):
                                issues.append(
                                    DependencyIssue(
                                        code="INVALID_CONDITION",
                                        step_id=step.step_id,
                                        resource_type="condition",
                                        resource_id=step.rule_id,
                                        message=(
                                            f"Rule '{step.rule_id}' interval_minutes must be positive."
                                        ),
                                    )
                                )
                    except Exception as exc:
                        issues.append(
                            DependencyIssue(
                                code="INVALID_CONDITION",
                                step_id=step.step_id,
                                resource_type="condition",
                                resource_id=step.rule_id,
                                message=f"Rule '{step.rule_id}' has invalid condition: {exc}",
                            )
                        )

                # Device & capability validation
                action_template = rule.get("action_template") or {}
                device_id = step.required_device_id or action_template.get("source_device_id")
                required_caps = list(step.required_capabilities)
                template_caps = action_template.get("required_capabilities")
                if isinstance(template_caps, list):
                    for cap in template_caps:
                        if isinstance(cap, str) and cap not in required_caps:
                            required_caps.append(cap)

                if device_id:
                    checked_devices += 1
                    device = self.storage.get_device(device_id)
                    if device is None:
                        issues.append(
                            DependencyIssue(
                                code="DEVICE_NOT_FOUND",
                                step_id=step.step_id,
                                resource_type="device",
                                resource_id=device_id,
                                message=f"Referenced device '{device_id}' does not exist.",
                            )
                        )
                    else:
                        device_caps = device.get("capabilities") or {}
                        for cap in required_caps:
                            checked_capabilities += 1
                            if isinstance(device_caps, dict):
                                if cap not in device_caps:
                                    issues.append(
                                        DependencyIssue(
                                            code="CAPABILITY_MISSING",
                                            step_id=step.step_id,
                                            resource_type="capability",
                                            resource_id=f"{device_id}:{cap}",
                                            message=(
                                                f"Device '{device_id}' is missing required capability '{cap}'."
                                            ),
                                        )
                                    )
                                elif not bool(device_caps[cap]):
                                    issues.append(
                                        DependencyIssue(
                                            code="CAPABILITY_DISABLED",
                                            step_id=step.step_id,
                                            resource_type="capability",
                                            resource_id=f"{device_id}:{cap}",
                                            message=(
                                                f"Device '{device_id}' capability '{cap}' is disabled."
                                            ),
                                        )
                                    )
                            elif isinstance(device_caps, list):
                                if cap not in device_caps:
                                    issues.append(
                                        DependencyIssue(
                                            code="CAPABILITY_MISSING",
                                            step_id=step.step_id,
                                            resource_type="capability",
                                            resource_id=f"{device_id}:{cap}",
                                            message=(
                                                f"Device '{device_id}' is missing required capability '{cap}'."
                                            ),
                                        )
                                    )

        has_errors = any(issue.severity == "error" for issue in issues)
        return DependencyValidationResult(
            valid=not has_errors,
            chain_id=chain.id,
            issues=issues,
            checked_rules=checked_rules,
            checked_devices=checked_devices,
            checked_capabilities=checked_capabilities,
            checked_conditions=checked_conditions,
        )


# ---------------------------------------------------------------------------
# F19.5: Automation Loop Breaker & Persisted Alerts
# ---------------------------------------------------------------------------


class AutomationLoopBreaker:
    """Per-chain/per-rule circuit breaker with persisted states and alerts."""

    def __init__(
        self,
        storage: RuleChainStorage,
        now_fn: Callable[[], datetime] = _utc_now,
    ):
        self.storage = storage
        self.now_fn = now_fn

    def get_state(
        self,
        target_id: str,
        *,
        target_type: TargetType = "chain",
        failure_threshold: int = 3,
        recovery_timeout_seconds: int = 60,
    ) -> CircuitBreakerState:
        existing = self.storage.get_breaker_state(target_id)
        if existing is not None:
            return existing
        return CircuitBreakerState(
            target_id=target_id,
            target_type=target_type,
            state="closed",
            failure_count=0,
            loop_count=0,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
            updated_at=self.now_fn().isoformat(),
        )

    def check_allowed(
        self,
        target_id: str,
        *,
        target_type: TargetType = "chain",
        failure_threshold: int = 3,
        recovery_timeout_seconds: int = 60,
        now: datetime | None = None,
    ) -> tuple[bool, CircuitBreakerState, float]:
        """Checks whether execution is permitted, transitioning open -> half_open after recovery timeout."""
        current = _to_utc(now) or self.now_fn()
        state = self.get_state(
            target_id,
            target_type=target_type,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
        )

        if state.state == "closed":
            return True, state, 0.0

        if state.state == "half_open":
            return True, state, 0.0

        # state == "open"
        opened_dt = _to_utc(state.opened_at)
        if opened_dt is not None:
            elapsed = (current - opened_dt).total_seconds()
            if elapsed >= state.recovery_timeout_seconds:
                updated = state.model_copy(
                    update={
                        "state": "half_open",
                        "half_open_at": current.isoformat(),
                        "updated_at": current.isoformat(),
                    }
                )
                saved = self.storage.upsert_breaker_state(updated)
                return True, saved, 0.0
            retry_after = max(0.0, state.recovery_timeout_seconds - elapsed)
            return False, state, round(retry_after, 3)

        return False, state, float(state.recovery_timeout_seconds)

    def record_success(
        self,
        target_id: str,
        *,
        target_type: TargetType = "chain",
        failure_threshold: int = 3,
        recovery_timeout_seconds: int = 60,
        now: datetime | None = None,
    ) -> CircuitBreakerState:
        current = _to_utc(now) or self.now_fn()
        state = self.get_state(
            target_id,
            target_type=target_type,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
        )
        updated = state.model_copy(
            update={
                "state": "closed",
                "failure_count": 0,
                "updated_at": current.isoformat(),
            }
        )
        return self.storage.upsert_breaker_state(updated)

    def record_failure(
        self,
        target_id: str,
        reason: str,
        *,
        target_type: TargetType = "chain",
        is_loop: bool = False,
        details: dict[str, Any] | None = None,
        failure_threshold: int = 3,
        recovery_timeout_seconds: int = 60,
        now: datetime | None = None,
    ) -> tuple[CircuitBreakerState, LoopBreakerAlert | None]:
        """Records a failure or loop event; trips to 'open' and emits a persisted alert when threshold met."""
        current = _to_utc(now) or self.now_fn()
        now_iso = current.isoformat()
        state = self.get_state(
            target_id,
            target_type=target_type,
            failure_threshold=failure_threshold,
            recovery_timeout_seconds=recovery_timeout_seconds,
        )

        new_failures = state.failure_count + 1
        new_loops = state.loop_count + (1 if is_loop else 0)
        eff_threshold = failure_threshold or state.failure_threshold
        eff_recovery = recovery_timeout_seconds or state.recovery_timeout_seconds

        should_trip = is_loop or state.state == "half_open" or new_failures >= eff_threshold
        new_state: BreakerState = "open" if should_trip else state.state

        updated = state.model_copy(
            update={
                "target_type": target_type,
                "state": new_state,
                "failure_count": new_failures,
                "loop_count": new_loops,
                "failure_threshold": eff_threshold,
                "recovery_timeout_seconds": eff_recovery,
                "last_failure_at": now_iso,
                "opened_at": now_iso if should_trip else state.opened_at,
                "last_reason": reason,
                "updated_at": now_iso,
            }
        )
        saved_state = self.storage.upsert_breaker_state(updated)

        alert: LoopBreakerAlert | None = None
        if should_trip:
            alert = self.storage.create_alert(
                LoopBreakerAlert(
                    target_id=target_id,
                    target_type=target_type,
                    severity="critical" if is_loop else "warning",
                    breaker_state="open",
                    reason=reason,
                    details={
                        "is_loop": is_loop,
                        "failure_count": new_failures,
                        "loop_count": new_loops,
                        "failure_threshold": eff_threshold,
                        **(details or {}),
                    },
                    created_at=now_iso,
                )
            )
            logger.warning(
                "Loop breaker tripped for %s '%s': %s (alert=%s)",
                target_type,
                target_id,
                reason,
                alert.id,
            )

        return saved_state, alert

    def reset(
        self,
        target_id: str,
        *,
        target_type: TargetType = "chain",
        now: datetime | None = None,
    ) -> CircuitBreakerState:
        """Manually resets a tripped circuit breaker back to 'closed' and resolves open alerts."""
        current = _to_utc(now) or self.now_fn()
        now_iso = current.isoformat()
        state = self.get_state(target_id, target_type=target_type)
        updated = state.model_copy(
            update={
                "state": "closed",
                "failure_count": 0,
                "opened_at": None,
                "half_open_at": None,
                "last_reset_at": now_iso,
                "updated_at": now_iso,
            }
        )
        saved = self.storage.upsert_breaker_state(updated)
        self.storage.resolve_alerts_for_target(target_id, now_iso=now_iso)
        return saved

    def list_states(self, state: BreakerState | None = None) -> list[CircuitBreakerState]:
        return self.storage.list_breaker_states(state=state)

    def list_alerts(
        self,
        *,
        target_id: str | None = None,
        acknowledged: bool | None = None,
        resolved: bool | None = None,
        limit: int = 50,
    ) -> list[LoopBreakerAlert]:
        return self.storage.list_alerts(
            target_id=target_id,
            acknowledged=acknowledged,
            resolved=resolved,
            limit=limit,
        )

    def acknowledge_alert(
        self, alert_id: str, *, acknowledged_by: str = "operator"
    ) -> LoopBreakerAlert | None:
        return self.storage.acknowledge_alert(
            alert_id,
            acknowledged_by=acknowledged_by,
            now_iso=self.now_fn().isoformat(),
        )


# ---------------------------------------------------------------------------
# F19.1–F19.5: RuleChainService Orchestrator
# ---------------------------------------------------------------------------


StepExecutorFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[dict[str, Any]] | dict[str, Any]]


def _resolve_template_value(value: Any, context: dict[str, Any]) -> Any:
    if not isinstance(value, str):
        return value
    m_ctx = _CONTEXT_PLACEHOLDER_RE.match(value)
    if m_ctx:
        key = m_ctx.group(1)
        return context.get(key, value)
    m_step = _STEP_PLACEHOLDER_RE.match(value)
    if m_step:
        sid, key = m_step.group(1), m_step.group(2)
        step_out = (context.get("step_outputs") or {}).get(sid)
        if isinstance(step_out, dict) and key in step_out:
            return step_out[key]
    return value


def _default_step_executor(rule: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Default deterministic step executor when no external engine callback is attached."""
    template = rule.get("action_template") or {}
    raw_params = template.get("params") or {}
    resolved_params = {k: _resolve_template_value(v, context) for k, v in raw_params.items()}
    return {
        "success": True,
        "rule_id": rule["id"],
        "rule_name": rule.get("name"),
        "action_type": template.get("action_type"),
        "category": template.get("category"),
        "params": resolved_params,
    }


class RuleChainService:
    """High-level service coordinating rule chains, cycle checks, rate limits,
    dependency validation, and loop breaker alerts."""

    def __init__(
        self,
        store: DatabaseStore,
        *,
        step_executor: StepExecutorFn | None = None,
        now_fn: Callable[[], datetime] = _utc_now,
    ):
        self.store = store
        self.storage = RuleChainStorage(store)
        self.now_fn = now_fn
        self.rate_limiter = TriggerRateLimitGuard(self.storage, now_fn=now_fn)
        self.validator = DependencyValidator(self.storage)
        self.breaker = AutomationLoopBreaker(self.storage, now_fn=now_fn)
        self.step_executor: StepExecutorFn = step_executor or _default_step_executor

    # --- Chain CRUD with F19.2 Static Cycle Detection ---

    def create_chain(self, chain: RuleChain) -> RuleChain:
        existing_chains = self.storage.list_chains()
        rule_links = self.storage.list_links()
        cycle = CircularReferenceDetector.check_chain_graph(chain, existing_chains, rule_links)
        if cycle is not None:
            raise CircularReferenceError(cycle)
        return self.storage.create_chain(chain)

    def get_chain(self, chain_id: str) -> RuleChain | None:
        return self.storage.get_chain(chain_id)

    def list_chains(self, enabled_only: bool = False) -> list[RuleChain]:
        return self.storage.list_chains(enabled_only=enabled_only)

    def update_chain(self, chain: RuleChain) -> RuleChain | None:
        existing = self.storage.get_chain(chain.id)
        if existing is None:
            return None
        existing_chains = self.storage.list_chains()
        rule_links = self.storage.list_links()
        cycle = CircularReferenceDetector.check_chain_graph(chain, existing_chains, rule_links)
        if cycle is not None:
            raise CircularReferenceError(cycle)
        return self.storage.update_chain(chain)

    def delete_chain(self, chain_id: str) -> bool:
        return self.storage.delete_chain(chain_id)

    # --- Rule Links with F19.2 Static Cycle Detection ---

    def create_rule_link(
        self,
        source_rule_id: str,
        target_rule_id: str,
        condition: LinkCondition = "on_success",
    ) -> RuleLink:
        existing_links = self.storage.list_links()
        CircularReferenceDetector.validate_new_link(source_rule_id, target_rule_id, existing_links)
        link = RuleLink(
            source_rule_id=source_rule_id,
            target_rule_id=target_rule_id,
            condition=condition,
        )
        return self.storage.create_link(link)

    def list_rule_links(self) -> list[RuleLink]:
        return self.storage.list_links()

    def delete_rule_link(self, link_id: str) -> bool:
        return self.storage.delete_link(link_id)

    def check_cycles(self, candidate_chain: RuleChain | None = None) -> dict[str, Any]:
        """Runs static DFS cycle checks across chains and rule links."""
        existing_chains = self.storage.list_chains()
        rule_links = self.storage.list_links()

        if candidate_chain is not None:
            cycle = CircularReferenceDetector.check_chain_graph(
                candidate_chain, existing_chains, rule_links
            )
            return {
                "has_cycle": cycle is not None,
                "cycle_path": cycle or [],
            }

        link_cycle = CircularReferenceDetector.check_rule_links(rule_links)
        if link_cycle is not None:
            return {"has_cycle": True, "cycle_path": link_cycle}

        for ch in existing_chains:
            cycle = CircularReferenceDetector.check_chain_graph(ch, existing_chains, rule_links)
            if cycle is not None:
                return {"has_cycle": True, "cycle_path": cycle}

        return {"has_cycle": False, "cycle_path": []}

    # --- F19.4 Pre-Execution Validation ---

    def validate_chain(
        self, chain_id: str, *, check_chain_enabled: bool = True
    ) -> DependencyValidationResult:
        chain = self.storage.get_chain(chain_id)
        if chain is None:
            raise KeyError(f"Rule chain '{chain_id}' not found.")
        return self.validator.validate_chain(chain, check_chain_enabled=check_chain_enabled)

    # --- F19.1 Sequential Chain Execution ---

    async def execute_chain(
        self,
        chain_id: str,
        initial_context: dict[str, Any] | None = None,
        *,
        dry_run: bool = False,
        skip_validation: bool = False,
        now: datetime | None = None,
        _guard: RuntimeLoopGuard | None = None,
    ) -> ChainExecutionResult:
        chain = self.storage.get_chain(chain_id)
        if chain is None:
            raise KeyError(f"Rule chain '{chain_id}' not found.")

        start_perf = time.perf_counter()
        current = _to_utc(now) or self.now_fn()
        started_at = current.isoformat()
        execution_id = f"exec_{os.urandom(6).hex()}"
        init_ctx = copy.deepcopy(initial_context or {})
        context = copy.deepcopy(init_ctx)
        context.setdefault("step_outputs", {})

        def _finish(
            status: str,
            step_results: list[StepExecutionRecord],
            *,
            error_message: str | None = None,
            validation: DependencyValidationResult | None = None,
            breaker_tripped: bool = False,
            persist: bool = True,
        ) -> ChainExecutionResult:
            finished_at = (self.now_fn() if now is None else current).isoformat()
            duration_ms = round((time.perf_counter() - start_perf) * 1000.0, 3)
            res = ChainExecutionResult(
                execution_id=execution_id,
                chain_id=chain.id,
                chain_name=chain.name,
                status=status,
                initial_context=init_ctx,
                final_context=context,
                step_results=step_results,
                error_message=error_message,
                started_at=started_at,
                finished_at=finished_at,
                duration_ms=duration_ms,
                validation=validation,
                breaker_tripped=breaker_tripped,
            )
            if persist and _guard is None:
                self.storage.save_execution(res)
            return res

        # 1. Circuit breaker check (F19.5)
        if not dry_run:
            allowed_by_breaker, breaker_state, retry_after = self.breaker.check_allowed(
                chain.id,
                target_type="chain",
                failure_threshold=chain.breaker_failure_threshold,
                recovery_timeout_seconds=chain.breaker_recovery_seconds,
                now=current,
            )
            if not allowed_by_breaker:
                return _finish(
                    "blocked",
                    [],
                    error_message=(
                        f"Circuit breaker for chain '{chain.id}' is {breaker_state.state}; "
                        f"retry after {retry_after:.2f}s."
                    ),
                )

        # 2. Chain-level trigger rate limit & cooldown check (F19.3)
        if not dry_run:
            policy = self.rate_limiter.get_policy(chain.id)
            eff_limit = chain.trigger_limit if chain.trigger_limit is not None else (
                policy.max_triggers if policy else None
            )
            eff_window = chain.trigger_window_seconds if chain.trigger_limit is not None else (
                policy.window_seconds if policy else chain.trigger_window_seconds
            )
            eff_cooldown = chain.cooldown_seconds if chain.cooldown_seconds > 0 else (
                policy.cooldown_seconds if policy else 0
            )
            if eff_limit is not None or eff_cooldown > 0:
                check = self.rate_limiter.check_and_record(
                    chain.id,
                    target_type="chain",
                    max_triggers=eff_limit,
                    window_seconds=eff_window,
                    cooldown_seconds=eff_cooldown,
                    now=current,
                    record=True,
                )
                if not check.allowed:
                    return _finish(
                        "suppressed",
                        [],
                        error_message=(
                            f"Chain '{chain.id}' trigger suppressed ({check.reason}); "
                            f"retry after {check.retry_after_seconds:.2f}s."
                        ),
                    )

        # 3. Pre-execution dependency validation (F19.4)
        validation_res: DependencyValidationResult | None = None
        if not skip_validation:
            validation_res = self.validator.validate_chain(chain, check_chain_enabled=True)
            if not validation_res.valid:
                summary = "; ".join(i.message for i in validation_res.issues)
                return _finish(
                    "validation_failed",
                    [],
                    error_message=f"Dependency validation failed: {summary}",
                    validation=validation_res,
                )

        # 4. Runtime loop guard (F19.2)
        guard = _guard or RuntimeLoopGuard(max_depth=chain.max_depth)
        chain_node = f"chain:{chain.id}"
        try:
            guard.enter(chain_node)
        except (CircularReferenceError, MaxDepthExceededError) as exc:
            if not dry_run:
                self.breaker.record_failure(
                    chain.id,
                    str(exc),
                    target_type="chain",
                    is_loop=True,
                    details={"execution_id": execution_id},
                    failure_threshold=chain.breaker_failure_threshold,
                    recovery_timeout_seconds=chain.breaker_recovery_seconds,
                    now=current,
                )
            return _finish(
                "loop_detected",
                [],
                error_message=str(exc),
                validation=validation_res,
                breaker_tripped=not dry_run,
            )

        step_results: list[StepExecutionRecord] = []
        step_index_by_id = {s.step_id: idx for idx, s in enumerate(chain.steps)}
        current_idx = 0
        overall_status = "completed"
        overall_error: str | None = None
        breaker_tripped = False

        try:
            while 0 <= current_idx < len(chain.steps):
                step = chain.steps[current_idx]
                step_start_perf = time.perf_counter()
                step_started_at = (self.now_fn() if now is None else current).isoformat()

                if not step.enabled:
                    step_results.append(
                        StepExecutionRecord(
                            step_id=step.step_id,
                            rule_id=step.rule_id,
                            sub_chain_id=step.sub_chain_id,
                            status="skipped",
                            started_at=step_started_at,
                            finished_at=step_started_at,
                            duration_ms=0.0,
                            next_action="continue",
                        )
                    )
                    current_idx += 1
                    continue

                step_node = f"step:{chain.id}:{step.step_id}"
                try:
                    guard.enter(step_node)
                except (CircularReferenceError, MaxDepthExceededError) as exc:
                    overall_status = "loop_detected"
                    overall_error = str(exc)
                    if not dry_run:
                        self.breaker.record_failure(
                            chain.id,
                            str(exc),
                            target_type="chain",
                            is_loop=True,
                            details={"execution_id": execution_id, "step_id": step.step_id},
                            failure_threshold=chain.breaker_failure_threshold,
                            recovery_timeout_seconds=chain.breaker_recovery_seconds,
                            now=current,
                        )
                        breaker_tripped = True
                    break

                try:
                    if dry_run:
                        step_results.append(
                            StepExecutionRecord(
                                step_id=step.step_id,
                                rule_id=step.rule_id,
                                sub_chain_id=step.sub_chain_id,
                                status="dry_run",
                                started_at=step_started_at,
                                finished_at=step_started_at,
                                duration_ms=0.0,
                                output={"simulated": True},
                                next_action=step.on_success,
                            )
                        )
                        current_idx = self._resolve_next_index(
                            step.on_success, current_idx, step_index_by_id
                        )
                        continue

                    # Per-rule rate limit / cooldown check (F19.3)
                    if step.rule_id:
                        rule_policy = self.rate_limiter.get_policy(step.rule_id)
                        if rule_policy is not None:
                            r_check = self.rate_limiter.check_and_record(
                                step.rule_id,
                                target_type="rule",
                                now=current,
                                record=True,
                            )
                            if not r_check.allowed:
                                step_finished_at = (
                                    self.now_fn() if now is None else current
                                ).isoformat()
                                step_results.append(
                                    StepExecutionRecord(
                                        step_id=step.step_id,
                                        rule_id=step.rule_id,
                                        status="suppressed",
                                        started_at=step_started_at,
                                        finished_at=step_finished_at,
                                        duration_ms=round(
                                            (time.perf_counter() - step_start_perf) * 1000.0, 3
                                        ),
                                        error=(
                                            f"Rule '{step.rule_id}' suppressed ({r_check.reason}); "
                                            f"retry after {r_check.retry_after_seconds:.2f}s."
                                        ),
                                        next_action="continue",
                                    )
                                )
                                current_idx += 1
                                continue

                    # Execute step (sub-chain or rule)
                    step_succeeded = False
                    step_output: dict[str, Any] = {}
                    step_error: str | None = None

                    if step.sub_chain_id:
                        sub_res = await self.execute_chain(
                            step.sub_chain_id,
                            initial_context=context,
                            dry_run=False,
                            skip_validation=skip_validation,
                            now=now,
                            _guard=guard,
                        )
                        if sub_res.status == "loop_detected":
                            overall_status = "loop_detected"
                            overall_error = sub_res.error_message
                            breaker_tripped = True
                            self.breaker.record_failure(
                                chain.id,
                                sub_res.error_message or "Sub-chain loop detected",
                                target_type="chain",
                                is_loop=True,
                                details={"sub_chain_id": step.sub_chain_id},
                                failure_threshold=chain.breaker_failure_threshold,
                                recovery_timeout_seconds=chain.breaker_recovery_seconds,
                                now=current,
                            )
                            break
                        step_succeeded = sub_res.status == "completed"
                        step_output = {
                            "sub_chain_id": step.sub_chain_id,
                            "status": sub_res.status,
                            "final_context": sub_res.final_context,
                        }
                        if step_succeeded:
                            # Propagate updated keys from sub-chain back to parent context
                            for k, v in sub_res.final_context.items():
                                if k != "step_outputs":
                                    context[k] = v
                        else:
                            step_error = sub_res.error_message or f"Sub-chain '{step.sub_chain_id}' failed."
                    else:
                        rule = self.storage.get_automation_rule(step.rule_id or "")
                        if rule is None:
                            step_succeeded = False
                            step_error = f"Automation rule '{step.rule_id}' not found."
                        else:
                            try:
                                raw_out = self.step_executor(rule, context)
                                if inspect.isawaitable(raw_out):
                                    raw_out = await raw_out
                                step_output = raw_out if isinstance(raw_out, dict) else {"result": raw_out}
                                step_succeeded = bool(step_output.get("success", True))
                                if not step_succeeded:
                                    step_error = str(
                                        step_output.get("error") or f"Rule '{step.rule_id}' returned success=False"
                                    )
                            except Exception as exc:
                                step_succeeded = False
                                step_error = str(exc)

                    step_finished_at = (self.now_fn() if now is None else current).isoformat()
                    step_duration_ms = round((time.perf_counter() - step_start_perf) * 1000.0, 3)

                    if step_succeeded:
                        context["step_outputs"][step.step_id] = step_output
                        for src_key, dst_key in step.context_mapping.items():
                            if src_key in step_output:
                                context[dst_key] = step_output[src_key]
                        if isinstance(step_output.get("context_updates"), dict):
                            context.update(step_output["context_updates"])

                        step_results.append(
                            StepExecutionRecord(
                                step_id=step.step_id,
                                rule_id=step.rule_id,
                                sub_chain_id=step.sub_chain_id,
                                status="success",
                                started_at=step_started_at,
                                finished_at=step_finished_at,
                                duration_ms=step_duration_ms,
                                output=step_output,
                                next_action=step.on_success,
                            )
                        )
                        if step.on_success == "stop":
                            break
                        current_idx = self._resolve_next_index(
                            step.on_success, current_idx, step_index_by_id
                        )
                    else:
                        effective_stop = (
                            step.stop_on_error
                            if step.stop_on_error is not None
                            else chain.stop_on_error
                        )
                        # Determine branching on failure
                        if step.on_failure in step_index_by_id and step.stop_on_error is not True:
                            next_action = step.on_failure
                            should_halt = False
                        elif step.on_failure == "continue" and step.stop_on_error is not True:
                            next_action = "continue"
                            should_halt = False
                        elif step.on_failure == "stop" or effective_stop:
                            next_action = "stop"
                            should_halt = True
                        else:
                            next_action = "continue"
                            should_halt = False

                        step_results.append(
                            StepExecutionRecord(
                                step_id=step.step_id,
                                rule_id=step.rule_id,
                                sub_chain_id=step.sub_chain_id,
                                status="failed",
                                started_at=step_started_at,
                                finished_at=step_finished_at,
                                duration_ms=step_duration_ms,
                                output=step_output,
                                error=step_error,
                                next_action=next_action,
                            )
                        )
                        if should_halt:
                            overall_status = "failed"
                            overall_error = step_error
                            break
                        current_idx = self._resolve_next_index(
                            next_action, current_idx, step_index_by_id
                        )
                finally:
                    guard.exit(step_node)
        finally:
            guard.exit(chain_node)

        if dry_run:
            return _finish(
                "dry_run",
                step_results,
                validation=validation_res,
                persist=False,
            )

        # Update circuit breaker state on completion / failure (F19.5)
        if overall_status == "completed":
            self.breaker.record_success(
                chain.id,
                target_type="chain",
                failure_threshold=chain.breaker_failure_threshold,
                recovery_timeout_seconds=chain.breaker_recovery_seconds,
                now=current,
            )
        elif overall_status == "failed":
            b_state, alert = self.breaker.record_failure(
                chain.id,
                overall_error or "Rule chain step failed",
                target_type="chain",
                is_loop=False,
                details={"execution_id": execution_id},
                failure_threshold=chain.breaker_failure_threshold,
                recovery_timeout_seconds=chain.breaker_recovery_seconds,
                now=current,
            )
            if b_state.state == "open" and alert is not None:
                breaker_tripped = True

        return _finish(
            overall_status,
            step_results,
            error_message=overall_error,
            validation=validation_res,
            breaker_tripped=breaker_tripped,
        )

    @staticmethod
    def _resolve_next_index(
        directive: str,
        current_idx: int,
        step_index_by_id: dict[str, int],
    ) -> int:
        if directive == "stop":
            return -1
        if directive == "continue":
            return current_idx + 1
        if directive in step_index_by_id:
            return step_index_by_id[directive]
        return current_idx + 1

    # --- Execution History ---

    def list_executions(
        self,
        *,
        chain_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ChainExecutionResult]:
        return self.storage.list_executions(
            chain_id=chain_id, status=status, limit=limit, offset=offset
        )

    def get_execution(self, execution_id: str) -> ChainExecutionResult | None:
        return self.storage.get_execution(execution_id)
