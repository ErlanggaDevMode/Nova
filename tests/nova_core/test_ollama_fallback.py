import pytest
from unittest.mock import patch, MagicMock
from nova_core.router.llm_client import LLMClient

@patch("openai.resources.chat.completions.Completions.create")
def test_ollama_query_execution(mock_create, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LOCAL_LLM_URL", "http://localhost:11434/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "llama3")

    mock_choice = MagicMock()
    mock_choice.message.content = "Turning light on"
    
    mock_tool_call = MagicMock()
    mock_tool_call.function.name = "tuya_control_device"
    mock_tool_call.function.arguments = '{"device_id": "light_1", "command_name": "switch_led", "value": true}'
    mock_choice.message.tool_calls = [mock_tool_call]
    
    mock_create.return_value.choices = [mock_choice]

    client = LLMClient()
    action, text = client.query("turn on the light", "test_device")
    
    assert action is not None
    assert action.action_type == "tuya_control_device"
    assert action.params["value"] is True
