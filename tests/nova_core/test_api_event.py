import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock
from nova_core.main import app

def test_post_event_triggers_automation():
    client = TestClient(app)
    
    # Mock evaluate_and_fire method
    app.state.automation.evaluate_and_fire = AsyncMock()

    payload = {
        "type": "notification",
        "source": "com.whatsapp",
        "text": "Message from John: Hey there!"
    }
    
    response = client.post("/event", json=payload)
    assert response.status_code == 200
    assert response.json()["success"] is True

    app.state.automation.evaluate_and_fire.assert_called_once_with({
        "type": "notification",
        "source": "com.whatsapp",
        "text": "Message from John: Hey there!",
        "temperature": None,
        "condition": None
    })
