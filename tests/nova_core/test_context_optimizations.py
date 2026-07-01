import pytest
import time
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from nova_core.main import app
from nova_core.db.store import DatabaseStore
from nova_core.context.context_store import ContextStore

def test_context_list_truncation(tmp_path):
    db_file = tmp_path / "test_context.db"
    store = DatabaseStore(db_path=db_file)
    context_store = ContextStore(store)
    
    # Verify that listing inputs containing 10 elements gets truncated to the last 5
    raw_list = list(range(10))
    context_store.set_state("history", {"logs": raw_list}, "test_device")
    
    val = context_store.get_state("history")
    assert len(val["logs"]) == 5
    assert val["logs"] == [5, 6, 7, 8, 9]

def test_context_sliding_window_pruning(tmp_path):
    db_file = tmp_path / "test_context_prune.db"
    store = DatabaseStore(db_path=db_file)
    context_store = ContextStore(store)
    
    # 1. Add high priority state
    context_store.set_state("active_task", {"task": "Build Nova"}, "test_device")
    
    # 2. Add very large normal priority states that exceed the 1500 character formatting limit
    large_payload = {f"key_{i}": "value_large_string_etc" for i in range(100)}
    context_store.set_state("running_apps", large_payload, "test_device")
    
    # The set_state action triggers pruning. It should see that total size > 1500,
    # and drop the normal priority key 'running_apps' while leaving 'active_task' intact.
    val_apps = context_store.get_state("running_apps")
    assert val_apps is None
    
    val_task = context_store.get_state("active_task")
    assert val_task is not None
    assert val_task["task"] == "Build Nova"

def test_context_time_expiry(tmp_path):
    db_file = tmp_path / "test_context_expiry.db"
    store = DatabaseStore(db_path=db_file)
    context_store = ContextStore(store)
    
    # Mock set_state with backdated updated_at
    sql = """
    INSERT INTO context_state (key, value, updated_by_device_id, updated_at)
    VALUES (?, ?, ?, ?)
    """
    # Create normal key expired (created 40 mins ago -> TTL 30 mins/1800s)
    expired_time = (datetime.now(timezone.utc) - timedelta(minutes=40)).isoformat()
    with store.get_connection() as conn:
        conn.execute(sql, ("history", '{"logs": []}', "test_device", expired_time))
        
    # Active key (created 5 mins ago -> TTL 30 mins)
    active_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
    with store.get_connection() as conn:
        conn.execute(sql, ("running_apps", '{"apps": []}', "test_device", active_time))
        
    # Query context: this triggers the _expire_stale_states check automatically
    res = context_store.get_state("running_apps")
    assert res is not None
    
    expired = context_store.get_state("history")
    assert expired is None

def test_context_dump_endpoint(monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD", "nayakacode")
    client = TestClient(app)

    response = client.post("/auth/token", json={"username": "admin", "password": "nayakacode"})
    token = response.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    response = client.get("/context/dump", headers=headers)
    assert response.status_code == 200
    data = response.json()
    assert "active_keys_count" in data
    assert "formatted_size_chars" in data
    assert data["budget_limit_chars"] == 1500
