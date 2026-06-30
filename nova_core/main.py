from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from nova_core.ws.connection_manager import ConnectionManager
from nova_core.db.store import DatabaseStore
from nova_core.permission_registry import PermissionRegistry
from nova_core.router.hybrid_router import HybridRouter
from nova_core.context.context_store import ContextStore
from nova_core.presence.presence_tracker import PresenceTracker
from nova_core.api.routes_command import router as command_router
from nova_core.api.routes_capabilities import router as cap_router
from nova_core.api.routes_policy import router as policy_router
from nova_core.api.routes_history import router as history_router
import uvicorn
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nova.core")

app = FastAPI(title="Nova Core Server", version="0.3.0")

# Initialize and attach states
store = DatabaseStore()
manager = ConnectionManager()
registry = PermissionRegistry()
context_store = ContextStore(store)
router_engine = HybridRouter()
presence_tracker = PresenceTracker(store, manager)

app.state.store = store
app.state.manager = manager
app.state.registry = registry
app.state.context_store = context_store
app.state.router = router_engine
app.state.presence = presence_tracker

# Include REST routes
app.include_router(command_router)
app.include_router(cap_router)
app.include_router(policy_router)
app.include_router(history_router)

@app.websocket("/ws/{device_id}")
async def websocket_endpoint(websocket: WebSocket, device_id: str):
    manager_inst: ConnectionManager = app.state.manager
    store_inst: DatabaseStore = app.state.store
    presence_inst: PresenceTracker = app.state.presence
    
    device = store_inst.get_device(device_id)
    if not device:
        # Register a fallback device profile if not registered via REST
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
