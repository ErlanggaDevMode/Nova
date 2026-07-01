from fastapi import APIRouter, Request, Depends
from nova_core.auth import get_current_user

router = APIRouter()

@router.get("/context/dump")
def get_context_dump(request: Request, current_user: dict = Depends(get_current_user)):
    """
    Returns diagnostics on active context keys, budgets, priorities, and TTL counts.
    """
    app = request.app
    context_store = app.state.context_store
    return context_store.dump_diagnostics()

@router.get("/context/conflicts")
def get_context_conflicts(request: Request, limit: int = 20, current_user: dict = Depends(get_current_user)):
    """
    Returns logs of context synchronization conflicts and rejections.
    """
    app = request.app
    store = app.state.store
    return store.get_context_conflicts(limit=limit)
