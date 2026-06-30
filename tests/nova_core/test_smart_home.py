import pytest
import os
from unittest.mock import patch, MagicMock
from nova_core.integrations.smart_home.tuya import TuyaClient

def test_tuya_stub_execution():
    client = TuyaClient()
    res = client.send_device_command("light_1", "switch_led", True)
    assert res["success"] is True
    assert res["stub"] is True
    assert res["command"] == "switch_led"
    assert res["value"] is True

@patch("requests.post")
@patch("requests.get")
def test_tuya_real_execution(mock_get, mock_post, monkeypatch):
    monkeypatch.setenv("TUYA_CLIENT_ID", "test_client")
    monkeypatch.setenv("TUYA_CLIENT_SECRET", "test_secret")

    mock_get.return_value.status_code = 200
    mock_get.return_value.json = MagicMock(return_value={
        "success": True,
        "result": {"access_token": "mock_token"}
    })

    mock_post.return_value.status_code = 200
    mock_post.return_value.json = MagicMock(return_value={
        "success": True,
        "result": {"status": "ok"}
    })

    client = TuyaClient()
    res = client.send_device_command("light_1", "switch_led", True)
    assert res["success"] is True
    assert res["result"]["status"] == "ok"
