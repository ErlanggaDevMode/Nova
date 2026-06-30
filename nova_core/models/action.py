from pydantic import BaseModel
from typing import Literal

class ActionRequest(BaseModel):
    action_type: str
    category: str          # matches policy.yaml category keys
    params: dict
    source_device_id: str
    origin: Literal["local_match", "cloud_llm"]

class PermissionDecision(BaseModel):
    allowed: bool
    requires_confirmation: bool
    reason: str | None = None
