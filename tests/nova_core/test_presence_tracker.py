import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi import WebSocket
from nova_core.db.store import DatabaseStore
from nova_core.ws.connection_manager import ConnectionManager
from nova_core.presence.presence_tracker import PresenceTracker

@pytest.mark.anyio
async def test_presence_tracker(tmp_path):
    db_file = tmp_path / "test_presence.db"
    store = DatabaseStore(db_path=db_file)
    manager = ConnectionManager()
    tracker = PresenceTracker(store, manager)

    # Create two mock clients
    ws_client1 = AsyncMock(spec=WebSocket)
    ws_client2 = AsyncMock(spec=WebSocket)

    # Register them
    await manager.connect("device1", ws_client1)
    await manager.connect("device2", ws_client2)

    # Connect device1
    await tracker.on_connect("device1")

    # Both clients receive the presence notification
    assert ws_client1.send_json.call_count == 1
    assert ws_client2.send_json.call_count == 1

    call_args = ws_client1.send_json.call_args[0][0]
    assert call_args["event"] == "presence.changed"
    assert call_args["device_id"] == "device1"
    assert call_args["status"] == "online"

    # Reset mocks
    ws_client1.send_json.reset_mock()
    ws_client2.send_json.reset_mock()

    # Disconnect device2
    manager.disconnect("device2")
    await tracker.on_disconnect("device2")

    # Only remaining client (device1) receives offline notification
    assert ws_client1.send_json.call_count == 1
    assert ws_client2.send_json.call_count == 0

    call_args_offline = ws_client1.send_json.call_args[0][0]
    assert call_args_offline["event"] == "presence.changed"
    assert call_args_offline["device_id"] == "device2"
    assert call_args_offline["status"] == "offline"
