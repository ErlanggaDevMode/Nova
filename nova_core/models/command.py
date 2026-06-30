from pydantic import BaseModel
from typing import Literal
from .action import ActionRequest

class IntentMatch(BaseModel):
    action_type: str
    category: str
    params: dict
    confidence: float = 1.0

class RoutedResult(BaseModel):
    path: Literal["local", "cloud"]
    intent: IntentMatch | None = None
    action_request: ActionRequest | None = None
    response_text: str | None = None
