from fastapi import APIRouter, Request, Depends
from nova_core.auth import get_current_user

router = APIRouter()

@router.get("/database/status")
def get_db_status(request: Request, current_user: dict = Depends(get_current_user)):
    """
    Returns connection type, metrics counters, query durations, and pool levels.
    """
    app = request.app
    store = app.state.store
    
    metrics = store.get_metrics()
    return {
        "success": True,
        "status": "healthy",
        "metrics": metrics
    }
