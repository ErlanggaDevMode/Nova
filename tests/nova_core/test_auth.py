import pytest
import os
from fastapi.testclient import TestClient
from fastapi.websockets import WebSocketDisconnect
from unittest.mock import MagicMock
from nova_core.main import app
from nova_core.models import RoutedResult

def test_unauthorized_endpoints():
    # Lock down password for auth test
    os.environ["ADMIN_PASSWORD"] = "nayakacode"
    client = TestClient(app)
    response = client.post("/command", json={"raw_text": "hello", "source_device_id": "test"})
    assert response.status_code == 401

def test_login_and_authorized_endpoint():
    os.environ["ADMIN_PASSWORD"] = "nayakacode"
    client = TestClient(app)
    
    response = client.post("/auth/token", json={"username": "admin", "password": "wrongpassword"})
    assert response.status_code == 401

    response = client.post("/auth/token", json={"username": "admin", "password": "nayakacode"})
    assert response.status_code == 200
    token = response.json()["access_token"]
    assert token is not None

    headers = {"Authorization": f"Bearer {token}"}
    
    app.state.router.route_command = MagicMock(return_value=RoutedResult(
        path="local",
        action_request=None,
        response_text="Mock localized response"
    ))
    
    response = client.post("/command", json={"raw_text": "hello", "source_device_id": "test"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["response_text"] == "Mock localized response"

def test_websocket_auth():
    os.environ["ADMIN_PASSWORD"] = "nayakacode"
    client = TestClient(app)
    
    # 1. Connect without token (should trigger WebSocketDisconnect on receive)
    with pytest.raises(WebSocketDisconnect) as excinfo:
        with client.websocket_connect("/ws/test_client") as ws:
            ws.receive_json()
    assert excinfo.value.code == 4001
            
    # 2. Connect with token
    response = client.post("/auth/token", json={"username": "admin", "password": "nayakacode"})
    token = response.json()["access_token"]
    
    with client.websocket_connect(f"/ws/test_client?token={token}") as ws:
        # WebSocket connection successfully opens and remains open
        pass

    # Clear environment variables at end of tests to keep local dev open
    if "ADMIN_PASSWORD" in os.environ:
        del os.environ["ADMIN_PASSWORD"]
