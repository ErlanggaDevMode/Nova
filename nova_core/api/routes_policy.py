from fastapi import APIRouter, Request, HTTPException
import yaml

router = APIRouter()

@router.get("/policy")
async def get_policy(request: Request):
    registry = request.app.state.registry
    return registry.policy

@router.patch("/policy")
async def update_policy(payload: dict, request: Request):
    registry = request.app.state.registry
    try:
        with open(registry.policy_path, "w", encoding="utf-8") as f:
            yaml.dump(payload, f)
        registry.reload()
        return {"success": True, "policy": registry.policy}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to update policy: {str(e)}")
