"""REST endpoints for Weather API & Conditions (Phase 17: F17.1–F17.5).

Mount in `nova_core/main.py` with:
    from nova_core.api.routes_weather import router as weather_router
    app.include_router(weather_router, dependencies=[Depends(get_current_user)])
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from nova_core.integrations.weather import (
    AlertSeverity,
    EntityType,
    WeatherAlertRule,
    WeatherAuthError,
    WeatherClientError,
    WeatherRuleType,
    WeatherService,
    WeatherSnapshot,
    WeatherStorage,
    WeatherTimeoutError,
    WeatherUnits,
)

router = APIRouter(prefix="/api/v1/weather", tags=["weather"])


def get_weather_service(request: Request) -> WeatherService:
    """Resolves or lazily initializes a WeatherService bound to app.state.store."""
    service = getattr(request.app.state, "weather_service", None)
    if service is not None:
        return service

    store = getattr(request.app.state, "store", None)
    if store is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Weather storage backend is not configured on app.state.",
        )

    fetcher = getattr(request.app.state, "weather_http_fetcher", None)
    storage = WeatherStorage(store)
    service = WeatherService(storage=storage, fetcher=fetcher)
    request.app.state.weather_service = service
    return service


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class WeatherConfigUpdateRequest(BaseModel):
    provider: Optional[str] = Field(default=None, min_length=1, max_length=64)
    api_key: Optional[str] = Field(default=None, max_length=256)
    base_url: Optional[str] = Field(default=None, min_length=1, max_length=512)
    units: Optional[WeatherUnits] = Field(default=None)
    cache_ttl_seconds: Optional[int] = Field(default=None, ge=0, le=86400)
    timeout_seconds: Optional[float] = Field(default=None, gt=0.0, le=120.0)
    default_city: Optional[str] = Field(default=None, max_length=128)
    default_lat: Optional[float] = Field(default=None)
    default_lon: Optional[float] = Field(default=None)
    enabled: Optional[bool] = Field(default=None)


class WeatherLocationUpdateRequest(BaseModel):
    entity_id: str = Field(default="default", min_length=1, max_length=128)
    entity_type: EntityType = Field(default="device")
    city: Optional[str] = Field(default=None, max_length=128)
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    label: Optional[str] = Field(default=None, max_length=128)


class WeatherRuleCreateRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    enabled: bool = Field(default=True)
    rule_type: WeatherRuleType
    threshold: Optional[float] = Field(default=None)
    units: WeatherUnits = Field(default="metric")
    from_conditions: list[str] = Field(default_factory=list)
    to_conditions: list[str] = Field(default_factory=list)
    entity_id: Optional[str] = Field(default=None, max_length=128)
    cooldown_seconds: int = Field(default=600, ge=0, le=86400 * 7)
    severity: AlertSeverity = Field(default="warning")
    description: Optional[str] = Field(default=None, max_length=512)


class WeatherSnapshotInput(BaseModel):
    location_name: str = Field(default="Unknown")
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    temperature: float
    feels_like: Optional[float] = Field(default=None)
    humidity: float = Field(..., ge=0.0, le=100.0)
    pressure: Optional[float] = Field(default=None)
    wind_speed: Optional[float] = Field(default=None)
    condition: str = Field(..., min_length=1)
    description: str = Field(default="")
    units: WeatherUnits = Field(default="metric")


class WeatherEvaluateRequest(BaseModel):
    entity_id: Optional[str] = Field(default=None, max_length=128)
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    city: Optional[str] = Field(default=None, max_length=128)
    units: Optional[WeatherUnits] = Field(default=None)
    snapshot: Optional[WeatherSnapshotInput] = Field(default=None)
    previous_condition: Optional[str] = Field(default=None)
    force_refresh: bool = Field(default=False)
    evaluated_at: Optional[datetime] = Field(default=None)


# ---------------------------------------------------------------------------
# Endpoints (F17.5)
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_weather_config(
    service: WeatherService = Depends(get_weather_service),
):
    """Returns persisted weather configuration with API key masked."""
    cfg = service.get_config()
    return {
        "success": True,
        "config": cfg.to_public_dict(),
    }


@router.put("/config")
async def update_weather_config(
    payload: WeatherConfigUpdateRequest,
    service: WeatherService = Depends(get_weather_service),
):
    """Updates weather API configuration and returns masked representation."""
    try:
        saved = service.update_config(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid weather configuration: {exc}",
        ) from exc

    return {
        "success": True,
        "config": saved.to_public_dict(),
    }


@router.put("/location")
async def update_weather_location(
    payload: WeatherLocationUpdateRequest,
    service: WeatherService = Depends(get_weather_service),
):
    """Updates dynamic weather location (coordinates or city) for a device or user."""
    try:
        saved = service.locations.update_location(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid location update: {exc}",
        ) from exc

    return {
        "success": True,
        "location": saved.model_dump(mode="json"),
    }


@router.get("/location")
async def get_weather_locations(
    entity_id: Optional[str] = Query(default=None),
    entity_type: Optional[EntityType] = Query(default=None),
    service: WeatherService = Depends(get_weather_service),
):
    """Fetches a specific entity location or lists all registered locations."""
    if entity_id:
        loc = service.locations.get_location(entity_id)
        if loc is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Location for entity '{entity_id}' not found.",
            )
        return {
            "success": True,
            "location": loc.model_dump(mode="json"),
        }

    items = service.locations.list_locations(entity_type=entity_type)
    return {
        "success": True,
        "count": len(items),
        "locations": [loc.model_dump(mode="json") for loc in items],
    }


@router.post("/rules", status_code=status.HTTP_201_CREATED)
async def create_weather_rule(
    payload: WeatherRuleCreateRequest,
    service: WeatherService = Depends(get_weather_service),
):
    """Creates a weather threshold or condition-change alert rule."""
    try:
        rule = WeatherAlertRule(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid weather alert rule: {exc}",
        ) from exc

    created = service.create_rule(rule)
    return {
        "success": True,
        "rule": created.model_dump(mode="json"),
    }


@router.get("/rules")
async def list_weather_rules(
    enabled_only: bool = Query(default=False),
    rule_type: Optional[WeatherRuleType] = Query(default=None),
    entity_id: Optional[str] = Query(default=None),
    service: WeatherService = Depends(get_weather_service),
):
    """Lists configured weather alert rules."""
    rules = service.list_rules(
        enabled_only=enabled_only,
        rule_type=rule_type,
        entity_id=entity_id,
    )
    return {
        "success": True,
        "count": len(rules),
        "rules": [r.model_dump(mode="json") for r in rules],
    }


@router.delete("/rules/{rule_id}")
async def delete_weather_rule(
    rule_id: str,
    service: WeatherService = Depends(get_weather_service),
):
    """Deletes a weather alert rule by ID."""
    deleted = service.delete_rule(rule_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Weather alert rule '{rule_id}' not found.",
        )
    return {
        "success": True,
        "rule_id": rule_id,
        "message": f"Weather alert rule '{rule_id}' deleted successfully.",
    }


@router.post("/evaluate")
async def evaluate_weather(
    payload: WeatherEvaluateRequest,
    service: WeatherService = Depends(get_weather_service),
):
    """Evaluates weather thresholds and condition-change triggers for a location or snapshot."""
    snap_model: Optional[WeatherSnapshot] = None
    if payload.snapshot is not None:
        try:
            snap_model = WeatherSnapshot(**payload.snapshot.model_dump())
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Invalid weather snapshot: {exc}",
            ) from exc

    try:
        result = service.evaluate(
            entity_id=payload.entity_id,
            lat=payload.lat,
            lon=payload.lon,
            city=payload.city,
            units=payload.units,
            snapshot=snap_model,
            previous_condition=payload.previous_condition,
            force_refresh=payload.force_refresh,
            evaluated_at=payload.evaluated_at,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc
    except WeatherAuthError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc
    except WeatherTimeoutError as exc:
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail=str(exc),
        ) from exc
    except WeatherClientError as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc),
        ) from exc

    return {
        "success": True,
        "result": result.model_dump(mode="json"),
    }
