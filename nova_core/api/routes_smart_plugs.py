"""REST endpoints for Smart Plug HTTP Integrations — Shelly & Kasa (Phase 20: F20.1–F20.5).

Mount in `nova_core/main.py` with:
    from nova_core.api.routes_smart_plugs import router as smart_plugs_router
    app.include_router(smart_plugs_router, dependencies=[Depends(get_current_user)])
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nova_core.integrations.smart_home.smart_plugs import (
    SmartPlugAction,
    SmartPlugAuthError,
    SmartPlugDevice,
    SmartPlugError,
    SmartPlugProtocolError,
    SmartPlugProvider,
    SmartPlugRuleParseError,
    SmartPlugService,
    SmartPlugStorage,
    SmartPlugTimeoutError,
)

router = APIRouter(prefix="/api/v1/smart-plugs", tags=["smart_plugs"])


def get_smart_plug_service(request: Request) -> SmartPlugService:
    """Resolves or lazily initializes a SmartPlugService bound to app.state.store."""
    service = getattr(request.app.state, "smart_plug_service", None)
    if service is not None:
        return service

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Smart plug storage backend is not configured on app.state.",
        )

    shelly_transport = getattr(request.app.state, "shelly_http_transport", None)
    kasa_transport = getattr(request.app.state, "kasa_tcp_transport", None)
    shelly_probe = getattr(request.app.state, "smart_plug_shelly_probe", None)
    kasa_probe = getattr(request.app.state, "smart_plug_kasa_probe", None)

    storage = SmartPlugStorage(store)
    service = SmartPlugService(
        storage=storage,
        shelly_transport=shelly_transport,
        kasa_transport=kasa_transport,
        shelly_probe=shelly_probe,
        kasa_probe=kasa_probe,
    )
    request.app.state.smart_plug_service = service
    return service


def _translate_plug_error(exc: SmartPlugError) -> HTTPException:
    if isinstance(exc, SmartPlugTimeoutError):
        return HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc))
    if isinstance(exc, SmartPlugAuthError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    if isinstance(exc, SmartPlugProtocolError):
        return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    return HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class SmartPlugRegisterRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    provider: SmartPlugProvider
    host: str = Field(..., min_length=1, max_length=255)
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    protocol_version: str = Field(default="gen1", min_length=1, max_length=32)
    channel: int = Field(default=0, ge=0, le=32)
    category: str = Field(default="general", min_length=1, max_length=64)
    room: Optional[str] = Field(default=None, max_length=64)
    auth_username: Optional[str] = Field(default=None, max_length=128)
    auth_password: Optional[str] = Field(default=None, max_length=256)
    auth_token: Optional[str] = Field(default=None, max_length=512)
    use_https: bool = Field(default=False)
    timeout_seconds: float = Field(default=5.0, gt=0.0, le=60.0)
    enabled: bool = Field(default=True)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SmartPlugPowerRequest(BaseModel):
    plug_id: Optional[str] = Field(default=None, max_length=128)
    action: SmartPlugAction
    channel: Optional[int] = Field(default=None, ge=0, le=32)


class SmartPlugSyncRequest(BaseModel):
    plug_id: Optional[str] = Field(default=None, max_length=128)
    provider: Optional[SmartPlugProvider] = Field(default=None)
    category: Optional[str] = Field(default=None, max_length=64)
    room: Optional[str] = Field(default=None, max_length=64)
    raise_on_error: bool = Field(default=False)


class SmartPlugDiscoverRequest(BaseModel):
    cidr: Optional[str] = Field(default=None, max_length=64)
    hosts: Optional[list[str]] = Field(default=None)
    providers: Optional[list[SmartPlugProvider]] = Field(default=None)
    timeout_seconds: float = Field(default=1.5, ge=0.05, le=15.0)
    max_concurrency: int = Field(default=16, ge=1, le=64)
    auto_register: bool = Field(default=False)
    default_category: str = Field(default="general", min_length=1, max_length=64)
    default_room: Optional[str] = Field(default=None, max_length=64)


class SmartPlugRuleParseRequest(BaseModel):
    name: Optional[str] = Field(default=None, max_length=128)
    expression: Optional[str] = Field(default=None, max_length=1024)
    rule: Optional[dict[str, Any]] = Field(default=None)
    enabled: bool = Field(default=True)
    save: bool = Field(default=False)


# ---------------------------------------------------------------------------
# Endpoints — Collection & Static Routes (registered before /{plug_id})
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_201_CREATED)
async def register_smart_plug(
    payload: SmartPlugRegisterRequest,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Registers a Shelly or TP-Link Kasa smart plug."""
    try:
        device = SmartPlugDevice(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid smart plug configuration: {exc}",
        ) from exc

    created = service.register_plug(device)
    return {
        "success": True,
        "plug": created.to_public_dict(),
    }


@router.get("")
async def list_smart_plugs(
    provider: Optional[SmartPlugProvider] = Query(default=None),
    category: Optional[str] = Query(default=None),
    room: Optional[str] = Query(default=None),
    enabled_only: bool = Query(default=False),
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Lists registered smart plugs with optional provider/category/room filters."""
    plugs = service.list_plugs(
        provider=provider,
        category=category,
        room=room,
        enabled_only=enabled_only,
    )
    return {
        "success": True,
        "count": len(plugs),
        "plugs": [p.to_public_dict() for p in plugs],
    }


@router.post("/discover")
async def discover_smart_plugs(
    payload: SmartPlugDiscoverRequest,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Triggers a bounded private-network discovery scan for Shelly and Kasa smart plugs."""
    try:
        result = service.discover_plugs(
            cidr=payload.cidr,
            hosts=payload.hosts,
            providers=payload.providers,
            timeout_seconds=payload.timeout_seconds,
            max_concurrency=payload.max_concurrency,
            auto_register=payload.auto_register,
            default_category=payload.default_category,
            default_room=payload.default_room,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid discovery request: {exc}",
        ) from exc

    return {
        "success": True,
        "result": result.model_dump(mode="json"),
    }


@router.post("/power")
async def control_smart_plug_power(
    payload: SmartPlugPowerRequest,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Sends an on/off/toggle control command to a registered smart plug."""
    if not payload.plug_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="'plug_id' is required in request body for /power endpoint.",
        )
    try:
        result = service.send_power_command(
            payload.plug_id,
            payload.action,
            channel=payload.channel,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SmartPlugError as exc:
        raise _translate_plug_error(exc) from exc

    return {
        "success": True,
        "result": result.model_dump(mode="json"),
    }


@router.post("/sync")
async def sync_smart_plugs(
    payload: Optional[SmartPlugSyncRequest] = None,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Syncs live status & power/wattage for one or all matching smart plugs into SQLite."""
    req = payload or SmartPlugSyncRequest()
    if req.plug_id:
        try:
            snap = service.sync_plug(req.plug_id, raise_on_error=req.raise_on_error)
        except KeyError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
        except SmartPlugError as exc:
            raise _translate_plug_error(exc) from exc
        return {
            "success": True,
            "snapshot": snap.model_dump(mode="json"),
        }

    batch = service.sync_all_plugs(
        provider=req.provider,
        category=req.category,
        room=req.room,
    )
    return {
        "success": True,
        "result": batch.model_dump(mode="json"),
    }


@router.post("/rules/parse")
async def parse_smart_plug_rule(
    payload: SmartPlugRuleParseRequest,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Parses and validates a custom smart plug category rule (DSL or dict), optionally saving it."""
    if payload.expression is None and payload.rule is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide either 'expression' (DSL string) or 'rule' (structured dict).",
        )
    rule_input: str | dict[str, Any] = (
        payload.expression if payload.expression is not None else payload.rule  # type: ignore[assignment]
    )
    try:
        parsed = service.parse_rule(
            rule_input,
            name=payload.name,
            enabled=payload.enabled,
            save=payload.save,
        )
    except SmartPlugRuleParseError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    return {
        "success": True,
        "saved": payload.save,
        "rule": parsed.model_dump(mode="json"),
    }


@router.get("/rules")
async def list_smart_plug_rules(
    category: Optional[str] = Query(default=None),
    room: Optional[str] = Query(default=None),
    enabled_only: bool = Query(default=False),
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Lists persisted smart plug category automation rules."""
    rules = service.list_rules(
        category=category,
        room=room,
        enabled_only=enabled_only,
    )
    return {
        "success": True,
        "count": len(rules),
        "rules": [r.model_dump(mode="json") for r in rules],
    }


@router.delete("/rules/{rule_id}")
async def delete_smart_plug_rule(
    rule_id: str,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Deletes a persisted smart plug category rule by ID."""
    if not service.delete_rule(rule_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Smart plug rule '{rule_id}' not found.",
        )
    return {
        "success": True,
        "rule_id": rule_id,
        "message": f"Smart plug rule '{rule_id}' deleted successfully.",
    }


# ---------------------------------------------------------------------------
# Endpoints — Per-Plug Dynamic Routes
# ---------------------------------------------------------------------------


@router.get("/{plug_id}")
async def get_smart_plug(
    plug_id: str,
    include_history: bool = Query(default=False),
    history_limit: int = Query(default=20, ge=1, le=200),
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Fetches a registered smart plug by ID (credentials masked)."""
    plug = service.get_plug(plug_id)
    if plug is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Smart plug '{plug_id}' not found.",
        )

    body: dict[str, Any] = {
        "success": True,
        "plug": plug.to_public_dict(),
    }
    if include_history:
        body["history"] = service.get_status_history(plug_id, limit=history_limit)
    return body


@router.delete("/{plug_id}")
async def delete_smart_plug(
    plug_id: str,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Deletes a registered smart plug and its telemetry history."""
    if not service.delete_plug(plug_id):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Smart plug '{plug_id}' not found.",
        )
    return {
        "success": True,
        "plug_id": plug_id,
        "message": f"Smart plug '{plug_id}' deleted successfully.",
    }


@router.post("/{plug_id}/power")
async def control_specific_smart_plug_power(
    plug_id: str,
    payload: SmartPlugPowerRequest,
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Sends an on/off/toggle control command to a specific registered smart plug."""
    try:
        result = service.send_power_command(
            plug_id,
            payload.action,
            channel=payload.channel,
        )
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except SmartPlugError as exc:
        raise _translate_plug_error(exc) from exc

    return {
        "success": True,
        "result": result.model_dump(mode="json"),
    }


@router.post("/{plug_id}/sync")
async def sync_specific_smart_plug(
    plug_id: str,
    raise_on_error: bool = Query(default=False),
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Syncs live status & power/wattage for a specific smart plug and persists snapshot."""
    try:
        snap = service.sync_plug(plug_id, raise_on_error=raise_on_error)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except SmartPlugError as exc:
        raise _translate_plug_error(exc) from exc

    return {
        "success": True,
        "snapshot": snap.model_dump(mode="json"),
    }


@router.get("/{plug_id}/history")
async def get_smart_plug_history(
    plug_id: str,
    limit: int = Query(default=50, ge=1, le=500),
    service: SmartPlugService = Depends(get_smart_plug_service),
):
    """Returns persisted status & wattage telemetry history for a smart plug."""
    try:
        history = service.get_status_history(plug_id, limit=limit)
    except KeyError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    return {
        "success": True,
        "plug_id": plug_id,
        "count": len(history),
        "history": history,
    }
