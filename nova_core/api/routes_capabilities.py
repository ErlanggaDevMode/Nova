from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Literal

router = APIRouter()

class CapabilitiesRegistration(BaseModel):
    name: str
    platform: Literal["desktop", "android", "web"]
    capabilities: dict

@router.get("/capabilities/{device_id}")
async def get_capabilities(device_id: str, request: Request):
    store = request.app.state.store
    device = store.get_device(device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found.")
    return device

@router.post("/capabilities/{device_id}")
async def register_capabilities(device_id: str, payload: CapabilitiesRegistration, request: Request):
    store = request.app.state.store
    store.register_device(device_id, payload.name, payload.platform, payload.capabilities)
    return {"success": True, "message": f"Device '{device_id}' registered successfully."}
