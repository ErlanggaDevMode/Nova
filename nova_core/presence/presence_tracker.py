from datetime import datetime, timezone
import logging
from nova_core.db.store import DatabaseStore
from nova_core.ws.connection_manager import ConnectionManager

logger = logging.getLogger("nova.presence")

class PresenceTracker:
    def __init__(self, store: DatabaseStore, manager: ConnectionManager):
        self.store = store
        self.manager = manager

    async def on_connect(self, device_id: str) -> None:
        """
        Handles device connection event, registering/updating the device status,
        and broadcasting presence change to all active WebSocket clients.
        """
        device = self.store.get_device(device_id)
        if device:
            # Re-register to update the last_seen_at timestamp
            self.store.register_device(
                device_id=device_id,
                name=device["name"],
                platform=device["platform"],
                capabilities=device["capabilities"]
            )
        
        logger.info(f"Presence: device '{device_id}' is online.")
        await self._broadcast_presence(device_id, "online")

    async def on_disconnect(self, device_id: str) -> None:
        """
        Handles device disconnection event, broadcasting status update.
        """
        logger.info(f"Presence: device '{device_id}' is offline.")
        await self._broadcast_presence(device_id, "offline")

    async def _broadcast_presence(self, device_id: str, status: str) -> None:
        payload = {
            "event": "presence.changed",
            "device_id": device_id,
            "status": status,
            "timestamp": datetime.now(timezone.utc).isoformat()
        }
        
        # Broadcast presence event to all active WebSocket clients
        for dev_id, ws in list(self.manager.active_connections.items()):
            try:
                await ws.send_json(payload)
            except Exception as e:
                logger.error(f"Failed to send presence update to '{dev_id}': {e}")
