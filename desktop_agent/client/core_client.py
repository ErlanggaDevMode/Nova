import json
import asyncio
import threading
import logging
from typing import Callable, Optional

logger = logging.getLogger("nova.client")

class CoreClient:
    def __init__(self, device_id: str, server_url: str = "http://127.0.0.1:8000"):
        self.device_id = device_id
        self.server_url = server_url.rstrip('/')
        
        # Derive ws:// URL from http:// URL
        ws_scheme = "ws" if self.server_url.startswith("http://") else "wss"
        host_port = self.server_url.split("://")[1]
        self.ws_url = f"{ws_scheme}://{host_port}/ws/{self.device_id}"
        self.ws_connection = None
        self._action_callback: Optional[Callable[[dict], dict]] = None
        self.loop = None

    def register_capabilities(self, name: str, capabilities: dict) -> bool:
        try:
            import requests
            url = f"{self.server_url}/capabilities/{self.device_id}"
            payload = {
                "name": name,
                "platform": "desktop",
                "capabilities": capabilities
            }
            res = requests.post(url, json=payload, timeout=5)
            return res.status_code == 200
        except Exception as e:
            logger.error(f"Failed to register capabilities: {e}")
            return False

    def send_command(self, raw_text: str) -> dict:
        try:
            import requests
            url = f"{self.server_url}/command"
            payload = {
                "raw_text": raw_text,
                "source_device_id": self.device_id
            }
            res = requests.post(url, json=payload, timeout=10)
            if res.status_code == 200:
                return res.json()
            else:
                return {"success": False, "error": f"Server returned status {res.status_code}"}
        except Exception as e:
            return {"success": False, "error": f"HTTP request failed: {str(e)}"}

    def confirm_action(self, action_id: str) -> dict:
        try:
            import requests
            url = f"{self.server_url}/command/{action_id}/confirm"
            res = requests.post(url, timeout=60)
            if res.status_code == 200:
                return res.json()
            else:
                return {"success": False, "error": f"Server returned status {res.status_code}"}
        except Exception as e:
            return {"success": False, "error": f"HTTP confirmation failed: {str(e)}"}

    def start_websocket_listener(self, action_callback: Callable[[dict], dict]):
        self._action_callback = action_callback
        
        def run_loop():
            import websockets # Imported here to avoid missing module errors at startup
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(self._websocket_loop())

        thread = threading.Thread(target=run_loop, daemon=True)
        thread.start()

    async def _websocket_loop(self):
        import websockets
        while True:
            try:
                async with websockets.connect(self.ws_url) as ws:
                    self.ws_connection = ws
                    while True:
                        msg_str = await ws.recv()
                        data = json.loads(msg_str)
                        event = data.get("event")
                        
                        if event == "action.execute":
                            action_id = data.get("action_id")
                            # Run local callback to execute action
                            result = self._action_callback(data)
                            response = {
                                "event": "action.result",
                                "action_id": action_id,
                                "result": result
                            }
                            await ws.send(json.dumps(response))
            except Exception as e:
                self.ws_connection = None
                await asyncio.sleep(5)
