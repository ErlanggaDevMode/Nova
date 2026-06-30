import yaml
from pathlib import Path
import pytest
from nova_core.models import ActionRequest
from nova_core.permission_registry import PermissionRegistry

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
            },
            "read_only_info": {
                "confirmation": "none",
                "actions": ["get_battery"]
            },
            "shell_command": {
                "confirmation": "none",
                "actions": ["run_script"]
            }
        }
    }
    path = tmp_path / "test_policy.yaml"
    with open(path, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)
    return path

def test_registry_enforces_rules(test_policy_path):
    registry = PermissionRegistry(test_policy_path)
    
    action1 = ActionRequest(action_type="open_app", category="app_control", params={"app_name": "notepad"}, source_device_id="dev", origin="local_match")
    assert registry.check(action1).allowed is True
    assert registry.check(action1).requires_confirmation is False
    
    action2 = ActionRequest(action_type="open_app", category="app_control", params={"app_name": "unknown"}, source_device_id="dev", origin="local_match")
    assert registry.check(action2).allowed is True
    assert registry.check(action2).requires_confirmation is True
    
    action3 = ActionRequest(action_type="run_script", category="shell_command", params={"command": "dir"}, source_device_id="dev", origin="local_match")
    assert registry.check(action3).allowed is True
    assert registry.check(action3).requires_confirmation is True
