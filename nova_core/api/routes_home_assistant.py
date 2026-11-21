"""REST endpoints for Home Assistant Event Adapters (Phase 21: F21.1–F21.5).

Mount in `nova_core/main.py` with:
    from nova_core.api.routes_home_assistant import router as home_assistant_router
    app.include_router(home_assistant_router, dependencies=[Depends(get_current_user)])
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nova_core.integrations.smart_home.home_assistant import (
    AttributeOperator,
    HAEntityTrigger,
    HomeAssistantAuthError,
    HomeAssistantConnectionError,
    HomeAssistantError,
    HomeAssistantService,
    HomeAssistantServiceError,
    HomeAssistantStorage,
)

router = APIRouter(prefix="/api/v1/home-assistant", tags=["home_assistant"])


def get_home_assistant_service(request: Request) -> HomeAssistantService:
    """Resolves or lazily initializes a HomeAssistantService bound to app.state.store."""
    service = getattr(request.app.state, "home_assistant_service", None)
    if service is not None:
        return service

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Home Assistant storage backend is not configured on app.state.",
        )

    ws_factory = getattr(request.app.state, "ha_ws_factory", None)
    http_requester = getattr(request.app.state, "ha_http_requester", None)
    on_event = getattr(request.app.state, "ha_event_callback", None)

    storage = HomeAssistantStorage(store)
    service = HomeAssistantService(
        storage=storage,
        ws_factory=ws_factory,
        http_requester=http_requester,
        on_event=on_event,
    )
    request.app.state.home_assistant_service = service
    return service


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class HAConfigUpdateRequest(BaseModel):
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=512)
    ws_url: Optional[str] = Field(default=None, max_length=512)
    access_token: Optional[str] = Field(default=None, max_length=1024)
    verify_ssl: Optional[bool] = Field(default=None)
    timeout_seconds: Optional[float] = Field(default=None, gt=0.0, le=120.0)
    reconnect_base_delay: Optional[float] = Field(default=None, gt=0.0, le=300.0)
    reconnect_max_delay: Optional[float] = Field(default=None, gt=0.0, le=3600.0)
    auto_subscribe_events: Optional[list[str]] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)


class HASyncRequest(BaseModel):
    entities: Optional[list[dict[str, Any]]] = Field(default=None)
    transport: Literal["auto", "ws", "rest"] = Field(default="auto")


class HATriggerCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    enabled: bool = Field(default=True)
    entity_id: str = Field(..., min_length=1, max_length=256)
    from_state: Optional[str] = Field(default=None)
    to_state: Optional[str] = Field(default=None)
    attribute_name: Optional[str] = Field(default=None, max_length=128)
    attribute_operator: Optional[AttributeOperator] = Field(default=None)
    attribute_value: Any = Field(default=None)
    cooldown_seconds: int = Field(default=0, ge=0, le=86400 * 7)
    event_type: str = Field(default="ha.state_trigger", min_length=1, max_length=128)
    description: Optional[str] = Field(default=None, max_length=512)


class HAEventIngestRequest(BaseModel):
    event: dict[str, Any] = Field(...)
    evaluated_at: Optional[datetime] = Field(default=None)


class HAServiceCallRequest(BaseModel):
    domain: str = Field(..., min_length=1, max_length=64)
    service: str = Field(..., min_length=1, max_length=64)
    entity_id: Optional[str] = Field(default=None, max_length=256)
    target: Optional[dict[str, Any]] = Field(default=None)
    service_data: Optional[dict[str, Any]] = Field(default=None)
    transport: Literal["auto", "ws", "rest"] = Field(default="auto")


# ---------------------------------------------------------------------------
# Endpoints (F21.5)
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_ha_config(
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Returns persisted Home Assistant configuration with access token masked."""
    cfg = service.get_config()
    return {
        "success": True,
        "config": cfg.to_public_dict(),
    }


@router.put("/config")
async def update_ha_config(
    payload: HAConfigUpdateRequest,
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Updates Home Assistant connection configuration and returns masked representation."""
    try:
        saved = service.update_config(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid Home Assistant configuration: {exc}",
        ) from exc

    return {
        "success": True,
        "config": saved.to_public_dict(),
    }


@router.get("/status")
async def get_ha_status(
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Returns WebSocket connection state, backoff info, and synced entity/trigger counts."""
    return {
        "success": True,
        "status": service.get_status(),
    }


@router.get("/entities")
async def list_ha_entities(
    domain: Optional[str] = Query(default=None),
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Lists synced Home Assistant entities and their mapped Nova capabilities."""
    entities = service.list_entities(domain=domain)
    return {
        "success": True,
        "count": len(entities),
        "entities": [e.model_dump(mode="json") for e in entities],
    }


@router.get("/entities/{entity_id}")
async def get_ha_entity(
    entity_id: str,
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Fetches a single synced Home Assistant entity by ID."""
    entity = service.get_entity(entity_id)
    if entity is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Home Assistant entity '{entity_id}' not found.",
        )
    return {
        "success": True,
        "entity": entity.model_dump(mode="json"),
    }


@router.post("/sync")
async def sync_ha_entities(
    payload: Optional[HASyncRequest] = None,
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Syncs Home Assistant entity states and device capability profiles into SQLite."""
    req = payload or HASyncRequest()
    try:
        result = await service.sync_entities(
            raw_states=req.entities,
            transport=req.transport,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HomeAssistantAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except HomeAssistantConnectionError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except HomeAssistantServiceError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return {
        "success": True,
        "sync": result.model_dump(mode="json"),
    }


@router.get("/triggers")
async def list_ha_triggers(
    enabled_only: bool = Query(default=False),
    entity_id: Optional[str] = Query(default=None),
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Lists registered Home Assistant entity state triggers."""
    triggers = service.list_triggers(
        enabled_only=enabled_only,
        entity_id=entity_id,
    )
    return {
        "success": True,
        "count": len(triggers),
        "triggers": [t.model_dump(mode="json") for t in triggers],
    }


@router.post("/triggers", status_code=status.HTTP_201_CREATED)
async def create_ha_trigger(
    payload: HATriggerCreateRequest,
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Creates a Home Assistant entity state or attribute trigger rule."""
    try:
        trigger = HAEntityTrigger(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid Home Assistant entity trigger: {exc}",
        ) from exc

    created = service.create_trigger(trigger)
    return {
        "success": True,
        "trigger": created.model_dump(mode="json"),
    }


@router.delete("/triggers/{trigger_id}")
async def delete_ha_trigger(
    trigger_id: str,
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Deletes a Home Assistant entity trigger by ID."""
    deleted = service.delete_trigger(trigger_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Home Assistant trigger '{trigger_id}' not found.",
        )
    return {
        "success": True,
        "trigger_id": trigger_id,
        "message": f"Home Assistant trigger '{trigger_id}' deleted successfully.",
    }


@router.post("/events/ingest")
async def ingest_ha_event(
    payload: HAEventIngestRequest,
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Ingests a Home Assistant event, evaluates entity triggers, and dispatches Nova event."""
    result = await service.ingest_event(
        payload.event,
        transport="ingest",
        evaluated_at=payload.evaluated_at,
    )
    return {
        "success": True,
        "dispatch": result.model_dump(mode="json"),
    }


@router.post("/services/call")
async def call_ha_service(
    payload: HAServiceCallRequest,
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Dispatches an outbound Home Assistant `call_service` command over WS or REST."""
    try:
        result = await service.call_service(
            domain=payload.domain,
            service=payload.service,
            entity_id=payload.entity_id,
            service_data=payload.service_data,
            target=payload.target,
            transport=payload.transport,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except HomeAssistantAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except HomeAssistantError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return {
        "success": True,
        "result": result.model_dump(mode="json"),
    }


@router.get("/capabilities")
async def get_ha_capabilities(
    domain: Optional[str] = Query(default=None),
    service: HomeAssistantService = Depends(get_home_assistant_service),
):
    """Returns Home Assistant domain capability sync maps and synced entity capability summaries."""
    return {
        "success": True,
        "capabilities": service.get_capabilities(domain=domain),
    }
