from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel
from pathlib import Path
from nova_core.auth import get_current_user

router = APIRouter()

class RollbackRequest(BaseModel):
    version: str

@router.post("/db/rollback")
def post_db_rollback(payload: RollbackRequest, request: Request, current_user: dict = Depends(get_current_user)):
    """Runs the rollback scripts for a specific migration version."""
    app = request.app
    store = app.state.store
    success = store.rollback_migration(payload.version)
    if not success:
        raise HTTPException(status_code=400, detail=f"Rollback failed for version '{payload.version}'")
    return {"success": True, "message": f"Successfully rolled back version '{payload.version}'."}

@router.get("/db/validate")
def get_db_validate(request: Request, current_user: dict = Depends(get_current_user)):
    """Validates structural database integrity and core tables."""
    app = request.app
    store = app.state.store
    return store.validate_database_integrity()

@router.post("/db/backup")
def post_db_backup(request: Request, current_user: dict = Depends(get_current_user)):
    """Backs up the database data into a local JSON fixture file."""
    app = request.app
    store = app.state.store
    backup_file = Path(__file__).parent.parent.parent / "nova_backup.json"
    res = store.export_data_to_json(backup_file)
    if not res.get("success"):
        raise HTTPException(status_code=500, detail=res.get("error", "Backup failed"))
    return res

@router.post("/db/vacuum")
def post_db_vacuum(request: Request, current_user: dict = Depends(get_current_user)):
    """Triggers db vacuum structural optimization."""
    app = request.app
    store = app.state.store
    res = store.vacuum_database()
    if not res.get("success"):
        raise HTTPException(status_code=500, detail=res.get("error", "Vacuum failed"))
    return res
