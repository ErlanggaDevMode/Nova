from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import asyncio
import datetime
from pathlib import Path
from nova_core.ws.connection_manager import ConnectionManager
from nova_core.db.store import DatabaseStore
from nova_core.permission_registry import PermissionRegistry
from nova_core.router.hybrid_router import HybridRouter
from nova_core.context.context_store import ContextStore
from nova_core.presence.presence_tracker import PresenceTracker
from nova_core.automation import RulesStore, AutomationEngine
from nova_core.auth import get_current_user, verify_access_token

# Import routers
from nova_core.api.routes_command import router as command_router
from nova_core.api.routes_capabilities import router as cap_router
from nova_core.api.routes_policy import router as policy_router
from nova_core.api.routes_history import router as history_router
from nova_core.api.routes_automation import router as auto_router
from nova_core.api.routes_event import router as event_router
from nova_core.api.routes_auth import router as auth_router

import uvicorn
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nova.core")

app = FastAPI(title="Nova Core Server", version="0.5.0")

# Initialize and attach states
store = DatabaseStore()
manager = ConnectionManager()
registry = PermissionRegistry()
context_store = ContextStore(store)
router_engine = HybridRouter()
presence_tracker = PresenceTracker(store, manager)
rules_store = RulesStore(store)
auto_engine = AutomationEngine(store, rules_store, registry, manager)

app.state.store = store
app.state.manager = manager
app.state.registry = registry
app.state.context_store = context_store
app.state.router = router_engine
app.state.presence = presence_tracker
app.state.rules_store = rules_store
app.state.automation = auto_engine

# Include open auth routes
app.include_router(auth_router)

# Include protected REST routes
app.include_router(command_router, dependencies=[Depends(get_current_user)])
app.include_router(cap_router, dependencies=[Depends(get_current_user)])
app.include_router(policy_router, dependencies=[Depends(get_current_user)])
app.include_router(history_router, dependencies=[Depends(get_current_user)])
app.include_router(auto_router, dependencies=[Depends(get_current_user)])
app.include_router(event_router, dependencies=[Depends(get_current_user)])

# Expose open static dashboard assets
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "web"), name="static")

@app.get("/")
async def get_dashboard():
    return FileResponse(Path(__file__).parent / "web" / "index.html")

@app.on_event("startup")
async def startup_event():
    # Start background tick loop for periodic automation triggers
    async def run_ticks():
        tick = 0
        logger.info("Automation background tick evaluation loop started.")
        while True:
            await asyncio.sleep(60)
            tick += 1
            now = datetime.datetime.now()
            event = {
                "type": "time",
                "time_of_day": now.strftime("%H:%M"),
                "tick_counter": tick
            }
            try:
                await app.state.automation.evaluate_and_fire(event)
            except Exception as e:
                logger.error(f"Automation tick evaluation failure: {e}")

    asyncio.create_task(run_ticks())

@app.websocket("/ws/{device_id}")
async def websocket_endpoint(websocket: WebSocket, device_id: str):
    # Perform JWT check from query params
    token = websocket.query_params.get("token")
    if not token:
        await websocket.accept()
        await websocket.close(code=4001, reason="Authentication token missing")
        return
    try:
        verify_access_token(token)
    except Exception:
        await websocket.accept()
        await websocket.close(code=4002, reason="Authentication failed")
        return

    manager_inst: ConnectionManager = app.state.manager
    store_inst: DatabaseStore = app.state.store
    presence_inst: PresenceTracker = app.state.presence
    
    device = store_inst.get_device(device_id)
    if not device:
        store_inst.register_device(device_id, f"Device {device_id[:4]}", "desktop", {})
        
    await manager_inst.connect(device_id, websocket)
    await presence_inst.on_connect(device_id)
    
    try:
        while True:
            data = await websocket.receive_json()
            event = data.get("event")
            
            if event == "action.result":
                action_id = data.get("action_id")
                result = data.get("result", {})
                manager_inst.resolve_action(action_id, result)
                
    except WebSocketDisconnect:
        manager_inst.disconnect(device_id)
        await presence_inst.on_disconnect(device_id)
    except Exception as e:
        logger.error(f"WebSocket error for '{device_id}': {e}")
        manager_inst.disconnect(device_id)
        await presence_inst.on_disconnect(device_id)

def main():
    uvicorn.run("nova_core.main:app", host="127.0.0.1", port=8000, reload=False)

if __name__ == "__main__":
    main()
