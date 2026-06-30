from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from nova_core.ws.connection_manager import ConnectionManager
from nova_core.db.store import DatabaseStore
from nova_core.permission_registry import PermissionRegistry
from nova_core.router.hybrid_router import HybridRouter
from nova_core.api.routes_command import router as command_router
from nova_core.api.routes_capabilities import router as cap_router
from nova_core.api.routes_policy import router as policy_router
import uvicorn
import logging

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("nova.core")

app = FastAPI(title="Nova Core Server", version="0.2.0")

# Initialize and attach states
app.state.store = DatabaseStore()
app.state.registry = PermissionRegistry()
app.state.manager = ConnectionManager()
app.state.router = HybridRouter()

# Include REST routes
app.include_router(command_router)
app.include_router(cap_router)
app.include_router(policy_router)

@app.websocket("/ws/{device_id}")
async def websocket_endpoint(websocket: WebSocket, device_id: str):
    manager: ConnectionManager = app.state.manager
    store: DatabaseStore = app.state.store
    
    device = store.get_device(device_id)
    if not device:
        # Register a fallback device profile if not registered via REST
        store.register_device(device_id, f"Device {device_id[:4]}", "desktop", {})
        
    await manager.connect(device_id, websocket)
    logger.info(f"Device '{device_id}' connected via WebSocket.")
    
    try:
        while True:
            data = await websocket.receive_json()
            event = data.get("event")
            
            if event == "action.result":
                action_id = data.get("action_id")
                result = data.get("result", {})
                manager.resolve_action(action_id, result)
                
    except WebSocketDisconnect:
        manager.disconnect(device_id)
        logger.info(f"Device '{device_id}' disconnected.")
    except Exception as e:
        logger.error(f"WebSocket error for '{device_id}': {e}")
        manager.disconnect(device_id)

def main():
    uvicorn.run("nova_core.main:app", host="127.0.0.1", port=8000, reload=False)

if __name__ == "__main__":
    main()
