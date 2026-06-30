from pydantic import BaseModel
from typing import Literal, Optional

class AutomationCondition(BaseModel):
    type: Literal["time", "notification", "weather"]
    
    def matches(self, event: dict) -> bool:
        return False

class TimeCondition(AutomationCondition):
    type: Literal["time"] = "time"
    time_of_day: Optional[str] = None      # "HH:MM"
    interval_minutes: Optional[int] = None  # trigger interval

    def matches(self, event: dict) -> bool:
        if event.get("type") != "time":
            return False
        
        if self.time_of_day:
            return event.get("time_of_day") == self.time_of_day
            
        if self.interval_minutes is not None:
            tick = event.get("tick_counter", 0)
            return tick % self.interval_minutes == 0
            
        return False

class NotificationCondition(AutomationCondition):
    type: Literal["notification"] = "notification"
    source: Optional[str] = None
    contains: Optional[str] = None

    def matches(self, event: dict) -> bool:
        if event.get("type") != "notification":
            return False
            
        if self.source and event.get("source", "").lower() != self.source.lower():
            return False
            
        if self.contains and self.contains.lower() not in event.get("text", "").lower():
            return False
            
        return True

class WeatherCondition(AutomationCondition):
    type: Literal["weather"] = "weather"
    temp_above: Optional[float] = None
    temp_below: Optional[float] = None
    condition_is: Optional[str] = None

    def matches(self, event: dict) -> bool:
        if event.get("type") != "weather":
            return False
            
        temp = event.get("temperature")
        if temp is not None:
            if self.temp_above is not None and temp <= self.temp_above:
                return False
            if self.temp_below is not None and temp >= self.temp_below:
                return False
                
        cond = event.get("condition")
        if self.condition_is and cond and self.condition_is.lower() != cond.lower():
            return False
            
        return True

def parse_condition(data: dict) -> AutomationCondition:
    cond_type = data.get("type")
    if cond_type == "time":
        return TimeCondition(**data)
    elif cond_type == "notification":
        return NotificationCondition(**data)
    elif cond_type == "weather":
        return WeatherCondition(**data)
    raise ValueError(f"Unknown condition type '{cond_type}'")
