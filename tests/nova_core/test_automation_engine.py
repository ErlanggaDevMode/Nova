import pytest
import yaml
from unittest.mock import AsyncMock, MagicMock
from fastapi import WebSocket
from nova_core.db.store import DatabaseStore
from nova_core.permission_registry import PermissionRegistry
from nova_core.ws.connection_manager import ConnectionManager
from nova_core.automation.rules_store import RulesStore
from nova_core.automation.engine import AutomationEngine

@pytest.mark.anyio
async def test_automation_engine_execution(tmp_path):
    db_file = tmp_path / "test_auto.db"
    store = DatabaseStore(db_path=db_file)
    rules_store = RulesStore(store)
    
    # Configure custom test policy
    policy_path = tmp_path / "test_policy.yaml"
    policy_content = {
        "categories": {
            "read_only_info": {
                "confirmation": "none",
                "actions": ["get_battery"]
            },
            "shell_command": {
                "confirmation": "always_explicit",
                "actions": ["run_script"]
            }
        }
    }
    with open(policy_path, "w", encoding="utf-8") as f:
        yaml.dump(policy_content, f)

    registry = PermissionRegistry(policy_path=policy_path)
    manager = ConnectionManager()
    engine = AutomationEngine(store, rules_store, registry, manager)

    # Register mock client
    ws_client = AsyncMock(spec=WebSocket)
    await manager.connect("desktop_client_1", ws_client)
    store.register_device("desktop_client_1", "Desktop", "desktop", {})

    # Create automation rule
    rules_store.create_rule(
        name="Auto Battery Query",
        condition={"type": "time", "interval_minutes": 2},
        action_template={
            "action_type": "get_battery",
            "category": "read_only_info",
            "params": {},
            "source_device_id": "desktop_client_1",
            "origin": "cloud_llm"
        }
    )

    # Evaluate tick 1 (should not fire)
    await engine.evaluate_and_fire({"type": "time", "tick_counter": 1})
    ws_client.send_json.assert_not_called()

    # Mock manager dispatch
    manager.send_action_request = AsyncMock(return_value={"success": True, "battery": {"percent": 95, "power_plugged": True}})
    
    # Evaluate tick 2 (should fire)
    await engine.evaluate_and_fire({"type": "time", "tick_counter": 2})
    manager.send_action_request.assert_called_once()
