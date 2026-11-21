"""REST endpoints for Time & Cron Scheduler Trigger (Phase 15: F15.5)."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nova_core.automation.cron_scheduler import (
    CronParseError,
    CronScheduler,
    TimezoneError,
    get_timezone_offset_info,
    parse_cron,
    validate_timezone,
)

router = APIRouter(prefix="/api/v1/scheduler", tags=["scheduler"])


def _get_scheduler(request: Request) -> CronScheduler:
    scheduler = getattr(request.app.state, "cron_scheduler", None)
    if scheduler is None or scheduler.store is not request.app.state.store:
        scheduler = CronScheduler(request.app.state.store)
        request.app.state.cron_scheduler = scheduler
    return scheduler


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid ISO-8601 datetime '{value}': {exc}",
        ) from exc


class ScheduledRuleCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    cron_expression: str = Field(..., min_length=1)
    timezone: str = Field(default="UTC")
    action_template: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = Field(default=True)
    prevent_overlap: bool = Field(default=True)
    lock_ttl_seconds: int = Field(default=300, ge=1, le=86400)


class ScheduledRuleUpdate(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    cron_expression: Optional[str] = Field(default=None, min_length=1)
    timezone: Optional[str] = Field(default=None)
    action_template: Optional[dict[str, Any]] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)
    prevent_overlap: Optional[bool] = Field(default=None)
    lock_ttl_seconds: Optional[int] = Field(default=None, ge=1, le=86400)


class SchedulerEvaluateRequest(BaseModel):
    cron_expression: Optional[str] = Field(
        default=None,
        description="When provided, evaluates this cron expression without touching stored rules.",
    )
    timezone: str = Field(default="UTC")
    at: Optional[str] = Field(
        default=None,
        description="Optional ISO-8601 evaluation timestamp (defaults to current UTC time).",
    )
    count: int = Field(
        default=3,
        ge=1,
        le=20,
        description="Number of upcoming run timestamps to compute in expression preview mode.",
    )
    dry_run: bool = Field(
        default=False,
        description="When true in stored-rules mode, checks due rules without executing or logging.",
    )
    rule_id: Optional[str] = Field(
        default=None,
        description="Optional specific scheduled rule ID to evaluate.",
    )


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_scheduled_rule(payload: ScheduledRuleCreate, request: Request):
    """Creates a new cron-scheduled rule with validated cron expression and IANA timezone."""
    try:
        cron = parse_cron(payload.cron_expression)
        validate_timezone(payload.timezone)
        next_dt = cron.next_run_at(tz=payload.timezone)
    except (CronParseError, TimezoneError) as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    store = request.app.state.store
    rule = store.create_scheduled_rule(
        name=payload.name,
        cron_expression=payload.cron_expression.strip(),
        timezone_name=payload.timezone.strip(),
        action_template=payload.action_template,
        enabled=payload.enabled,
        prevent_overlap=payload.prevent_overlap,
        lock_ttl_seconds=payload.lock_ttl_seconds,
        next_run_at=next_dt.astimezone(timezone.utc).isoformat(),
    )
    return {
        "success": True,
        "rule": rule,
    }


@router.get("/rules")
async def list_scheduled_rules(
    request: Request,
    enabled: Optional[bool] = Query(default=None),
):
    """Lists all scheduled rules, optionally filtered by enabled state."""
    store = request.app.state.store
    rules = store.list_scheduled_rules(enabled=enabled)
    return {
        "success": True,
        "count": len(rules),
        "rules": rules,
    }


@router.get("/rules/{rule_id}")
async def get_scheduled_rule(rule_id: str, request: Request):
    """Returns a single scheduled rule along with its lock status."""
    store = request.app.state.store
    rule = store.get_scheduled_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled rule not found.")
    scheduler = _get_scheduler(request)
    return {
        "success": True,
        "rule": rule,
        "lock": scheduler.lock_manager.get_lock_info(rule_id),
    }


@router.patch("/rules/{rule_id}")
async def update_scheduled_rule(rule_id: str, payload: ScheduledRuleUpdate, request: Request):
    """Updates an existing scheduled rule and recalculates `next_run_at` when schedule changes."""
    store = request.app.state.store
    existing = store.get_scheduled_rule(rule_id)
    if not existing:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled rule not found.")

    target_cron = (
        payload.cron_expression.strip()
        if payload.cron_expression is not None
        else existing["cron_expression"]
    )
    target_tz = (
        payload.timezone.strip()
        if payload.timezone is not None
        else existing["timezone"]
    )

    next_run_iso: str | None = None
    if payload.cron_expression is not None or payload.timezone is not None:
        try:
            cron = parse_cron(target_cron)
            validate_timezone(target_tz)
            next_run_iso = cron.next_run_at(tz=target_tz).astimezone(timezone.utc).isoformat()
        except (CronParseError, TimezoneError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    updated = store.update_scheduled_rule(
        rule_id=rule_id,
        name=payload.name,
        cron_expression=target_cron if payload.cron_expression is not None else None,
        timezone_name=target_tz if payload.timezone is not None else None,
        action_template=payload.action_template,
        enabled=payload.enabled,
        prevent_overlap=payload.prevent_overlap,
        lock_ttl_seconds=payload.lock_ttl_seconds,
        next_run_at=next_run_iso,
    )
    return {
        "success": True,
        "rule": updated,
    }


@router.delete("/rules/{rule_id}")
async def delete_scheduled_rule(rule_id: str, request: Request):
    """Deletes a scheduled rule by ID."""
    store = request.app.state.store
    deleted = store.delete_scheduled_rule(rule_id)
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Scheduled rule not found.")
    return {
        "success": True,
        "message": f"Scheduled rule '{rule_id}' deleted successfully.",
    }


@router.get("/executions")
async def list_scheduler_executions(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    rule_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
):
    """Lists scheduler execution history logs and summary stats."""
    store = request.app.state.store
    items = store.get_scheduler_executions(
        limit=limit,
        offset=offset,
        rule_id=rule_id,
        status=status_filter,
    )
    stats = store.get_scheduler_execution_stats(rule_id=rule_id)
    return {
        "success": True,
        "count": len(items),
        "items": items,
        "stats": stats,
    }


@router.post("/evaluate")
async def evaluate_scheduler(payload: SchedulerEvaluateRequest, request: Request):
    """Evaluates either an ad-hoc cron expression (preview) or all due stored scheduled rules."""
    eval_dt = _parse_iso_datetime(payload.at)

    if payload.cron_expression is not None:
        try:
            cron = parse_cron(payload.cron_expression)
            validate_timezone(payload.timezone)
        except (CronParseError, TimezoneError) as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

        ref_dt = eval_dt if eval_dt is not None else datetime.now(timezone.utc)
        tz_info = get_timezone_offset_info(ref_dt, payload.timezone)
        matches = cron.matches(ref_dt, tz=payload.timezone)
        upcoming = cron.next_n_runs(payload.count, after=ref_dt, tz=payload.timezone)

        return {
            "success": True,
            "mode": "expression",
            "cron_expression": payload.cron_expression.strip(),
            "timezone": payload.timezone.strip(),
            "matches": matches,
            "timezone_info": tz_info,
            "next_run_at": upcoming[0].isoformat() if upcoming else None,
            "next_run_at_utc": upcoming[0].astimezone(timezone.utc).isoformat() if upcoming else None,
            "upcoming_runs": [dt.isoformat() for dt in upcoming],
        }

    scheduler = _get_scheduler(request)
    summary = await scheduler.evaluate_due_rules(
        now=eval_dt,
        dry_run=payload.dry_run,
        rule_id=payload.rule_id,
    )
    return {
        "success": True,
        "mode": "rules",
        **summary,
    }
