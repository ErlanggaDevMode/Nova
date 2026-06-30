import os
import json
from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from nova_core.models import RoutedResult, ActionRequest

router = APIRouter()

class CommandSubmission(BaseModel):
    raw_text: str
    source_device_id: str

@router.post("/command")
async def submit_command(payload: CommandSubmission, request: Request):
    app = request.app
    store = app.state.store
    registry = app.state.registry
    router_engine = app.state.router
    manager = app.state.manager

    command_id = os.urandom(8).hex()

    # 1. Route the command (local vs cloud)
    routed: RoutedResult = router_engine.route_command(payload.raw_text, payload.source_device_id)
    
    # Log the command in the DB
    store.log_command(command_id, payload.raw_text, payload.source_device_id, routed.path)

    if not routed.action_request:
        # Just text response from LLM
        return {
            "success": True,
            "response_text": routed.response_text or "No response from assistant.",
            "action_request": None
        }

    # 2. Check permission registry
    action = routed.action_request
    decision = registry.check(action)
    action_id = os.urandom(8).hex()

    decision_dict = {
        "allowed": decision.allowed,
        "requires_confirmation": decision.requires_confirmation,
        "reason": decision.reason
    }

    # Log initial action record
    store.log_action(
        action_id=action_id,
        command_id=command_id,
        action_type=action.action_type,
        category=action.category,
        params=action.params,
        permission_decision=decision_dict,
        executed=False
    )

    if not decision.allowed:
        return {
            "success": False,
            "error": f"Permission denied: {decision.reason}",
            "action_id": action_id
        }

    if decision.requires_confirmation:
        return {
            "success": True,
            "requires_confirmation": True,
            "action_id": action_id,
            "action": action.model_dump(),
            "reason": decision.reason
        }

    # 3. Allowed, no confirmation -> execute immediately via WebSocket
    result = await manager.send_action_request(payload.source_device_id, action_id, action)
    executed = result.get("success", False)
    store.update_action_result(action_id, executed, result)

    return {
        "success": executed,
        "action_id": action_id,
        "action_type": action.action_type,
        "category": action.category,
        "result": result
    }

@router.post("/command/{action_id}/confirm")
async def confirm_command(action_id: str, request: Request):
    app = request.app
    store = app.state.store
    manager = app.state.manager

    sql = "SELECT * FROM actions WHERE id = ?"
    with store.get_connection() as conn:
        row = conn.execute(sql, (action_id,)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="Action not found.")
        row_dict = dict(row)

    params = json.loads(row_dict["params"])
    
    command_id = row_dict["command_id"]
    sql_cmd = "SELECT source_device_id FROM commands WHERE id = ?"
    with store.get_connection() as conn:
        cmd_row = conn.execute(sql_cmd, (command_id,)).fetchone()
        if not cmd_row:
            raise HTTPException(status_code=404, detail="Command associated with action not found.")
        device_id = cmd_row["source_device_id"]

    action = ActionRequest(
        action_type=row_dict["action_type"],
        category=row_dict["category"],
        params=params,
        source_device_id=device_id,
        origin="cloud_llm"
    )

    # Dispatch via connection manager
    result = await manager.send_action_request(device_id, action_id, action)
    executed = result.get("success", False)
    store.update_action_result(action_id, executed, result)

    return {
        "success": executed,
        "action_id": action_id,
        "action_type": action.action_type,
        "category": action.category,
        "result": result
    }
