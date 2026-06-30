import asyncio
from typing import Dict
from fastapi import WebSocket
from nova_core.models import ActionRequest

class ConnectionManager:
    def __init__(self):
        # Maps device_id -> WebSocket
        self.active_connections: Dict[str, WebSocket] = {}
        # Maps action_id -> asyncio.Future (to wait for results)
        self.pending_actions: Dict[str, asyncio.Future] = {}

    async def connect(self, device_id: str, websocket: WebSocket):
        await websocket.accept()
        self.active_connections[device_id] = websocket

    def disconnect(self, device_id: str):
        if device_id in self.active_connections:
            del self.active_connections[device_id]

    async def send_action_request(self, device_id: str, action_id: str, action: ActionRequest) -> dict:
        """
        Dispatches an action to a connected client and waits for the result.
        """
        if device_id not in self.active_connections:
            return {"success": False, "error": f"Device '{device_id}' is not connected."}

        websocket = self.active_connections[device_id]
        future = asyncio.get_running_loop().create_future()
        self.pending_actions[action_id] = future

        payload = {
            "event": "action.execute",
            "action_id": action_id,
            "action_type": action.action_type,
            "category": action.category,
            "params": action.params
        }

        try:
            await websocket.send_json(payload)
            # Wait for response (up to 60 seconds)
            result = await asyncio.wait_for(future, timeout=60.0)
            return result
        except asyncio.TimeoutError:
            return {"success": False, "error": "Execution timed out on the client."}
        except Exception as e:
            return {"success": False, "error": f"Failed to dispatch action: {str(e)}"}
        finally:
            if action_id in self.pending_actions:
                del self.pending_actions[action_id]

    def resolve_action(self, action_id: str, result: dict):
        if action_id in self.pending_actions:
            if not self.pending_actions[action_id].done():
                self.pending_actions[action_id].set_result(result)
