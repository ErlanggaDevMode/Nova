"""REST endpoints for command schema validation and execution history (Phase 14: F14.5)."""

from __future__ import annotations

import time
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from nova_core.router.validators import (
    ACTION_PARAM_SCHEMAS,
    get_action_json_schema,
    list_action_schemas,
    validate_command_payload,
)

router = APIRouter(prefix="/api/v1/commands", tags=["command-validation"])


class CommandValidateRequest(BaseModel):
    action_type: Optional[str] = Field(default=None, description="Target Nova action_type.")
    params: Optional[dict[str, Any] | str] = Field(
        default=None, description="Tool call arguments (dict or raw JSON string)."
    )
    raw_payload: Optional[str] = Field(
        default=None,
        description="Optional full JSON payload string containing action_type and params.",
    )
    source_device_id: str = Field(default="api", description="Originating device identifier.")
    record_history: bool = Field(
        default=False,
        description="When true, records the validation check into execution_history.",
    )


@router.post("/validate")
async def validate_command_endpoint(payload: CommandValidateRequest, request: Request):
    """Validates a structured Nova command payload and returns errors + repair suggestions."""
    start = time.perf_counter()

    if payload.raw_payload is not None:
        report = validate_command_payload(payload.raw_payload, action_type=payload.action_type)
    else:
        report = validate_command_payload(
            {
                "action_type": payload.action_type,
                "params": payload.params if payload.params is not None else {},
            }
        )

    duration_ms = round((time.perf_counter() - start) * 1000.0, 3)

    if payload.record_history:
        store = request.app.state.store
        store.log_execution_history(
            action_type=report.action_type or (payload.action_type or "unknown"),
            category=report.category or "unknown",
            source_device_id=payload.source_device_id,
            status="validated" if report.valid else "validation_error",
            params=report.normalized_params if report.normalized_params is not None else {},
            result={"repaired": report.repaired, "repairs_applied": report.repairs_applied},
            error_message="; ".join(e.message for e in report.errors) if report.errors else None,
            validated=report.valid,
            repaired=report.repaired,
            duration_ms=duration_ms,
        )

    return {
        "valid": report.valid,
        "action_type": report.action_type,
        "category": report.category,
        "normalized_params": report.normalized_params,
        "repaired": report.repaired,
        "repairs_applied": report.repairs_applied,
        "errors": [e.model_dump() for e in report.errors],
        "suggestions": report.suggestions,
        "schema": report.schema_definition,
        "duration_ms": duration_ms,
    }


@router.get("/schemas")
async def get_command_schemas():
    """Lists JSON Schemas for all supported Nova tool actions."""
    schemas = list_action_schemas()
    return {
        "success": True,
        "count": len(schemas),
        "schemas": schemas,
    }


@router.get("/schemas/{action_type}")
async def get_command_schema(action_type: str):
    """Returns the JSON Schema for a specific Nova action_type."""
    if action_type not in ACTION_PARAM_SCHEMAS:
        raise HTTPException(
            status_code=404,
            detail={
                "error": f"Unknown action_type '{action_type}'.",
                "available_actions": sorted(ACTION_PARAM_SCHEMAS.keys()),
            },
        )
    return {
        "success": True,
        "action_type": action_type,
        "schema": get_action_json_schema(action_type),
    }


@router.get("/history")
async def get_execution_history_endpoint(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    action_type: Optional[str] = Query(default=None),
    status: Optional[str] = Query(default=None),
    source_device_id: Optional[str] = Query(default=None),
):
    """Returns recorded command/action execution history and summary statistics."""
    store = request.app.state.store
    items = store.get_execution_history(
        limit=limit,
        offset=offset,
        action_type=action_type,
        status=status,
        source_device_id=source_device_id,
    )
    stats = store.get_execution_history_stats()
    return {
        "success": True,
        "count": len(items),
        "items": items,
        "stats": stats,
    }
