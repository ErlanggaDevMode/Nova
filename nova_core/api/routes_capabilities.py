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

@router.get("/capabilities")
async def list_capabilities(request: Request):
    store = request.app.state.store
    manager = request.app.state.manager
    sql = "SELECT id, name, platform, last_seen_at FROM devices ORDER BY last_seen_at DESC"
    with store.get_connection() as conn:
        rows = conn.execute(sql).fetchall()
    
    connected_ids = list(manager.active_connections.keys())
    
    devices = []
    for r in rows:
        d = dict(r)
        d["online"] = d["id"] in connected_ids
        devices.append(d)
    return {"devices": devices}

