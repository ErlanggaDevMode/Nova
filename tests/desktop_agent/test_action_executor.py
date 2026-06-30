import pytest
from unittest.mock import MagicMock
from pathlib import Path
import yaml

from desktop_agent.models import ActionRequest, PermissionDecision
from desktop_agent.permission_registry import PermissionRegistry
from desktop_agent.logger import NovaLogger
from desktop_agent.action_executor import ActionExecutor

@pytest.fixture
def mock_logger():
    logger = MagicMock(spec=NovaLogger)
    return logger

@pytest.fixture
def test_policy_path(tmp_path):
    policy_content = {
        "categories": {
            "app_control": {
                "confirmation": "none",
                "whitelist": [
                    {"name": "notepad", "executable": "notepad.exe"}
                ],
                "default_confirmation": "confirm"
            }
        }
    }
    path = tmp_path / "test_policy.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
    return path

def test_execute_whitelisted_app(test_policy_path, mock_logger):
    registry = PermissionRegistry(test_policy_path)
    
    # Mock confirmation callback to ensure it's not even called
    confirm_cb = MagicMock(return_value=True)
    
    mock_platform = MagicMock()
    mock_platform.open_application.return_value = True
    
    executor = ActionExecutor(registry, mock_logger, platform_adapter=mock_platform, confirm_callback=confirm_cb)
    
    action = ActionRequest(
        action_type="open_app",
        category="app_control",
        params={"app_name": "notepad"}
    )
    
    res = executor.execute(action)
    
    assert res["success"] is True
    executor.platform_adapter.open_application.assert_called_once_with("notepad.exe")
    confirm_cb.assert_not_called()
    mock_logger.log_action.assert_called_once()

def test_execute_non_whitelisted_app_confirmed(test_policy_path, mock_logger):
    registry = PermissionRegistry(test_policy_path)
    confirm_cb = MagicMock(return_value=True)
    
    mock_platform = MagicMock()
    mock_platform.open_application.return_value = True
    
    executor = ActionExecutor(registry, mock_logger, platform_adapter=mock_platform, confirm_callback=confirm_cb)
    
    action = ActionRequest(
        action_type="open_app",
        category="app_control",
        params={"app_name": "firefox"}
    )
    
    res = executor.execute(action)
    
    assert res["success"] is True
    confirm_cb.assert_called_once_with(action)
    executor.platform_adapter.open_application.assert_called_once_with("firefox")
    mock_logger.log_action.assert_called_once()

def test_execute_non_whitelisted_app_rejected(test_policy_path, mock_logger):
    registry = PermissionRegistry(test_policy_path)
    confirm_cb = MagicMock(return_value=False) # User says NO
    
    mock_platform = MagicMock()
    
    executor = ActionExecutor(registry, mock_logger, platform_adapter=mock_platform, confirm_callback=confirm_cb)
    
    action = ActionRequest(
        action_type="open_app",
        category="app_control",
        params={"app_name": "firefox"}
    )
    
    res = executor.execute(action)
    
    # Proves permission-aware execution blocks unauthorized action!
    assert res["success"] is False
    assert "rejected" in res["error"]
    confirm_cb.assert_called_once_with(action)
    executor.platform_adapter.open_application.assert_not_called()
    # Check that logger recorded the rejection
    mock_logger.log_action.assert_called_once()
    args, kwargs = mock_logger.log_action.call_args
    assert kwargs["executed"] is False
    assert "rejected" in kwargs["result"]["error"]

def test_execute_permission_denied(test_policy_path, mock_logger):
    registry = PermissionRegistry(test_policy_path)
    confirm_cb = MagicMock(return_value=True)
    
    mock_platform = MagicMock()
    
    executor = ActionExecutor(registry, mock_logger, platform_adapter=mock_platform, confirm_callback=confirm_cb)
    
    # Requesting category that doesn't exist/unsupported
    action = ActionRequest(
        action_type="send_email",
        category="communication",
        params={}
    )
    
    res = executor.execute(action)
    
    assert res["success"] is False
    assert "denied" in res["error"]
    confirm_cb.assert_not_called()
    executor.platform_adapter.open_application.assert_not_called()
    mock_logger.log_action.assert_called_once()

def test_execute_system_info(mock_logger, tmp_path):
    policy_content = {
        "categories": {
            "read_only_info": {
                "confirmation": "none",
                "actions": ["get_battery", "list_running_apps", "get_system_info"]
            }
        }
    }
    policy_file = tmp_path / "sys_policy.yaml"
    with open(policy_file, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
        
    registry = PermissionRegistry(policy_file)
    mock_platform = MagicMock()
    mock_platform.get_battery_info.return_value = {"available": True, "percent": 85, "power_plugged": True}
    mock_platform.list_running_apps.return_value = [{"name": "code", "pid": 1234}]
    mock_platform.get_system_metrics.return_value = {"cpu_percent": 15.0, "ram_percent": 45.0}
    
    executor = ActionExecutor(registry, mock_logger, platform_adapter=mock_platform)
    
    action1 = ActionRequest(action_type="get_battery", category="read_only_info", params={})
    res1 = executor.execute(action1)
    assert res1["success"] is True
    assert res1["battery"]["percent"] == 85
    mock_platform.get_battery_info.assert_called_once()
    
    action2 = ActionRequest(action_type="list_running_apps", category="read_only_info", params={})
    res2 = executor.execute(action2)
    assert res2["success"] is True
    assert len(res2["running_apps"]) == 1
    mock_platform.list_running_apps.assert_called_once()
    
    action3 = ActionRequest(action_type="get_system_info", category="read_only_info", params={})
    res3 = executor.execute(action3)
    assert res3["success"] is True
    assert res3["system_metrics"]["cpu_percent"] == 15.0

def test_execute_file_ops(mock_logger, tmp_path):
    permitted_dir = tmp_path / "sandbox"
    permitted_dir.mkdir()
    
    policy_content = {
        "categories": {
            "file_system": {
                "read": {
                    "confirmation": "none",
                    "allowed_paths": [str(permitted_dir)]
                },
                "write": {
                    "confirmation": "confirm",
                    "allowed_paths": [str(permitted_dir)]
                },
                "delete": {
                    "confirmation": "confirm",
                    "allowed_paths": [str(permitted_dir)]
                }
            }
        }
    }
    policy_file = tmp_path / "file_policy.yaml"
    with open(policy_file, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
        
    registry = PermissionRegistry(policy_file)
    confirm_cb = MagicMock(return_value=True)
    
    executor = ActionExecutor(registry, mock_logger, confirm_callback=confirm_cb)
    
    target_file = permitted_dir / "test.txt"
    
    action_write = ActionRequest(
        action_type="write_file",
        category="file_system",
        params={"path": str(target_file), "content": "hello executor"}
    )
    res_write = executor.execute(action_write)
    assert res_write["success"] is True
    assert target_file.read_text() == "hello executor"
    confirm_cb.assert_called_once_with(action_write)
    
    confirm_cb.reset_mock()
    
    action_read = ActionRequest(
        action_type="read_file",
        category="file_system",
        params={"path": str(target_file)}
    )
    res_read = executor.execute(action_read)
    assert res_read["success"] is True
    assert res_read["content"] == "hello executor"
    confirm_cb.assert_not_called()
    
    action_search = ActionRequest(
        action_type="search_files",
        category="file_system",
        params={"query": "test", "search_path": str(permitted_dir)}
    )
    res_search = executor.execute(action_search)
    assert res_search["success"] is True
    assert any(str(target_file) in f for f in res_search["files"])
    
    action_delete = ActionRequest(
        action_type="delete_file",
        category="file_system",
        params={"path": str(target_file)}
    )
    res_delete = executor.execute(action_delete)
    assert res_delete["success"] is True
    assert not target_file.exists()
    confirm_cb.assert_called_once_with(action_delete)

def test_execute_shell_command(mock_logger, tmp_path):
    policy_content = {
        "categories": {
            "shell_command": {
                "confirmation": "none",
                "actions": ["run_script"]
            }
        }
    }
    policy_file = tmp_path / "shell_policy.yaml"
    with open(policy_file, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
        
    registry = PermissionRegistry(policy_file)
    confirm_cb = MagicMock(return_value=True)
    
    executor = ActionExecutor(registry, mock_logger, confirm_callback=confirm_cb)
    
    action = ActionRequest(
        action_type="run_script",
        category="shell_command",
        params={"command": "echo hello_nova"}
    )
    res = executor.execute(action)
    
    assert res["success"] is True
    assert "hello_nova" in res["stdout"]
    confirm_cb.assert_called_once_with(action)

