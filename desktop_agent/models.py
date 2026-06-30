from typing import Literal
from pydantic import BaseModel

class ActionRequest(BaseModel):
    action_type: str
    category: str          # matches policy.yaml category keys
    params: dict
    source_device_id: str = "desktop"
    origin: Literal["local_match", "cloud_llm"] = "local_match"

class PermissionDecision(BaseModel):
    allowed: bool
    requires_confirmation: bool
    reason: str | None = None
