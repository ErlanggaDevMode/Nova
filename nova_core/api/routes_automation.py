from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

class RuleCreation(BaseModel):
    name: str
    condition: dict
    action_template: dict
    enabled: Optional[int] = 1

class RuleUpdate(BaseModel):
    name: Optional[str] = None
    condition: Optional[dict] = None
    action_template: Optional[dict] = None
    enabled: Optional[int] = None

@router.get("/automation/rules")
async def list_rules(request: Request):
    rules_store = request.app.state.rules_store
    return {"success": True, "rules": rules_store.list_rules()}

@router.post("/automation/rules")
async def create_rule(payload: RuleCreation, request: Request):
    rules_store = request.app.state.rules_store
    try:
        from nova_core.automation.conditions import parse_condition
        parse_condition(payload.condition)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid condition schema: {str(e)}")

    rule_id = rules_store.create_rule(
        name=payload.name,
        condition=payload.condition,
        action_template=payload.action_template,
        enabled=payload.enabled
    )
    return {"success": True, "rule_id": rule_id}

@router.patch("/automation/rules/{rule_id}")
async def update_rule(rule_id: str, payload: RuleUpdate, request: Request):
    rules_store = request.app.state.rules_store
    rule = rules_store.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found.")

    if payload.condition is not None:
        try:
            from nova_core.automation.conditions import parse_condition
            parse_condition(payload.condition)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid condition schema: {str(e)}")

    rules_store.update_rule(
        rule_id=rule_id,
        name=payload.name,
        condition=payload.condition,
        action_template=payload.action_template,
        enabled=payload.enabled
    )
    return {"success": True, "rule_id": rule_id}

@router.delete("/automation/rules/{rule_id}")
async def delete_rule(rule_id: str, request: Request):
    rules_store = request.app.state.rules_store
    rule = rules_store.get_rule(rule_id)
    if not rule:
        raise HTTPException(status_code=404, detail="Rule not found.")
    rules_store.delete_rule(rule_id)
    return {"success": True, "message": f"Rule '{rule_id}' deleted successfully."}
