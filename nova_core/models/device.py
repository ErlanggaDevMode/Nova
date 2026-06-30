from pydantic import BaseModel
from typing import Literal

class DeviceRegistration(BaseModel):
    device_id: str
    name: str
    platform: Literal["desktop", "android", "web"]
    capabilities: dict
