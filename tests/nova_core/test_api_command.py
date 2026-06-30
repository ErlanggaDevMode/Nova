import pytest
from fastapi.testclient import TestClient
from nova_core.main import app
from nova_core.db.store import DatabaseStore

client = TestClient(app)

def test_api_capabilities():
    # Register capabilities
    res = client.post("/capabilities/test_device", json={
        "name": "Test CLI Client",
        "platform": "desktop",
        "capabilities": {"open_app": True}
    })
    assert res.status_code == 200
    assert res.json()["success"] is True
    
    # Retrieve capabilities
    res_get = client.get("/capabilities/test_device")
    assert res_get.status_code == 200
    assert res_get.json()["name"] == "Test CLI Client"

def test_api_policy():
    res = client.get("/policy")
    assert res.status_code == 200
    assert "categories" in res.json()

def test_api_command_conversational():
    res = client.post("/command", json={
        "raw_text": "hello assistant",
        "source_device_id": "test_device"
    })
    assert res.status_code == 200
    res_data = res.json()
    assert res_data["success"] is True
    assert "response_text" in res_data
    assert res_data["action_request"] is None
