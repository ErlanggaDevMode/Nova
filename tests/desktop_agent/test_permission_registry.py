import yaml
from pathlib import Path
import pytest
from desktop_agent.models import ActionRequest
from desktop_agent.permission_registry import PermissionRegistry

@pytest.fixture
def test_policy_path(tmp_path):
    policy_content = {
        "categories": {
            "app_control": {
                "confirmation": "none",
                "whitelist": [
                    {"name": "notepad", "executable": "notepad.exe"},
                    {"name": "calc", "executable": "calc.exe"}
                ],
                "default_confirmation": "confirm"
            },
            "read_only_info": {
                "confirmation": "none",
                "actions": ["get_battery"]
            },
            "shell_command": {
                "confirmation": "none", # Attempt to disable confirmation for test
                "actions": ["run_script"]
            }
        }
    }
    path = tmp_path / "test_policy.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
    return path

def test_whitelisted_app(test_policy_path):
    registry = PermissionRegistry(test_policy_path)
    
    # Whitelisted app name
    action = ActionRequest(
        action_type="open_app",
        category="app_control",
        params={"app_name": "notepad"}
    )
    decision = registry.check(action)
    assert decision.allowed is True
    assert decision.requires_confirmation is False

    # Whitelisted executable
    action_exec = ActionRequest(
        action_type="open_app",
        category="app_control",
        params={"executable": "calc.exe"}
    )
    decision_exec = registry.check(action_exec)
    assert decision_exec.allowed is True
    assert decision_exec.requires_confirmation is False

def test_non_whitelisted_app(test_policy_path):
    registry = PermissionRegistry(test_policy_path)
    
    action = ActionRequest(
        action_type="open_app",
        category="app_control",
        params={"app_name": "unknown_app"}
    )
    decision = registry.check(action)
    assert decision.allowed is True
    assert decision.requires_confirmation is True # Fallback to default_confirmation: confirm

def test_shell_command_security_floor(test_policy_path):
    registry = PermissionRegistry(test_policy_path)
    
    action = ActionRequest(
        action_type="run_script",
        category="shell_command",
        params={"script": "format C:"}
    )
    decision = registry.check(action)
    assert decision.allowed is True
    # Non-negotiable security floor check: requires_confirmation must be True
    # even though our test policy set confirmation to "none".
    assert decision.requires_confirmation is True

def test_unknown_category(test_policy_path):
    registry = PermissionRegistry(test_policy_path)
    
    action = ActionRequest(
        action_type="some_action",
        category="invalid_category",
        params={}
    )
    decision = registry.check(action)
    assert decision.allowed is False

def test_file_system_permissions(tmp_path):
    policy_content = {
        "categories": {
            "file_system": {
                "read": {
                    "confirmation": "none",
                    "allowed_paths": ["~/Documents", "~/Downloads"]
                },
                "write": {
                    "confirmation": "confirm",
                    "allowed_paths": ["~/Documents/nova-workspace"]
                },
                "delete": {
                    "confirmation": "confirm",
                    "allowed_paths": ["~/Documents/nova-workspace"]
                }
            }
        }
    }
    policy_file = tmp_path / "file_system_policy.yaml"
    with open(policy_file, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
        
    registry = PermissionRegistry(policy_file)
    
    # 1. Read inside permitted path
    action = ActionRequest(
        action_type="read_file",
        category="file_system",
        params={"path": "~/Documents/notes.txt"}
    )
    decision = registry.check(action)
    assert decision.allowed is True
    assert decision.requires_confirmation is False
    
    # 2. Read outside permitted path
    action = ActionRequest(
        action_type="read_file",
        category="file_system",
        params={"path": "/etc/passwd"}
    )
    decision = registry.check(action)
    assert decision.allowed is False
    
    # 3. Write inside permitted path
    action = ActionRequest(
        action_type="write_file",
        category="file_system",
        params={"path": "~/Documents/nova-workspace/todo.txt"}
    )
    decision = registry.check(action)
    assert decision.allowed is True
    assert decision.requires_confirmation is True
    
    # 4. Write outside permitted path
    action = ActionRequest(
        action_type="write_file",
        category="file_system",
        params={"path": "~/Documents/todo.txt"}
    )
    decision = registry.check(action)
    assert decision.allowed is False

