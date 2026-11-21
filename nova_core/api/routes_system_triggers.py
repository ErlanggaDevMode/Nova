"""REST endpoints for Platform System Event Triggers (Phase 18: F18.1–F18.5).

Mount in ``nova_core/main.py`` with:
    from nova_core.api.routes_system_triggers import router as system_triggers_router
    app.include_router(system_triggers_router, dependencies=[Depends(get_current_user)])
"""

from __future__ import annotations

import json
import logging
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field, ValidationError

from desktop_agent.system_triggers import (
    BLOCKED_AUTONOMOUS_CATEGORIES,
    DiskThresholdConfig,
    LowBatteryConfig,
    NetworkConnectivityConfig,
    ResourceThresholdConfig,
    SessionLockConfig,
    SystemTriggerEvent,
    SystemTriggersConfig,
    TriggerSeverity,
    TriggerType,
)

logger = logging.getLogger("nova.api.system_triggers")

router = APIRouter(prefix="/api/v1/triggers/system", tags=["system_triggers"])


# ---------------------------------------------------------------------------
# Request Schemas
# ---------------------------------------------------------------------------


class SystemTriggerConfigPatch(BaseModel):
    """Partial update payload for system trigger thresholds and policies."""

    enabled: Optional[bool] = Field(default=None)
    poll_interval_seconds: Optional[float] = Field(default=None, ge=0.1, le=3600.0)
    disk: Optional[DiskThresholdConfig] = Field(default=None)
    resources: Optional[ResourceThresholdConfig] = Field(default=None)
    network: Optional[NetworkConnectivityConfig] = Field(default=None)
    session_lock: Optional[SessionLockConfig] = Field(default=None)
    battery: Optional[LowBatteryConfig] = Field(default=None)


class SystemTriggerEventIngestRequest(BaseModel):
    """Incoming system trigger event from a desktop agent or monitor."""

    event_id: Optional[str] = Field(default=None, max_length=128)
    trigger_type: TriggerType
    event_name: str = Field(..., min_length=1, max_length=128)
    severity: TriggerSeverity = Field(default=TriggerSeverity.INFO)
    source_device_id: str = Field(default="desktop_agent_client", max_length=128)
    timestamp: Optional[datetime] = Field(default=None)
    message: str = Field(default="")
    metrics: dict[str, Any] = Field(default_factory=dict)
    state_transition: Optional[dict[str, Any]] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)
    forward_to_automation: bool = Field(default=True)


def _validate_safe_trigger_config(config: SystemTriggersConfig) -> None:
    """Rejects unsafe autonomous action definitions (e.g. ``shell_command``)."""
    for action in config.battery.actions:
        if action.category.strip().lower() in BLOCKED_AUTONOMOUS_CATEGORIES:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    f"Autonomous trigger action '{action.action_type}' with category "
                    f"'{action.category}' is not permitted for background system triggers."
                ),
            )


# ---------------------------------------------------------------------------
# Storage Adapter (DatabaseStore-backed or In-Memory fallback, zero store.py edits)
# ---------------------------------------------------------------------------


class SystemTriggerStorage:
    """Persists system trigger configs and event history.

    When attached to ``DatabaseStore`` (``app.state.store``), manages its own
    ``system_trigger_configs`` and ``system_trigger_history`` tables cleanly
    without modifying ``nova_core/db/store.py``. Falls back to a thread-safe
    in-memory store when no database store is present on ``app.state``.
    """

    def __init__(self, db_store: Any = None) -> None:
        self._db_store = db_store
        self._lock = threading.Lock()
        self._mem_configs: dict[str, dict[str, Any]] = {}
        self._mem_history: list[dict[str, Any]] = []
        self._mem_seq: int = 0
        if self._db_store is not None:
            self._init_tables()

    def _connection(self):
        return self._db_store.get_connection()

    def _init_tables(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_trigger_configs (
                    device_id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS system_trigger_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_id TEXT NOT NULL,
                    trigger_type TEXT NOT NULL,
                    event_name TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    source_device_id TEXT NOT NULL,
                    message TEXT NOT NULL,
                    metrics_json TEXT NOT NULL,
                    state_transition_json TEXT,
                    metadata_json TEXT NOT NULL,
                    event_timestamp TEXT NOT NULL,
                    received_at TEXT NOT NULL
                )
                """
            )

    # ---- Config CRUD ----

    def get_config(self, device_id: str = "default") -> SystemTriggersConfig:
        if self._db_store is None:
            with self._lock:
                raw = self._mem_configs.get(device_id)
            return SystemTriggersConfig(**raw) if raw else SystemTriggersConfig()

        with self._connection() as conn:
            row = conn.execute(
                "SELECT config_json FROM system_trigger_configs WHERE device_id = ?",
                (device_id,),
            ).fetchone()
        if not row:
            return SystemTriggersConfig()
        data = json.loads(dict(row)["config_json"])
        return SystemTriggersConfig(**data)

    def save_config(
        self, config: SystemTriggersConfig, device_id: str = "default"
    ) -> SystemTriggersConfig:
        payload = config.model_dump(mode="json")
        now_iso = datetime.now(timezone.utc).isoformat()

        if self._db_store is None:
            with self._lock:
                self._mem_configs[device_id] = payload
            return config

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO system_trigger_configs (device_id, config_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET
                    config_json = excluded.config_json,
                    updated_at = excluded.updated_at
                """,
                (device_id, json.dumps(payload), now_iso),
            )
        return config

    # ---- Event History ----

    def record_event(self, event: SystemTriggerEvent) -> dict[str, Any]:
        received_at = datetime.now(timezone.utc).isoformat()
        event_ts = event.timestamp.astimezone(timezone.utc).isoformat()

        if self._db_store is None:
            with self._lock:
                self._mem_seq += 1
                item = {
                    "id": self._mem_seq,
                    "event_id": event.event_id,
                    "trigger_type": event.trigger_type.value,
                    "event_name": event.event_name,
                    "severity": event.severity.value,
                    "source_device_id": event.source_device_id,
                    "message": event.message,
                    "metrics": dict(event.metrics),
                    "state_transition": (
                        dict(event.state_transition) if event.state_transition else None
                    ),
                    "metadata": dict(event.metadata),
                    "timestamp": event_ts,
                    "received_at": received_at,
                }
                self._mem_history.append(item)
                return item

        with self._connection() as conn:
            cursor = conn.execute(
                """
                INSERT INTO system_trigger_history (
                    event_id, trigger_type, event_name, severity, source_device_id,
                    message, metrics_json, state_transition_json, metadata_json,
                    event_timestamp, received_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.trigger_type.value,
                    event.event_name,
                    event.severity.value,
                    event.source_device_id,
                    event.message,
                    json.dumps(event.metrics),
                    json.dumps(event.state_transition)
                    if event.state_transition is not None
                    else None,
                    json.dumps(event.metadata),
                    event_ts,
                    received_at,
                ),
            )
            row_id = getattr(cursor, "lastrowid", None)
            if row_id is None:
                row = conn.execute(
                    "SELECT id FROM system_trigger_history WHERE event_id = ? ORDER BY id DESC LIMIT 1",
                    (event.event_id,),
                ).fetchone()
                row_id = dict(row)["id"] if row else None

        return {
            "id": row_id,
            "event_id": event.event_id,
            "trigger_type": event.trigger_type.value,
            "event_name": event.event_name,
            "severity": event.severity.value,
            "source_device_id": event.source_device_id,
            "message": event.message,
            "metrics": dict(event.metrics),
            "state_transition": (
                dict(event.state_transition) if event.state_transition else None
            ),
            "metadata": dict(event.metadata),
            "timestamp": event_ts,
            "received_at": received_at,
        }

    @staticmethod
    def _row_to_dict(row: Any) -> dict[str, Any]:
        d = dict(row)
        return {
            "id": d["id"],
            "event_id": d["event_id"],
            "trigger_type": d["trigger_type"],
            "event_name": d["event_name"],
            "severity": d["severity"],
            "source_device_id": d["source_device_id"],
            "message": d["message"],
            "metrics": json.loads(d["metrics_json"]) if d.get("metrics_json") else {},
            "state_transition": (
                json.loads(d["state_transition_json"])
                if d.get("state_transition_json")
                else None
            ),
            "metadata": json.loads(d["metadata_json"]) if d.get("metadata_json") else {},
            "timestamp": d["event_timestamp"],
            "received_at": d["received_at"],
        }

    def list_history(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        trigger_type: Optional[str] = None,
        severity: Optional[str] = None,
        source_device_id: Optional[str] = None,
        event_name: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        if self._db_store is None:
            with self._lock:
                items = list(reversed(self._mem_history))
            if trigger_type:
                items = [i for i in items if i["trigger_type"] == trigger_type]
            if severity:
                items = [i for i in items if i["severity"] == severity]
            if source_device_id:
                items = [i for i in items if i["source_device_id"] == source_device_id]
            if event_name:
                items = [i for i in items if i["event_name"] == event_name]
            return items[offset : offset + limit]

        clauses: list[str] = []
        params: list[Any] = []
        if trigger_type:
            clauses.append("trigger_type = ?")
            params.append(trigger_type)
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if source_device_id:
            clauses.append("source_device_id = ?")
            params.append(source_device_id)
        if event_name:
            clauses.append("event_name = ?")
            params.append(event_name)

        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM system_trigger_history {where_sql} "
            "ORDER BY id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_dict(r) for r in rows]

    def get_stats(self, *, source_device_id: Optional[str] = None) -> dict[str, Any]:
        items = self.list_history(limit=10000, offset=0, source_device_id=source_device_id)
        by_type: dict[str, int] = {}
        by_severity: dict[str, int] = {}
        latest_by_type: dict[str, dict[str, Any]] = {}

        for item in items:
            t = item["trigger_type"]
            s = item["severity"]
            by_type[t] = by_type.get(t, 0) + 1
            by_severity[s] = by_severity.get(s, 0) + 1
            if t not in latest_by_type:
                latest_by_type[t] = {
                    "event_id": item["event_id"],
                    "event_name": item["event_name"],
                    "severity": s,
                    "timestamp": item["timestamp"],
                    "source_device_id": item["source_device_id"],
                    "state_transition": item["state_transition"],
                }

        return {
            "total": len(items),
            "by_trigger_type": by_type,
            "by_severity": by_severity,
            "latest_event_at": items[0]["timestamp"] if items else None,
            "latest_by_type": latest_by_type,
        }

    def clear_history(self, *, source_device_id: Optional[str] = None) -> int:
        if self._db_store is None:
            with self._lock:
                if source_device_id is None:
                    removed = len(self._mem_history)
                    self._mem_history.clear()
                    return removed
                before = len(self._mem_history)
                self._mem_history = [
                    i for i in self._mem_history if i["source_device_id"] != source_device_id
                ]
                return before - len(self._mem_history)

        with self._connection() as conn:
            if source_device_id is None:
                count_row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM system_trigger_history"
                ).fetchone()
                removed = int(dict(count_row)["cnt"]) if count_row else 0
                conn.execute("DELETE FROM system_trigger_history")
                return removed
            count_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM system_trigger_history WHERE source_device_id = ?",
                (source_device_id,),
            ).fetchone()
            removed = int(dict(count_row)["cnt"]) if count_row else 0
            conn.execute(
                "DELETE FROM system_trigger_history WHERE source_device_id = ?",
                (source_device_id,),
            )
            return removed


def get_system_trigger_storage(request: Request) -> SystemTriggerStorage:
    """Resolves or lazily initializes ``SystemTriggerStorage`` on ``app.state``."""
    db_store = getattr(request.app.state, "store", None)
    storage = getattr(request.app.state, "system_trigger_storage", None)
    if storage is not None and storage._db_store is db_store:
        return storage

    storage = SystemTriggerStorage(db_store=db_store)
    request.app.state.system_trigger_storage = storage
    return storage


# ---------------------------------------------------------------------------
# REST Endpoints (/api/v1/triggers/system)
# ---------------------------------------------------------------------------


@router.get("/config")
async def get_system_trigger_config(
    device_id: str = Query(default="default", min_length=1, max_length=128),
    storage: SystemTriggerStorage = Depends(get_system_trigger_storage),
):
    """Returns configured thresholds and action policies for system triggers."""
    config = storage.get_config(device_id=device_id)
    return {
        "success": True,
        "device_id": device_id,
        "config": config.model_dump(mode="json"),
    }


@router.put("/config")
async def replace_system_trigger_config(
    payload: SystemTriggersConfig,
    device_id: str = Query(default="default", min_length=1, max_length=128),
    storage: SystemTriggerStorage = Depends(get_system_trigger_storage),
):
    """Replaces and persists the full system trigger threshold configuration."""
    _validate_safe_trigger_config(payload)
    saved = storage.save_config(payload, device_id=device_id)
    return {
        "success": True,
        "device_id": device_id,
        "config": saved.model_dump(mode="json"),
    }


@router.patch("/config")
async def patch_system_trigger_config(
    payload: SystemTriggerConfigPatch,
    device_id: str = Query(default="default", min_length=1, max_length=128),
    storage: SystemTriggerStorage = Depends(get_system_trigger_storage),
):
    """Partially updates one or more system trigger threshold sections."""
    current = storage.get_config(device_id=device_id)
    merged_data = current.model_dump()
    patch_data = payload.model_dump(exclude_none=True)
    merged_data.update(patch_data)

    try:
        updated = SystemTriggersConfig(**merged_data)
    except (ValueError, ValidationError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid system trigger configuration: {exc}",
        ) from exc

    _validate_safe_trigger_config(updated)
    saved = storage.save_config(updated, device_id=device_id)
    return {
        "success": True,
        "device_id": device_id,
        "config": saved.model_dump(mode="json"),
    }


@router.post("/events", status_code=status.HTTP_201_CREATED)
async def ingest_system_trigger_event(
    payload: SystemTriggerEventIngestRequest,
    request: Request,
    storage: SystemTriggerStorage = Depends(get_system_trigger_storage),
):
    """Ingests a system trigger event, logs it to history, and optionally fires automation."""
    event = SystemTriggerEvent(
        event_id=payload.event_id or uuid.uuid4().hex,
        trigger_type=payload.trigger_type,
        event_name=payload.event_name,
        severity=payload.severity,
        source_device_id=payload.source_device_id,
        timestamp=payload.timestamp or datetime.now(timezone.utc),
        message=payload.message,
        metrics=payload.metrics,
        state_transition=payload.state_transition,
        metadata=payload.metadata,
    )
    stored = storage.record_event(event)

    automation_forwarded = False
    automation_error: Optional[str] = None
    if payload.forward_to_automation:
        automation = getattr(request.app.state, "automation", None)
        if automation is not None and hasattr(automation, "evaluate_and_fire"):
            try:
                await automation.evaluate_and_fire(event.to_automation_event())
                automation_forwarded = True
            except Exception as exc:
                automation_error = str(exc)
                logger.error(
                    "AutomationEngine failed handling system trigger event '%s': %s",
                    event.event_name,
                    exc,
                )

    return {
        "success": True,
        "event": stored,
        "automation_forwarded": automation_forwarded,
        "automation_error": automation_error,
    }


@router.get("/history")
async def get_system_trigger_history(
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
    trigger_type: Optional[TriggerType] = Query(default=None),
    severity: Optional[TriggerSeverity] = Query(default=None),
    source_device_id: Optional[str] = Query(default=None),
    event_name: Optional[str] = Query(default=None),
    storage: SystemTriggerStorage = Depends(get_system_trigger_storage),
):
    """Lists persisted system trigger events with filters and summary stats."""
    items = storage.list_history(
        limit=limit,
        offset=offset,
        trigger_type=trigger_type.value if trigger_type else None,
        severity=severity.value if severity else None,
        source_device_id=source_device_id,
        event_name=event_name,
    )
    stats = storage.get_stats(source_device_id=source_device_id)
    return {
        "success": True,
        "count": len(items),
        "items": items,
        "stats": stats,
    }


@router.delete("/history")
async def clear_system_trigger_history(
    source_device_id: Optional[str] = Query(default=None),
    storage: SystemTriggerStorage = Depends(get_system_trigger_storage),
):
    """Clears stored system trigger event history (optionally scoped to one device)."""
    deleted = storage.clear_history(source_device_id=source_device_id)
    return {
        "success": True,
        "deleted": deleted,
    }


@router.get("/status")
async def get_system_trigger_status(
    device_id: str = Query(default="default", min_length=1, max_length=128),
    storage: SystemTriggerStorage = Depends(get_system_trigger_storage),
):
    """Returns active trigger configuration summary and latest observed trigger states."""
    config = storage.get_config(device_id=device_id)
    stats = storage.get_stats(
        source_device_id=None if device_id == "default" else device_id
    )
    return {
        "success": True,
        "device_id": device_id,
        "enabled": config.enabled,
        "config": config.model_dump(mode="json"),
        "stats": stats,
    }
