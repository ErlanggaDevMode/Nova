"""REST endpoints for Rule Chain & Loop Protections (Phase 19: F19.1–F19.5).

Mount in `nova_core/main.py` with:
    from nova_core.api.routes_rule_chains import router as rule_chains_router
    app.include_router(rule_chains_router, dependencies=[Depends(get_current_user)])
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nova_core.automation.rule_chaining import (
    BreakerState,
    ChainStep,
    CircularReferenceError,
    LinkCondition,
    RuleChain,
    RuleChainService,
    TargetType,
)

router = APIRouter(prefix="/api/v1/rule-chains", tags=["rule_chains"])


def get_rule_chain_service(request: Request) -> RuleChainService:
    """Resolves or lazily initializes a RuleChainService bound to app.state.store."""
    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Rule chain storage backend is not configured on app.state.",
        )

    service = getattr(request.app.state, "rule_chain_service", None)
    if service is not None and service.store is store:
        return service

    step_executor = getattr(request.app.state, "rule_chain_step_executor", None)
    service = RuleChainService(store=store, step_executor=step_executor)
    request.app.state.rule_chain_service = service
    return service


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class RuleChainCreateRequest(BaseModel):
    id: Optional[str] = Field(default=None, min_length=1, max_length=128)
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


class RuleChainUpdateRequest(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=200)
    description: Optional[str] = Field(default=None, max_length=1000)
    enabled: Optional[bool] = Field(default=None)
    steps: Optional[list[ChainStep]] = Field(default=None)
    max_depth: Optional[int] = Field(default=None, ge=1, le=100)
    stop_on_error: Optional[bool] = Field(default=None)
    trigger_limit: Optional[int] = Field(default=None, ge=1)
    trigger_window_seconds: Optional[int] = Field(default=None, ge=1)
    cooldown_seconds: Optional[int] = Field(default=None, ge=0)
    breaker_failure_threshold: Optional[int] = Field(default=None, ge=1)
    breaker_recovery_seconds: Optional[int] = Field(default=None, ge=1)


class RuleLinkCreateRequest(BaseModel):
    source_rule_id: str = Field(..., min_length=1, max_length=128)
    target_rule_id: str = Field(..., min_length=1, max_length=128)
    condition: LinkCondition = Field(default="on_success")


class CycleCheckRequest(BaseModel):
    chain: Optional[RuleChainCreateRequest] = Field(default=None)


class ChainExecuteRequest(BaseModel):
    initial_context: dict[str, Any] = Field(default_factory=dict)
    dry_run: bool = Field(default=False)
    skip_validation: bool = Field(default=False)
    at: Optional[datetime] = Field(default=None)


class TriggerLimitConfigRequest(BaseModel):
    target_type: TargetType = Field(default="rule")
    max_triggers: int = Field(default=10, ge=1)
    window_seconds: int = Field(default=60, ge=1)
    cooldown_seconds: int = Field(default=0, ge=0)


class TriggerLimitCheckRequest(BaseModel):
    target_type: TargetType = Field(default="rule")
    record: bool = Field(default=False)
    at: Optional[datetime] = Field(default=None)


class AlertAcknowledgeRequest(BaseModel):
    acknowledged_by: str = Field(default="operator", min_length=1, max_length=128)


# ---------------------------------------------------------------------------
# Static-prefix Endpoints (declared before /{chain_id} to avoid route shadowing)
# ---------------------------------------------------------------------------


@router.post("/links", status_code=status.HTTP_201_CREATED)
async def create_rule_link(
    payload: RuleLinkCreateRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Creates a directed rule-to-rule link after static DFS cycle validation (F19.2)."""
    try:
        link = service.create_rule_link(
            source_rule_id=payload.source_rule_id,
            target_rule_id=payload.target_rule_id,
            condition=payload.condition,
        )
    except CircularReferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "cycle_path": exc.cycle_path,
            },
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Failed to create rule link: {exc}",
        ) from exc

    return {
        "success": True,
        "link": link.model_dump(mode="json"),
    }


@router.get("/links")
async def list_rule_links(
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Lists all configured rule-to-rule links."""
    links = service.list_rule_links()
    return {
        "success": True,
        "count": len(links),
        "links": [lnk.model_dump(mode="json") for lnk in links],
    }


@router.delete("/links/{link_id}")
async def delete_rule_link(
    link_id: str,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Deletes a rule-to-rule link by ID."""
    deleted = service.delete_rule_link(link_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule link '{link_id}' not found.",
        )
    return {
        "success": True,
        "link_id": link_id,
        "message": f"Rule link '{link_id}' deleted successfully.",
    }


@router.post("/check-cycles")
async def check_cycles(
    payload: CycleCheckRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Runs static DFS cycle detection on stored graphs or a candidate rule chain (F19.2)."""
    candidate: RuleChain | None = None
    if payload.chain is not None:
        data = payload.chain.model_dump(exclude_none=True)
        candidate = RuleChain(**data)
    report = service.check_cycles(candidate_chain=candidate)
    return {
        "success": True,
        **report,
    }


@router.get("/executions")
async def list_chain_executions(
    chain_id: Optional[str] = Query(default=None),
    status_filter: Optional[str] = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Lists persisted rule chain execution history records (F19.1)."""
    items = service.list_executions(
        chain_id=chain_id,
        status=status_filter,
        limit=limit,
        offset=offset,
    )
    return {
        "success": True,
        "count": len(items),
        "items": [item.model_dump(mode="json") for item in items],
    }


@router.get("/executions/{execution_id}")
async def get_chain_execution(
    execution_id: str,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Fetches a single rule chain execution record with step-level history."""
    item = service.get_execution(execution_id)
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Chain execution '{execution_id}' not found.",
        )
    return {
        "success": True,
        "execution": item.model_dump(mode="json"),
    }


@router.put("/limits/{target_id}")
async def configure_trigger_limits(
    target_id: str,
    payload: TriggerLimitConfigRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Configures sliding-window trigger rate limits and cooldown schedule for a rule or chain (F19.3)."""
    policy = service.rate_limiter.set_policy(
        target_id,
        target_type=payload.target_type,
        max_triggers=payload.max_triggers,
        window_seconds=payload.window_seconds,
        cooldown_seconds=payload.cooldown_seconds,
    )
    return {
        "success": True,
        "policy": policy.model_dump(mode="json"),
    }


@router.get("/limits/{target_id}")
async def get_trigger_limits(
    target_id: str,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Fetches trigger rate limit and cooldown policy for a rule or chain (F19.3)."""
    policy = service.rate_limiter.get_policy(target_id)
    if policy is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Trigger limit policy for '{target_id}' not found.",
        )
    status_check = service.rate_limiter.check_and_record(
        target_id, target_type=policy.target_type, record=False
    )
    return {
        "success": True,
        "policy": policy.model_dump(mode="json"),
        "status": status_check.model_dump(mode="json"),
    }


@router.post("/limits/{target_id}/check")
async def check_trigger_limits(
    target_id: str,
    payload: TriggerLimitCheckRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Checks (and optionally records) whether a trigger is permitted under limits & cooldown (F19.3)."""
    result = service.rate_limiter.check_and_record(
        target_id,
        target_type=payload.target_type,
        now=payload.at,
        record=payload.record,
    )
    return {
        "success": True,
        "result": result.model_dump(mode="json"),
    }


@router.get("/breakers")
async def list_loop_breakers(
    state: Optional[BreakerState] = Query(default=None),
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Lists circuit breaker states across chains and rules (F19.5)."""
    breakers = service.breaker.list_states(state=state)
    return {
        "success": True,
        "count": len(breakers),
        "breakers": [b.model_dump(mode="json") for b in breakers],
    }


@router.get("/breakers/{target_id}")
async def get_loop_breaker(
    target_id: str,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Inspects the circuit breaker state and recent alerts for a target chain or rule (F19.5)."""
    breaker_state = service.breaker.get_state(target_id)
    alerts = service.breaker.list_alerts(target_id=target_id, limit=20)
    return {
        "success": True,
        "breaker": breaker_state.model_dump(mode="json"),
        "alerts": [a.model_dump(mode="json") for a in alerts],
    }


@router.post("/breakers/{target_id}/reset")
async def reset_loop_breaker(
    target_id: str,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Resets a tripped circuit breaker back to 'closed' and resolves open alerts (F19.5)."""
    breaker_state = service.breaker.reset(target_id)
    return {
        "success": True,
        "breaker": breaker_state.model_dump(mode="json"),
    }


@router.get("/alerts")
async def list_loop_breaker_alerts(
    target_id: Optional[str] = Query(default=None),
    acknowledged: Optional[bool] = Query(default=None),
    resolved: Optional[bool] = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Lists persisted loop-breaker alert records (F19.5)."""
    alerts = service.breaker.list_alerts(
        target_id=target_id,
        acknowledged=acknowledged,
        resolved=resolved,
        limit=limit,
    )
    return {
        "success": True,
        "count": len(alerts),
        "alerts": [a.model_dump(mode="json") for a in alerts],
    }


@router.post("/alerts/{alert_id}/acknowledge")
async def acknowledge_loop_breaker_alert(
    alert_id: str,
    payload: AlertAcknowledgeRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Acknowledges a persisted loop-breaker alert (F19.5)."""
    alert = service.breaker.acknowledge_alert(
        alert_id, acknowledged_by=payload.acknowledged_by
    )
    if alert is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Loop breaker alert '{alert_id}' not found.",
        )
    return {
        "success": True,
        "alert": alert.model_dump(mode="json"),
    }


# ---------------------------------------------------------------------------
# Rule Chain CRUD, Validation & Execution Endpoints (F19.1, F19.2, F19.4)
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_rule_chain(
    payload: RuleChainCreateRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Creates a new sequential rule chain after static cycle validation."""
    data = payload.model_dump(exclude_none=True)
    chain = RuleChain(**data)
    try:
        created = service.create_chain(chain)
    except CircularReferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "cycle_path": exc.cycle_path,
            },
        ) from exc

    return {
        "success": True,
        "chain": created.model_dump(mode="json"),
    }


@router.get("")
async def list_rule_chains(
    enabled_only: bool = Query(default=False),
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Lists all rule chains."""
    chains = service.list_chains(enabled_only=enabled_only)
    return {
        "success": True,
        "count": len(chains),
        "chains": [c.model_dump(mode="json") for c in chains],
    }


@router.get("/{chain_id}")
async def get_rule_chain(
    chain_id: str,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Returns a rule chain along with its circuit breaker state."""
    chain = service.get_chain(chain_id)
    if chain is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule chain '{chain_id}' not found.",
        )
    breaker = service.breaker.get_state(chain_id)
    return {
        "success": True,
        "chain": chain.model_dump(mode="json"),
        "breaker": breaker.model_dump(mode="json"),
    }


@router.patch("/{chain_id}")
async def update_rule_chain(
    chain_id: str,
    payload: RuleChainUpdateRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Updates an existing rule chain after static cycle validation."""
    existing = service.get_chain(chain_id)
    if existing is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule chain '{chain_id}' not found.",
        )

    updates = payload.model_dump(exclude_unset=True)
    merged = existing.model_copy(update=updates)
    try:
        updated = service.update_chain(merged)
    except CircularReferenceError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail={
                "message": str(exc),
                "cycle_path": exc.cycle_path,
            },
        ) from exc

    return {
        "success": True,
        "chain": updated.model_dump(mode="json") if updated else None,
    }


@router.delete("/{chain_id}")
async def delete_rule_chain(
    chain_id: str,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Deletes a rule chain by ID."""
    deleted = service.delete_chain(chain_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Rule chain '{chain_id}' not found.",
        )
    return {
        "success": True,
        "chain_id": chain_id,
        "message": f"Rule chain '{chain_id}' deleted successfully.",
    }


@router.post("/{chain_id}/validate")
async def validate_rule_chain(
    chain_id: str,
    check_chain_enabled: bool = Query(default=True),
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Runs pre-execution dependency validation for a rule chain (F19.4)."""
    try:
        report = service.validate_chain(chain_id, check_chain_enabled=check_chain_enabled)
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc
    return {
        "success": True,
        "validation": report.model_dump(mode="json"),
    }


@router.post("/{chain_id}/execute")
async def execute_rule_chain(
    chain_id: str,
    payload: ChainExecuteRequest,
    service: RuleChainService = Depends(get_rule_chain_service),
):
    """Executes or dry-runs a rule chain sequentially with loop & rate protections (F19.1–F19.5)."""
    try:
        result = await service.execute_chain(
            chain_id,
            initial_context=payload.initial_context,
            dry_run=payload.dry_run,
            skip_validation=payload.skip_validation,
            now=payload.at,
        )
    except KeyError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    return {
        "success": result.status in ("completed", "dry_run"),
        "execution": result.model_dump(mode="json"),
    }
