import os
import logging
from datetime import datetime, timezone
from typing import Optional
from nova_core.models import ActionRequest
from nova_core.db.store import DatabaseStore
from nova_core.ws.connection_manager import ConnectionManager
from nova_core.permission_registry import PermissionRegistry
from nova_core.automation.rules_store import RulesStore
from nova_core.automation.conditions import parse_condition

logger = logging.getLogger("nova.automation")

class AutomationEngine:
    def __init__(self, store: DatabaseStore, rules_store: RulesStore, registry: PermissionRegistry, manager: ConnectionManager):
        self.store = store
        self.rules_store = rules_store
        self.registry = registry
        self.manager = manager

    async def evaluate_and_fire(self, event: dict) -> None:
        """
        Evaluates all enabled automation rules against a system event.
        If a rule matches, evaluates permissions and executes or triggers confirmation.
        """
        rules = self.rules_store.list_rules()
        enabled_rules = [r for r in rules if r.get("enabled", 1) == 1]
        
        for rule in enabled_rules:
            try:
                condition_data = rule["condition"]
                cond = parse_condition(condition_data)
                
                if cond.matches(event):
                    logger.info(f"Automation trigger: rule '{rule['name']}' matched event {event.get('type')}.")
                    await self._fire_rule(rule, event)
            except Exception as e:
                logger.error(f"Failed to evaluate rule '{rule.get('name')}': {e}")

    async def _fire_rule(self, rule: dict, event: dict) -> None:
        template = rule["action_template"]
        
        # Instantiate parameters, handling simple {event.field} placeholder substitutions
        params = dict(template.get("params", {}))
        for k, v in list(params.items()):
            if isinstance(v, str) and v.startswith("{event.") and v.endswith("}"):
                field = v[7:-1]
                params[k] = event.get(field, "")

        device_id = template.get("source_device_id", "desktop_agent_client")

        action_req = ActionRequest(
            action_type=template["action_type"],
            category=template["category"],
            params=params,
            source_device_id=device_id,
            origin="cloud_llm"
        )

        # 1. Enforce Registry Check
        decision = self.registry.check(action_req)
        action_id = os.urandom(8).hex()

        decision_dict = {
            "allowed": decision.allowed,
            "requires_confirmation": decision.requires_confirmation,
            "reason": decision.reason
        }

        # Log initial action execution
        self.store.log_action(
            action_id=action_id,
            command_id=None,  # Automations do not have a root user command
            action_type=action_req.action_type,
            category=action_req.category,
            params=action_req.params,
            permission_decision=decision_dict,
            executed=False
        )

        if not decision.allowed:
            logger.warning(f"Automation rule '{rule['name']}' blocked: {decision.reason}")
            return

        # 2. Gate with confirmation if required
        if decision.requires_confirmation:
            logger.info(f"Automation rule '{rule['name']}' requires user confirmation. Queueing event.")
            
            # Send confirmation.requested notification over WebSocket
            if device_id in self.manager.active_connections:
                payload = {
                    "event": "confirmation.requested",
                    "action_id": action_id,
                    "action": {
                        "action_type": action_req.action_type,
                        "category": action_req.category,
                        "params": action_req.params
                    },
                    "reason": decision.reason
                }
                try:
                    await self.manager.active_connections[device_id].send_json(payload)
                except Exception as e:
                    logger.error(f"Failed to send confirmation request to '{device_id}': {e}")
            return

        # 3. Direct Execution
        logger.info(f"Executing automation rule '{rule['name']}' on device '{device_id}'...")
        result = await self.manager.send_action_request(device_id, action_id, action_req)
        executed = result.get("success", False)
        self.store.update_action_result(action_id, executed, result)
