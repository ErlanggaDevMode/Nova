import pytest
import time
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient
from nova_core.main import app
from nova_core.db.store import DatabaseStore
from nova_core.context.context_store import ContextStore
from nova_core.ws.connection_manager import ConnectionManager

def test_context_deep_merge(tmp_path):
    db_file = tmp_path / "test_merge.db"
    store = DatabaseStore(db_path=db_file)
    context_store = ContextStore(store)
    
    # Init state dict
    context_store.set_state("active_task", {"task": "Nova core", "details": {"progress": 10}}, "desktop_agent")
    
    # Perform partial update (should merge instead of complete override)
    context_store.set_state("active_task", {"details": {"time_spent": 120}, "owner": "erlangga"}, "desktop_agent")
    
    val = context_store.get_state("active_task")
    assert val["task"] == "Nova core"
    assert val["owner"] == "erlangga"
    assert val["details"]["progress"] == 10
    assert val["details"]["time_spent"] == 120

def test_context_conflict_rejection(tmp_path):
    db_file = tmp_path / "test_conflict.db"
    store = DatabaseStore(db_path=db_file)
    context_store = ContextStore(store)
    
    # 1. Update from desktop_agent (priority 3)
    context_store.set_state("playback_status", {"state": "playing"}, "desktop_agent")
    
    # 2. Immediate update from web_dashboard (priority 1) - should be rejected and logged
    context_store.set_state("playback_status", {"state": "paused"}, "web_dashboard")
    
    val = context_store.get_state("playback_status")
    # Value should remain "playing" since low priority dashboard was rejected
    assert val["state"] == "playing"
    
    # Verify conflict log exists
    conflicts = store.get_context_conflicts()
    assert len(conflicts) == 1
    assert conflicts[0]["winning_device_id"] == "desktop_agent"
    assert conflicts[0]["losing_device_id"] == "web_dashboard"
    assert "playback_status" in conflicts[0]["key"]

def test_context_ws_delta_broadcasts(tmp_path):
    db_file = tmp_path / "test_broadcast.db"
    store = DatabaseStore(db_path=db_file)
    
    broadcast_calls = []
    async def mock_broadcast(msg):
        broadcast_calls.append(msg)
        
    manager = ConnectionManager()
    manager.broadcast = mock_broadcast
    
    context_store = ContextStore(store, manager=manager)
    
    # Setting state should trigger WebSocket broadcast
    context_store.set_state("current_battery", {"level": 88}, "android_agent")
    
    assert len(broadcast_calls) == 1
    payload = broadcast_calls[0]
    assert payload["event"] == "context.update"
    assert payload["key"] == "current_battery"
    assert payload["value"]["level"] == 88

def test_context_conflicts_endpoint(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nayakacode")
    client = TestClient(app)

    response = client.post("/auth/token", json={"username": "admin", "password": "nayakacode"})
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    response = client.get("/context/conflicts", headers=headers)
    assert response.status_code == 200
    assert isinstance(response.json(), list)
