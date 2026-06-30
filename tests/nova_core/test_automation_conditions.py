import pytest
from nova_core.automation.conditions import parse_condition

def test_time_condition():
    cond1 = parse_condition({"type": "time", "interval_minutes": 5})
    assert cond1.matches({"type": "time", "tick_counter": 10}) is True
    assert cond1.matches({"type": "time", "tick_counter": 11}) is False
    assert cond1.matches({"type": "weather", "tick_counter": 10}) is False

    cond2 = parse_condition({"type": "time", "time_of_day": "08:30"})
    assert cond2.matches({"type": "time", "time_of_day": "08:30"}) is True
    assert cond2.matches({"type": "time", "time_of_day": "09:00"}) is False

def test_notification_condition():
    cond = parse_condition({"type": "notification", "source": "WhatsApp", "contains": "hello"})
    
    assert cond.matches({"type": "notification", "source": "WhatsApp", "text": "Hello there"}) is True
    assert cond.matches({"type": "notification", "source": "SMS", "text": "Hello there"}) is False
    assert cond.matches({"type": "notification", "source": "WhatsApp", "text": "Goodbye"}) is False

def test_weather_condition():
    cond = parse_condition({"type": "weather", "temp_above": 30.0, "condition_is": "Rain"})
    
    assert cond.matches({"type": "weather", "temperature": 32.5, "condition": "Rain"}) is True
    assert cond.matches({"type": "weather", "temperature": 28.0, "condition": "Rain"}) is False
    assert cond.matches({"type": "weather", "temperature": 35.0, "condition": "Sunny"}) is False
