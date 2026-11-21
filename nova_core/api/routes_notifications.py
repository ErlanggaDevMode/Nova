"""REST endpoints for Advanced Notification Rules & History (Phase 16: F16.1–F16.5).

Mount in `nova_core/main.py` with:
    from nova_core.api.routes_notifications import router as notifications_router
    app.include_router(notifications_router, dependencies=[Depends(get_current_user)])
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nova_core.automation.notification_rules import (
    AlertLevel,
    NotificationPayload,
    NotificationRule,
    NotificationRuleEngine,
    NotificationStorage,
    PackageWhitelistConfig,
    SilentScheduleConfig,
    SilentScheduleWindow,
)

router = APIRouter(prefix="/api/v1/notifications", tags=["notifications"])


def get_notification_engine(request: Request) -> NotificationRuleEngine:
    """Resolves or lazily initializes a NotificationRuleEngine bound to app.state.store."""
    engine = getattr(request.app.state, "notification_engine", None)
    if engine is not None:
        return engine

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Notification storage backend is not configured on app.state.",
        )

    storage = NotificationStorage(store)
    engine = NotificationRuleEngine(storage=storage)
    request.app.state.notification_engine = engine
    return engine


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class NotificationIngestRequest(BaseModel):
    notification_id: Optional[str] = Field(default=None, max_length=128)
    source_device_id: str = Field(default="unknown", max_length=128)
    package_name: str = Field(..., min_length=1, max_length=256)
    title: str = Field(default="")
    body: str = Field(default="")
    sender: Optional[str] = Field(default=None)
    received_at: Optional[datetime] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    record_history: bool = Field(default=True)


class NotificationRuleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    enabled: bool = Field(default=True)
    package_patterns: list[str] = Field(default_factory=list)
    title_regex: Optional[str] = Field(default=None)
    body_regex: Optional[str] = Field(default=None)
    sender_regex: Optional[str] = Field(default=None)
    keywords: list[str] = Field(default_factory=list)
    regex_case_sensitive: bool = Field(default=False)
    alert_level: AlertLevel = Field(default=AlertLevel.NORMAL)
    priority_score: int = Field(default=50, ge=0, le=1000)
    bypass_silent: bool = Field(default=False)
    stop_on_match: bool = Field(default=False)
    description: Optional[str] = Field(default=None, max_length=512)


class SilentScheduleUpdateRequest(BaseModel):
    enabled: bool = Field(default=True)
    start_time: str = Field(default="22:00")
    end_time: str = Field(default="07:00")
    days_of_week: list[str | int] = Field(
        default_factory=lambda: ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    )
    timezone: str = Field(default="UTC")
    allow_critical_bypass: bool = Field(default=True)
    min_bypass_score: Optional[int] = Field(default=None, ge=0, le=1000)
    extra_windows: list[SilentScheduleWindow] = Field(default_factory=list)


class WhitelistUpdateRequest(BaseModel):
    mode: Literal["allow_all", "strict"] = Field(default="allow_all")
    allowed_packages: list[str] = Field(default_factory=list)
    blocked_packages: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Endpoints (F16.5)
# ---------------------------------------------------------------------------


@router.post("/ingest", status_code=status.HTTP_200_OK)
async def ingest_notification(
    payload: NotificationIngestRequest,
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Evaluates an incoming notification against whitelist, rules, classifier, and DND schedule."""
    notif_payload = NotificationPayload(
        notification_id=payload.notification_id,
        source_device_id=payload.source_device_id,
        package_name=payload.package_name,
        title=payload.title,
        body=payload.body,
        sender=payload.sender,
        received_at=payload.received_at,
        metadata=payload.metadata,
    )
    result = engine.evaluate(notif_payload, record_history=payload.record_history)
    return {
        "success": True,
        "result": result.model_dump(mode="json"),
    }


@router.get("/history")
async def get_notification_history(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    package_name: Optional[str] = Query(default=None),
    alert_level: Optional[AlertLevel] = Query(default=None),
    silenced: Optional[bool] = Query(default=None),
    source_device_id: Optional[str] = Query(default=None),
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Returns incoming notification history and summary statistics."""
    if engine.storage is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Notification history storage is not initialized.",
        )

    items = engine.storage.list_history(
        limit=limit,
        offset=offset,
        package_name=package_name,
        alert_level=alert_level.value if alert_level else None,
        silenced=silenced,
        source_device_id=source_device_id,
    )
    stats = engine.storage.get_history_stats()
    return {
        "success": True,
        "count": len(items),
        "items": items,
        "stats": stats,
    }


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_notification_rule(
    payload: NotificationRuleCreateRequest,
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Creates a new notification classification rule with validated regex patterns."""
    try:
        rule = NotificationRule(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid notification rule: {exc}",
        ) from exc

    created = engine.add_rule(rule)
    return {
        "success": True,
        "rule": created.model_dump(mode="json"),
    }


@router.get("/rules")
async def list_notification_rules(
    enabled_only: bool = Query(default=False),
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Lists notification classification rules ordered by priority."""
    rules = engine.list_rules(enabled_only=enabled_only)
    return {
        "success": True,
        "count": len(rules),
        "rules": [r.model_dump(mode="json") for r in rules],
    }


@router.delete("/rules/{rule_id}")
async def delete_notification_rule(
    rule_id: str,
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Deletes a notification classification rule by ID."""
    deleted = engine.delete_rule(rule_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Notification rule '{rule_id}' not found.",
        )
    return {
        "success": True,
        "rule_id": rule_id,
        "message": f"Notification rule '{rule_id}' deleted successfully.",
    }


@router.get("/silent-schedule")
async def get_silent_schedule(
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Returns the current silent notification / Do Not Disturb schedule and live status."""
    config = engine.get_silent_schedule()
    live_check = engine.silent_evaluator.evaluate(
        config,
        alert_level=AlertLevel.NORMAL,
        priority_score=50,
    )
    return {
        "success": True,
        "schedule": config.model_dump(mode="json"),
        "currently_active": live_check.in_silent_window,
        "local_time": live_check.local_time,
    }


@router.put("/silent-schedule")
async def update_silent_schedule(
    payload: SilentScheduleUpdateRequest,
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Updates and persists the silent notification / Do Not Disturb schedule."""
    try:
        config = SilentScheduleConfig(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid silent schedule configuration: {exc}",
        ) from exc

    saved = engine.set_silent_schedule(config)
    return {
        "success": True,
        "schedule": saved.model_dump(mode="json"),
    }


@router.get("/whitelist")
async def get_whitelist(
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Returns the current application package whitelist policy."""
    config = engine.get_whitelist_config()
    return {
        "success": True,
        "whitelist": config.model_dump(mode="json"),
    }


@router.put("/whitelist")
async def update_whitelist(
    payload: WhitelistUpdateRequest,
    engine: NotificationRuleEngine = Depends(get_notification_engine),
):
    """Updates and persists the application package whitelist policy."""
    config = PackageWhitelistConfig(**payload.model_dump())
    saved = engine.set_whitelist_config(config)
    return {
        "success": True,
        "whitelist": saved.model_dump(mode="json"),
    }
