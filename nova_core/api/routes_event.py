from fastapi import APIRouter, Request, HTTPException
from pydantic import BaseModel
from typing import Optional

router = APIRouter()

class EventPayload(BaseModel):
    type: str
    source: str
    text: str
    temperature: Optional[float] = None
    condition: Optional[str] = None

@router.post("/event")
async def post_event(payload: EventPayload, request: Request):
    engine = request.app.state.automation
    try:
        event_dict = payload.model_dump()
        await engine.evaluate_and_fire(event_dict)
        return {"success": True, "message": "Event processed successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
