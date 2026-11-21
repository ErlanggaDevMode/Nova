"""Weather API Integration, Threshold Alerts, Dynamic Locations & Condition Triggers (Phase 17: F17.1–F17.5).

Provides:
- F17.1: `OpenWeatherClient` with TTL caching, request timeouts, graceful error handling,
  and an injectable HTTP fetcher for offline unit tests.
- F17.2: `WeatherThresholdEvaluator` for temperature and humidity thresholds
  (`temp_above`, `temp_below`, `humidity_above`, `humidity_below`) with automatic
  metric/imperial/standard unit conversion.
- F17.3: `LocationUpdateAdapter` for per-device / per-user dynamic location updates
  with strict latitude (-90..90) and longitude (-180..180) validation.
- F17.4: `ConditionChangeEvaluator` for weather condition transition triggers
  (e.g., Clear -> Rain/Storm) with per-rule cooldown anti-spam protection.
- F17.5: `WeatherStorage` (standalone persistence via `DatabaseStore.get_connection()`
  or direct `sqlite3`) and `WeatherService` facade for REST API integration.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterator, Literal, Optional

import httpx
from pydantic import BaseModel, Field, field_validator, model_validator

_MIGRATION_0009_PATH = (
    Path(__file__).resolve().parent.parent / "db" / "migrations" / "0009_weather_config.sql"
)

WeatherUnits = Literal["metric", "imperial", "standard"]
EntityType = Literal["device", "user"]
AlertSeverity = Literal["info", "warning", "critical"]

HttpFetcher = Callable[[str, dict[str, Any], float], dict[str, Any]]


# ---------------------------------------------------------------------------
# Errors & Helpers
# ---------------------------------------------------------------------------


class WeatherClientError(Exception):
    """Base error raised by OpenWeatherClient on upstream or configuration failures."""

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.retryable = retryable


class WeatherTimeoutError(WeatherClientError):
    """Raised when an upstream weather request exceeds its configured timeout."""

    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=504, retryable=True)


class WeatherAuthError(WeatherClientError):
    """Raised when the OpenWeatherMap API key is missing or rejected."""

    def __init__(self, message: str, *, status_code: int = 401) -> None:
        super().__init__(message, status_code=status_code, retryable=False)


def mask_api_key(api_key: Optional[str]) -> str:
    """Masks a secret API key so raw credentials are never leaked in API responses."""
    if not api_key:
        return ""
    clean = api_key.strip()
    if not clean:
        return ""
    if len(clean) <= 6:
        return "*" * len(clean)
    if len(clean) <= 10:
        return f"{'*' * (len(clean) - 4)}{clean[-4:]}"
    return f"{clean[:3]}{'*' * (len(clean) - 7)}{clean[-4:]}"


def convert_temperature(value: float, from_units: str, to_units: str) -> float:
    """Converts temperature between metric (°C), imperial (°F), and standard (K)."""
    if from_units == to_units:
        return float(value)

    # First normalize to Celsius
    if from_units == "metric":
        celsius = float(value)
    elif from_units == "imperial":
        celsius = (float(value) - 32.0) * 5.0 / 9.0
    elif from_units == "standard":
        celsius = float(value) - 273.15
    else:
        raise ValueError(f"Unsupported source temperature unit: {from_units}")

    if to_units == "metric":
        return celsius
    if to_units == "imperial":
        return celsius * 9.0 / 5.0 + 32.0
    if to_units == "standard":
        return celsius + 273.15
    raise ValueError(f"Unsupported target temperature unit: {to_units}")


_CONDITION_ALIASES: dict[str, set[str]] = {
    "clear": {"clear", "sunny"},
    "clouds": {"clouds", "cloudy", "overcast"},
    "rain": {"rain", "rainy", "drizzle", "shower"},
    "drizzle": {"drizzle", "rain"},
    "storm": {"storm", "thunderstorm", "squall", "tornado"},
    "thunderstorm": {"thunderstorm", "storm", "squall"},
    "snow": {"snow", "snowy", "sleet", "blizzard"},
    "fog": {"fog", "mist", "haze", "smoke"},
    "mist": {"mist", "fog", "haze"},
}


def normalize_condition(condition: str) -> str:
    """Normalizes a weather condition string for consistent comparison."""
    return condition.strip().lower()


def condition_matches(observed: str, targets: list[str]) -> bool:
    """Returns True if observed condition matches any target (case-insensitive + aliases)."""
    if not targets:
        return True
    obs_norm = normalize_condition(observed)
    obs_group = _CONDITION_ALIASES.get(obs_norm, {obs_norm})
    for target in targets:
        t_norm = normalize_condition(target)
        t_group = _CONDITION_ALIASES.get(t_norm, {t_norm})
        if obs_norm == t_norm or obs_norm in t_group or t_norm in obs_group:
            return True
    return False


def _parse_iso_datetime(value: Optional[str | datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = value.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _validate_lat_lon_values(lat: Optional[float], lon: Optional[float]) -> None:
    if (lat is None) != (lon is None):
        raise ValueError("Both 'lat' and 'lon' must be provided together.")
    if lat is not None:
        if not math.isfinite(lat) or lat < -90.0 or lat > 90.0:
            raise ValueError(f"Latitude must be between -90 and 90, got {lat}.")
    if lon is not None:
        if not math.isfinite(lon) or lon < -180.0 or lon > 180.0:
            raise ValueError(f"Longitude must be between -180 and 180, got {lon}.")


# ---------------------------------------------------------------------------
# Domain Models
# ---------------------------------------------------------------------------


class WeatherRuleType(str, Enum):
    TEMP_ABOVE = "temp_above"
    TEMP_BELOW = "temp_below"
    HUMIDITY_ABOVE = "humidity_above"
    HUMIDITY_BELOW = "humidity_below"
    CONDITION_CHANGE = "condition_change"


class WeatherSnapshot(BaseModel):
    """Normalized weather observation from OpenWeatherMap or manual input."""

    location_name: str = Field(default="Unknown")
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    temperature: float
    feels_like: Optional[float] = Field(default=None)
    humidity: float = Field(..., ge=0.0, le=100.0)
    pressure: Optional[float] = Field(default=None)
    wind_speed: Optional[float] = Field(default=None)
    condition: str = Field(..., min_length=1)
    description: str = Field(default="")
    units: WeatherUnits = Field(default="metric")
    fetched_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    cached: bool = Field(default=False)
    raw: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _check_coords(self) -> "WeatherSnapshot":
        _validate_lat_lon_values(self.lat, self.lon)
        return self


class WeatherLocation(BaseModel):
    """Dynamic location record for a device or user (F17.3)."""

    entity_id: str = Field(default="default", min_length=1, max_length=128)
    entity_type: EntityType = Field(default="device")
    city: Optional[str] = Field(default=None, max_length=128)
    lat: Optional[float] = Field(default=None)
    lon: Optional[float] = Field(default=None)
    label: Optional[str] = Field(default=None, max_length=128)
    updated_at: Optional[str] = Field(default=None)

    @field_validator("city")
    @classmethod
    def _clean_city(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @model_validator(mode="after")
    def _validate_location(self) -> "WeatherLocation":
        _validate_lat_lon_values(self.lat, self.lon)
        if self.lat is None and self.lon is None and not self.city:
            raise ValueError("Location requires either ('lat', 'lon') coordinates or 'city'.")
        return self

    def location_key(self) -> str:
        if self.lat is not None and self.lon is not None:
            return f"entity:{self.entity_id}:coord:{self.lat:.4f},{self.lon:.4f}"
        assert self.city is not None
        return f"entity:{self.entity_id}:city:{self.city.lower()}"


class WeatherConfig(BaseModel):
    """Persisted weather integration settings (F17.5)."""

    id: str = Field(default="default")
    provider: str = Field(default="openweathermap", min_length=1, max_length=64)
    api_key: str = Field(default="")
    base_url: str = Field(
        default="https://api.openweathermap.org/data/2.5/weather",
        min_length=1,
        max_length=512,
    )
    units: WeatherUnits = Field(default="metric")
    cache_ttl_seconds: int = Field(default=300, ge=0, le=86400)
    timeout_seconds: float = Field(default=10.0, gt=0.0, le=120.0)
    default_city: Optional[str] = Field(default=None, max_length=128)
    default_lat: Optional[float] = Field(default=None)
    default_lon: Optional[float] = Field(default=None)
    enabled: bool = Field(default=True)
    updated_at: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def _check_default_coords(self) -> "WeatherConfig":
        _validate_lat_lon_values(self.default_lat, self.default_lon)
        return self

    def to_public_dict(self) -> dict[str, Any]:
        """Returns a response-safe dict with the API key masked."""
        return {
            "id": self.id,
            "provider": self.provider,
            "api_key_masked": mask_api_key(self.api_key),
            "has_api_key": bool(self.api_key and self.api_key.strip()),
            "base_url": self.base_url,
            "units": self.units,
            "cache_ttl_seconds": self.cache_ttl_seconds,
            "timeout_seconds": self.timeout_seconds,
            "default_city": self.default_city,
            "default_lat": self.default_lat,
            "default_lon": self.default_lon,
            "enabled": self.enabled,
            "updated_at": self.updated_at,
        }


class WeatherAlertRule(BaseModel):
    """Alert rule supporting threshold checks (F17.2) and condition transitions (F17.4)."""

    id: Optional[str] = Field(default=None, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    enabled: bool = Field(default=True)
    rule_type: WeatherRuleType
    threshold: Optional[float] = Field(default=None)
    units: WeatherUnits = Field(default="metric")
    from_conditions: list[str] = Field(default_factory=list)
    to_conditions: list[str] = Field(default_factory=list)
    entity_id: Optional[str] = Field(default=None, max_length=128)
    cooldown_seconds: int = Field(default=600, ge=0, le=86400 * 7)
    severity: AlertSeverity = Field(default="warning")
    description: Optional[str] = Field(default=None, max_length=512)
    last_triggered_at: Optional[str] = Field(default=None)
    created_at: Optional[str] = Field(default=None)
    updated_at: Optional[str] = Field(default=None)

    @model_validator(mode="after")
    def _validate_rule(self) -> "WeatherAlertRule":
        if self.rule_type in {
            WeatherRuleType.TEMP_ABOVE,
            WeatherRuleType.TEMP_BELOW,
            WeatherRuleType.HUMIDITY_ABOVE,
            WeatherRuleType.HUMIDITY_BELOW,
        }:
            if self.threshold is None or not math.isfinite(self.threshold):
                raise ValueError(f"Rule type '{self.rule_type.value}' requires a finite numeric threshold.")
            if self.rule_type in {WeatherRuleType.HUMIDITY_ABOVE, WeatherRuleType.HUMIDITY_BELOW}:
                if self.threshold < 0.0 or self.threshold > 100.0:
                    raise ValueError("Humidity threshold must be between 0 and 100.")
        return self


class WeatherAlertEvent(BaseModel):
    """Outcome of evaluating a single weather alert rule."""

    rule_id: str
    rule_name: str
    rule_type: WeatherRuleType
    severity: AlertSeverity
    triggered: bool
    suppressed_by_cooldown: bool = False
    current_value: Optional[float | str] = None
    previous_value: Optional[str] = None
    threshold: Optional[float] = None
    units: Optional[str] = None
    message: str
    evaluated_at: str


class WeatherEvaluationResult(BaseModel):
    """Aggregated evaluation response for a weather observation."""

    location_key: str
    snapshot: WeatherSnapshot
    previous_condition: Optional[str] = None
    condition_changed: bool = False
    evaluated_rules_count: int = 0
    triggered_alerts: list[WeatherAlertEvent] = Field(default_factory=list)
    suppressed_alerts: list[WeatherAlertEvent] = Field(default_factory=list)
    evaluated_at: str


# ---------------------------------------------------------------------------
# F17.1: OpenWeatherMap Client with TTL Caching, Timeout & Injectable Fetcher
# ---------------------------------------------------------------------------


def _default_http_fetcher(url: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
    """Default HTTP fetcher backed by httpx with graceful error translation."""
    try:
        resp = httpx.get(url, params=params, timeout=timeout)
    except httpx.TimeoutException as exc:
        raise WeatherTimeoutError(f"Weather API request timed out after {timeout}s.") from exc
    except httpx.HTTPError as exc:
        raise WeatherClientError(f"Weather API network error: {exc}", retryable=True) from exc

    if resp.status_code in (401, 403):
        raise WeatherAuthError(
            f"Weather API authentication failed (HTTP {resp.status_code}).",
            status_code=resp.status_code,
        )
    if resp.status_code == 404:
        raise WeatherClientError("Requested weather location was not found (HTTP 404).", status_code=404)
    if resp.status_code == 429:
        raise WeatherClientError("Weather API rate limit exceeded (HTTP 429).", status_code=429, retryable=True)
    if resp.status_code >= 500:
        raise WeatherClientError(
            f"Weather API server error (HTTP {resp.status_code}).",
            status_code=resp.status_code,
            retryable=True,
        )
    if resp.status_code >= 400:
        raise WeatherClientError(
            f"Weather API request failed (HTTP {resp.status_code}).",
            status_code=resp.status_code,
        )

    try:
        data = resp.json()
    except ValueError as exc:
        raise WeatherClientError("Weather API returned invalid JSON payload.") from exc

    if not isinstance(data, dict):
        raise WeatherClientError("Weather API returned unexpected non-object JSON payload.")
    return data


class OpenWeatherClient:
    """OpenWeatherMap API client with TTL cache, timeouts, and injectable HTTP fetcher."""

    DEFAULT_BASE_URL = "https://api.openweathermap.org/data/2.5/weather"

    def __init__(
        self,
        api_key: str = "",
        *,
        base_url: str = DEFAULT_BASE_URL,
        units: WeatherUnits = "metric",
        cache_ttl_seconds: int = 300,
        timeout_seconds: float = 10.0,
        fetcher: Optional[HttpFetcher] = None,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.units: WeatherUnits = units
        self.cache_ttl_seconds = max(0, int(cache_ttl_seconds))
        self.timeout_seconds = float(timeout_seconds)
        self._fetcher: HttpFetcher = fetcher or _default_http_fetcher
        self._time_fn = time_fn
        self._cache: dict[tuple[Any, ...], tuple[float, WeatherSnapshot]] = {}
        self._lock = threading.Lock()

    def clear_cache(self) -> None:
        with self._lock:
            self._cache.clear()

    def _build_cache_key(
        self,
        *,
        lat: Optional[float],
        lon: Optional[float],
        city: Optional[str],
        units: str,
    ) -> tuple[Any, ...]:
        if lat is not None and lon is not None:
            return ("coord", round(lat, 4), round(lon, 4), units)
        assert city is not None
        return ("city", city.strip().lower(), units)

    @staticmethod
    def parse_openweather_response(
        payload: dict[str, Any],
        *,
        units: WeatherUnits = "metric",
        fallback_city: Optional[str] = None,
        fallback_lat: Optional[float] = None,
        fallback_lon: Optional[float] = None,
    ) -> WeatherSnapshot:
        """Parses standard OpenWeatherMap `/data/2.5/weather` JSON into a WeatherSnapshot."""
        main = payload.get("main")
        if not isinstance(main, dict) or "temp" not in main or "humidity" not in main:
            raise WeatherClientError("OpenWeatherMap response missing required 'main.temp' or 'main.humidity'.")

        weather_list = payload.get("weather")
        if isinstance(weather_list, list) and weather_list and isinstance(weather_list[0], dict):
            condition = str(weather_list[0].get("main") or "Unknown")
            description = str(weather_list[0].get("description") or "")
        else:
            condition = "Unknown"
            description = ""

        coord = payload.get("coord") if isinstance(payload.get("coord"), dict) else {}
        lat_val = coord.get("lat", fallback_lat)
        lon_val = coord.get("lon", fallback_lon)
        wind = payload.get("wind") if isinstance(payload.get("wind"), dict) else {}

        return WeatherSnapshot(
            location_name=str(payload.get("name") or fallback_city or "Unknown"),
            lat=float(lat_val) if lat_val is not None else None,
            lon=float(lon_val) if lon_val is not None else None,
            temperature=float(main["temp"]),
            feels_like=float(main["feels_like"]) if main.get("feels_like") is not None else None,
            humidity=float(main["humidity"]),
            pressure=float(main["pressure"]) if main.get("pressure") is not None else None,
            wind_speed=float(wind["speed"]) if wind.get("speed") is not None else None,
            condition=condition,
            description=description,
            units=units,
            fetched_at=datetime.now(timezone.utc).isoformat(),
            cached=False,
            raw=payload,
        )

    def get_weather(
        self,
        *,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        city: Optional[str] = None,
        units: Optional[WeatherUnits] = None,
        force_refresh: bool = False,
        fallback_to_stale: bool = False,
    ) -> WeatherSnapshot:
        """Fetches current weather for coordinates or city with TTL caching."""
        _validate_lat_lon_values(lat, lon)
        clean_city = city.strip() if isinstance(city, str) and city.strip() else None
        if lat is None and clean_city is None:
            raise ValueError("Provide either ('lat', 'lon') coordinates or 'city'.")

        effective_units: WeatherUnits = units or self.units
        cache_key = self._build_cache_key(lat=lat, lon=lon, city=clean_city, units=effective_units)
        now = self._time_fn()

        stale_snapshot: Optional[WeatherSnapshot] = None
        with self._lock:
            cached_entry = self._cache.get(cache_key)
            if cached_entry is not None:
                expires_at, snap = cached_entry
                if not force_refresh and self.cache_ttl_seconds > 0 and now < expires_at:
                    return snap.model_copy(update={"cached": True})
                stale_snapshot = snap

        if not self.api_key or not self.api_key.strip():
            raise WeatherAuthError("OpenWeatherMap API key is not configured.")

        params: dict[str, Any] = {
            "appid": self.api_key.strip(),
            "units": effective_units,
        }
        if lat is not None and lon is not None:
            params["lat"] = lat
            params["lon"] = lon
        else:
            params["q"] = clean_city

        try:
            payload = self._fetcher(self.base_url, params, self.timeout_seconds)
            snapshot = self.parse_openweather_response(
                payload,
                units=effective_units,
                fallback_city=clean_city,
                fallback_lat=lat,
                fallback_lon=lon,
            )
        except WeatherClientError:
            if fallback_to_stale and stale_snapshot is not None:
                return stale_snapshot.model_copy(update={"cached": True})
            raise
        except TimeoutError as exc:
            if fallback_to_stale and stale_snapshot is not None:
                return stale_snapshot.model_copy(update={"cached": True})
            raise WeatherTimeoutError(f"Weather API request timed out: {exc}") from exc
        except Exception as exc:
            if fallback_to_stale and stale_snapshot is not None:
                return stale_snapshot.model_copy(update={"cached": True})
            raise WeatherClientError(f"Unexpected weather fetch error: {exc}", retryable=True) from exc

        if self.cache_ttl_seconds > 0:
            with self._lock:
                self._cache[cache_key] = (now + self.cache_ttl_seconds, snapshot)

        return snapshot


# ---------------------------------------------------------------------------
# F17.2: Temperature & Humidity Threshold Alert Evaluator
# ---------------------------------------------------------------------------


class WeatherThresholdEvaluator:
    """Evaluates temperature & humidity threshold rules with unit conversion and cooldown."""

    THRESHOLD_TYPES = {
        WeatherRuleType.TEMP_ABOVE,
        WeatherRuleType.TEMP_BELOW,
        WeatherRuleType.HUMIDITY_ABOVE,
        WeatherRuleType.HUMIDITY_BELOW,
    }

    def evaluate_rule(
        self,
        rule: WeatherAlertRule,
        snapshot: WeatherSnapshot,
        *,
        now: Optional[datetime] = None,
    ) -> WeatherAlertEvent:
        if rule.rule_type not in self.THRESHOLD_TYPES:
            raise ValueError(f"Rule type '{rule.rule_type.value}' is not a threshold rule.")
        assert rule.threshold is not None

        eval_dt = _parse_iso_datetime(now) or datetime.now(timezone.utc)
        eval_iso = eval_dt.isoformat()
        rule_id = rule.id or "unsaved"

        if rule.rule_type in {WeatherRuleType.TEMP_ABOVE, WeatherRuleType.TEMP_BELOW}:
            current_val = round(
                convert_temperature(snapshot.temperature, snapshot.units, rule.units), 4
            )
            unit_label = "°C" if rule.units == "metric" else ("°F" if rule.units == "imperial" else "K")
            if rule.rule_type == WeatherRuleType.TEMP_ABOVE:
                matched = current_val > rule.threshold
                comparator = ">"
            else:
                matched = current_val < rule.threshold
                comparator = "<"
            metric_name = "Temperature"
        else:
            current_val = float(snapshot.humidity)
            unit_label = "%"
            if rule.rule_type == WeatherRuleType.HUMIDITY_ABOVE:
                matched = current_val > rule.threshold
                comparator = ">"
            else:
                matched = current_val < rule.threshold
                comparator = "<"
            metric_name = "Humidity"

        if not matched:
            return WeatherAlertEvent(
                rule_id=rule_id,
                rule_name=rule.name,
                rule_type=rule.rule_type,
                severity=rule.severity,
                triggered=False,
                suppressed_by_cooldown=False,
                current_value=current_val,
                threshold=rule.threshold,
                units=rule.units if metric_name == "Temperature" else "%",
                message=(
                    f"{metric_name} {current_val}{unit_label} did not breach "
                    f"{comparator} {rule.threshold}{unit_label}."
                ),
                evaluated_at=eval_iso,
            )

        # Cooldown check
        if rule.cooldown_seconds > 0 and rule.last_triggered_at:
            last_dt = _parse_iso_datetime(rule.last_triggered_at)
            if last_dt is not None:
                elapsed = (eval_dt - last_dt).total_seconds()
                if elapsed < rule.cooldown_seconds:
                    remaining = max(0, int(rule.cooldown_seconds - elapsed))
                    return WeatherAlertEvent(
                        rule_id=rule_id,
                        rule_name=rule.name,
                        rule_type=rule.rule_type,
                        severity=rule.severity,
                        triggered=False,
                        suppressed_by_cooldown=True,
                        current_value=current_val,
                        threshold=rule.threshold,
                        units=rule.units if metric_name == "Temperature" else "%",
                        message=(
                            f"{metric_name} {current_val}{unit_label} {comparator} {rule.threshold}{unit_label} "
                            f"suppressed by cooldown ({remaining}s remaining)."
                        ),
                        evaluated_at=eval_iso,
                    )

        return WeatherAlertEvent(
            rule_id=rule_id,
            rule_name=rule.name,
            rule_type=rule.rule_type,
            severity=rule.severity,
            triggered=True,
            suppressed_by_cooldown=False,
            current_value=current_val,
            threshold=rule.threshold,
            units=rule.units if metric_name == "Temperature" else "%",
            message=(
                f"{metric_name} alert: {current_val}{unit_label} {comparator} "
                f"{rule.threshold}{unit_label} at {snapshot.location_name}."
            ),
            evaluated_at=eval_iso,
        )


# ---------------------------------------------------------------------------
# F17.4: Weather Condition Change Trigger Evaluator
# ---------------------------------------------------------------------------


class ConditionChangeEvaluator:
    """Detects weather condition transitions (e.g., Clear -> Rain/Storm) with cooldown."""

    def evaluate_rule(
        self,
        rule: WeatherAlertRule,
        *,
        previous_condition: Optional[str],
        current_condition: str,
        location_name: str = "Unknown",
        now: Optional[datetime] = None,
    ) -> WeatherAlertEvent:
        if rule.rule_type != WeatherRuleType.CONDITION_CHANGE:
            raise ValueError(f"Rule type '{rule.rule_type.value}' is not a condition_change rule.")

        eval_dt = _parse_iso_datetime(now) or datetime.now(timezone.utc)
        eval_iso = eval_dt.isoformat()
        rule_id = rule.id or "unsaved"

        if not previous_condition:
            return WeatherAlertEvent(
                rule_id=rule_id,
                rule_name=rule.name,
                rule_type=rule.rule_type,
                severity=rule.severity,
                triggered=False,
                current_value=current_condition,
                previous_value=None,
                message="Initial weather condition recorded; no prior condition for transition.",
                evaluated_at=eval_iso,
            )

        prev_norm = normalize_condition(previous_condition)
        curr_norm = normalize_condition(current_condition)
        if prev_norm == curr_norm:
            return WeatherAlertEvent(
                rule_id=rule_id,
                rule_name=rule.name,
                rule_type=rule.rule_type,
                severity=rule.severity,
                triggered=False,
                current_value=current_condition,
                previous_value=previous_condition,
                message=f"Weather condition unchanged ({current_condition}).",
                evaluated_at=eval_iso,
            )

        from_ok = condition_matches(previous_condition, rule.from_conditions)
        to_ok = condition_matches(current_condition, rule.to_conditions)
        if not (from_ok and to_ok):
            return WeatherAlertEvent(
                rule_id=rule_id,
                rule_name=rule.name,
                rule_type=rule.rule_type,
                severity=rule.severity,
                triggered=False,
                current_value=current_condition,
                previous_value=previous_condition,
                message=(
                    f"Transition {previous_condition} -> {current_condition} did not match "
                    f"rule filters."
                ),
                evaluated_at=eval_iso,
            )

        # Cooldown anti-spam check
        if rule.cooldown_seconds > 0 and rule.last_triggered_at:
            last_dt = _parse_iso_datetime(rule.last_triggered_at)
            if last_dt is not None:
                elapsed = (eval_dt - last_dt).total_seconds()
                if elapsed < rule.cooldown_seconds:
                    remaining = max(0, int(rule.cooldown_seconds - elapsed))
                    return WeatherAlertEvent(
                        rule_id=rule_id,
                        rule_name=rule.name,
                        rule_type=rule.rule_type,
                        severity=rule.severity,
                        triggered=False,
                        suppressed_by_cooldown=True,
                        current_value=current_condition,
                        previous_value=previous_condition,
                        message=(
                            f"Condition transition {previous_condition} -> {current_condition} "
                            f"suppressed by cooldown ({remaining}s remaining)."
                        ),
                        evaluated_at=eval_iso,
                    )

        return WeatherAlertEvent(
            rule_id=rule_id,
            rule_name=rule.name,
            rule_type=rule.rule_type,
            severity=rule.severity,
            triggered=True,
            suppressed_by_cooldown=False,
            current_value=current_condition,
            previous_value=previous_condition,
            message=(
                f"Weather condition changed at {location_name}: "
                f"{previous_condition} -> {current_condition}."
            ),
            evaluated_at=eval_iso,
        )


# ---------------------------------------------------------------------------
# F17.5: Standalone Storage Layer (DatabaseStore.get_connection or sqlite3)
# ---------------------------------------------------------------------------


class WeatherStorage:
    """Self-contained SQLite/DatabaseStore persistence for Phase 17 weather features."""

    DEFAULT_CONFIG_ID = "default"

    def __init__(self, store_or_path: Any) -> None:
        self._store = store_or_path if hasattr(store_or_path, "get_connection") else None
        self._db_path = None if self._store is not None else Path(store_or_path)
        self.ensure_schema()

    @contextmanager
    def _connection(self) -> Iterator[Any]:
        if self._store is not None:
            with self._store.get_connection() as conn:
                yield conn
            return

        assert self._db_path is not None
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def ensure_schema(self) -> None:
        """Ensures migration 0009 tables exist (idempotent via CREATE IF NOT EXISTS)."""
        migration_sql = _MIGRATION_0009_PATH.read_text(encoding="utf-8")
        with self._connection() as conn:
            conn.executescript(migration_sql)

    # ---- Config ----

    def get_config(self, config_id: str = DEFAULT_CONFIG_ID) -> WeatherConfig:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM weather_config WHERE id = ?",
                (config_id,),
            ).fetchone()
        if row is None:
            return WeatherConfig(id=config_id)
        data = dict(row)
        return WeatherConfig(
            id=data["id"],
            provider=data["provider"],
            api_key=data["api_key"],
            base_url=data["base_url"],
            units=data["units"],
            cache_ttl_seconds=int(data["cache_ttl_seconds"]),
            timeout_seconds=float(data["timeout_seconds"]),
            default_city=data["default_city"],
            default_lat=data["default_lat"],
            default_lon=data["default_lon"],
            enabled=bool(data["enabled"]),
            updated_at=data["updated_at"],
        )

    def upsert_config(self, config: WeatherConfig) -> WeatherConfig:
        now = datetime.now(timezone.utc).isoformat()
        stored = config.model_copy(update={"updated_at": now})
        sql = """
        INSERT INTO weather_config (
            id, provider, api_key, base_url, units, cache_ttl_seconds,
            timeout_seconds, default_city, default_lat, default_lon, enabled, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            provider = excluded.provider,
            api_key = excluded.api_key,
            base_url = excluded.base_url,
            units = excluded.units,
            cache_ttl_seconds = excluded.cache_ttl_seconds,
            timeout_seconds = excluded.timeout_seconds,
            default_city = excluded.default_city,
            default_lat = excluded.default_lat,
            default_lon = excluded.default_lon,
            enabled = excluded.enabled,
            updated_at = excluded.updated_at
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    stored.id,
                    stored.provider,
                    stored.api_key,
                    stored.base_url,
                    stored.units,
                    stored.cache_ttl_seconds,
                    stored.timeout_seconds,
                    stored.default_city,
                    stored.default_lat,
                    stored.default_lon,
                    1 if stored.enabled else 0,
                    stored.updated_at,
                ),
            )
        return stored

    # ---- Locations (F17.3) ----

    def upsert_location(self, location: WeatherLocation) -> WeatherLocation:
        now = datetime.now(timezone.utc).isoformat()
        stored = location.model_copy(update={"updated_at": now})
        sql = """
        INSERT INTO weather_locations (
            entity_id, entity_type, city, lat, lon, label, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(entity_id) DO UPDATE SET
            entity_type = excluded.entity_type,
            city = excluded.city,
            lat = excluded.lat,
            lon = excluded.lon,
            label = excluded.label,
            updated_at = excluded.updated_at
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    stored.entity_id,
                    stored.entity_type,
                    stored.city,
                    stored.lat,
                    stored.lon,
                    stored.label,
                    stored.updated_at,
                ),
            )
        return stored

    def get_location(self, entity_id: str) -> Optional[WeatherLocation]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM weather_locations WHERE entity_id = ?",
                (entity_id,),
            ).fetchone()
        if row is None:
            return None
        data = dict(row)
        return WeatherLocation(**data)

    def list_locations(self, entity_type: Optional[str] = None) -> list[WeatherLocation]:
        sql = "SELECT * FROM weather_locations"
        params: list[Any] = []
        if entity_type:
            sql += " WHERE entity_type = ?"
            params.append(entity_type)
        sql += " ORDER BY updated_at DESC"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [WeatherLocation(**dict(r)) for r in rows]

    def delete_location(self, entity_id: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM weather_locations WHERE entity_id = ?",
                (entity_id,),
            )
            return cursor.rowcount > 0

    # ---- Alert Rules ----

    @staticmethod
    def _row_to_rule(row: Any) -> WeatherAlertRule:
        data = dict(row)
        return WeatherAlertRule(
            id=data["id"],
            name=data["name"],
            enabled=bool(data["enabled"]),
            rule_type=WeatherRuleType(data["rule_type"]),
            threshold=data["threshold"],
            units=data["units"],
            from_conditions=json.loads(data["from_conditions"] or "[]"),
            to_conditions=json.loads(data["to_conditions"] or "[]"),
            entity_id=data["entity_id"],
            cooldown_seconds=int(data["cooldown_seconds"]),
            severity=data["severity"],
            description=data["description"],
            last_triggered_at=data["last_triggered_at"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def create_rule(self, rule: WeatherAlertRule) -> WeatherAlertRule:
        now = datetime.now(timezone.utc).isoformat()
        rule_id = rule.id or f"wrule_{os.urandom(8).hex()}"
        stored = rule.model_copy(
            update={"id": rule_id, "created_at": now, "updated_at": now}
        )
        sql = """
        INSERT INTO weather_alert_rules (
            id, name, enabled, rule_type, threshold, units,
            from_conditions, to_conditions, entity_id, cooldown_seconds,
            severity, description, last_triggered_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    stored.id,
                    stored.name,
                    1 if stored.enabled else 0,
                    stored.rule_type.value,
                    stored.threshold,
                    stored.units,
                    json.dumps(stored.from_conditions),
                    json.dumps(stored.to_conditions),
                    stored.entity_id,
                    stored.cooldown_seconds,
                    stored.severity,
                    stored.description,
                    stored.last_triggered_at,
                    stored.created_at,
                    stored.updated_at,
                ),
            )
        return stored

    def get_rule(self, rule_id: str) -> Optional[WeatherAlertRule]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM weather_alert_rules WHERE id = ?",
                (rule_id,),
            ).fetchone()
        return self._row_to_rule(row) if row is not None else None

    def list_rules(
        self,
        *,
        enabled_only: bool = False,
        rule_type: Optional[WeatherRuleType] = None,
        entity_id: Optional[str] = None,
        include_global_for_entity: bool = False,
    ) -> list[WeatherAlertRule]:
        clauses: list[str] = []
        params: list[Any] = []
        if enabled_only:
            clauses.append("enabled = 1")
        if rule_type is not None:
            clauses.append("rule_type = ?")
            params.append(rule_type.value)
        if entity_id is not None:
            if include_global_for_entity:
                clauses.append("(entity_id IS NULL OR entity_id = ?)")
                params.append(entity_id)
            else:
                clauses.append("entity_id = ?")
                params.append(entity_id)

        sql = "SELECT * FROM weather_alert_rules"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at ASC"
        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [self._row_to_rule(r) for r in rows]

    def update_rule_last_triggered(self, rule_id: str, triggered_at: str) -> None:
        with self._connection() as conn:
            conn.execute(
                "UPDATE weather_alert_rules SET last_triggered_at = ?, updated_at = ? WHERE id = ?",
                (triggered_at, triggered_at, rule_id),
            )

    def delete_rule(self, rule_id: str) -> bool:
        with self._connection() as conn:
            cursor = conn.execute(
                "DELETE FROM weather_alert_rules WHERE id = ?",
                (rule_id,),
            )
            return cursor.rowcount > 0

    # ---- Condition State ----

    def get_condition_state(self, location_key: str) -> Optional[dict[str, Any]]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT * FROM weather_condition_state WHERE location_key = ?",
                (location_key,),
            ).fetchone()
        return dict(row) if row is not None else None

    def upsert_condition_state(
        self,
        location_key: str,
        *,
        condition: str,
        temperature: Optional[float] = None,
        humidity: Optional[float] = None,
        observed_at: Optional[str] = None,
    ) -> dict[str, Any]:
        ts = observed_at or datetime.now(timezone.utc).isoformat()
        sql = """
        INSERT INTO weather_condition_state (
            location_key, last_condition, last_temperature, last_humidity, observed_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(location_key) DO UPDATE SET
            last_condition = excluded.last_condition,
            last_temperature = excluded.last_temperature,
            last_humidity = excluded.last_humidity,
            observed_at = excluded.observed_at
        """
        with self._connection() as conn:
            conn.execute(sql, (location_key, condition, temperature, humidity, ts))
        return {
            "location_key": location_key,
            "last_condition": condition,
            "last_temperature": temperature,
            "last_humidity": humidity,
            "observed_at": ts,
        }


# ---------------------------------------------------------------------------
# F17.3: Dynamic Location Updates Adapter
# ---------------------------------------------------------------------------


class LocationUpdateAdapter:
    """Adapter for updating and resolving per-device or per-user weather locations."""

    def __init__(self, storage: Optional[WeatherStorage] = None) -> None:
        self._storage = storage
        self._memory: dict[str, WeatherLocation] = {}

    def update_location(
        self,
        *,
        entity_id: str = "default",
        entity_type: EntityType = "device",
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        city: Optional[str] = None,
        label: Optional[str] = None,
    ) -> WeatherLocation:
        loc = WeatherLocation(
            entity_id=entity_id,
            entity_type=entity_type,
            lat=lat,
            lon=lon,
            city=city,
            label=label,
            updated_at=datetime.now(timezone.utc).isoformat(),
        )
        if self._storage is not None:
            return self._storage.upsert_location(loc)
        self._memory[loc.entity_id] = loc
        return loc

    def get_location(self, entity_id: str = "default") -> Optional[WeatherLocation]:
        if self._storage is not None:
            return self._storage.get_location(entity_id)
        return self._memory.get(entity_id)

    def list_locations(self, entity_type: Optional[str] = None) -> list[WeatherLocation]:
        if self._storage is not None:
            return self._storage.list_locations(entity_type=entity_type)
        items = list(self._memory.values())
        if entity_type:
            items = [i for i in items if i.entity_type == entity_type]
        return items

    def delete_location(self, entity_id: str) -> bool:
        if self._storage is not None:
            return self._storage.delete_location(entity_id)
        return self._memory.pop(entity_id, None) is not None


# ---------------------------------------------------------------------------
# F17.5: Unified Weather Service Facade
# ---------------------------------------------------------------------------


class WeatherService:
    """Coordinates WeatherStorage, OpenWeatherClient, LocationUpdateAdapter & Evaluators."""

    def __init__(
        self,
        storage: WeatherStorage,
        *,
        fetcher: Optional[HttpFetcher] = None,
        time_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self.storage = storage
        self.locations = LocationUpdateAdapter(storage=storage)
        self.threshold_evaluator = WeatherThresholdEvaluator()
        self.condition_evaluator = ConditionChangeEvaluator()
        self._fetcher = fetcher
        self._time_fn = time_fn
        self._client: Optional[OpenWeatherClient] = None

    def get_config(self) -> WeatherConfig:
        return self.storage.get_config()

    def update_config(
        self,
        *,
        provider: Optional[str] = None,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        units: Optional[WeatherUnits] = None,
        cache_ttl_seconds: Optional[int] = None,
        timeout_seconds: Optional[float] = None,
        default_city: Optional[str] = None,
        default_lat: Optional[float] = None,
        default_lon: Optional[float] = None,
        enabled: Optional[bool] = None,
    ) -> WeatherConfig:
        current = self.storage.get_config()
        updates: dict[str, Any] = {}
        if provider is not None:
            updates["provider"] = provider
        if api_key is not None:
            updates["api_key"] = api_key
        if base_url is not None:
            updates["base_url"] = base_url
        if units is not None:
            updates["units"] = units
        if cache_ttl_seconds is not None:
            updates["cache_ttl_seconds"] = cache_ttl_seconds
        if timeout_seconds is not None:
            updates["timeout_seconds"] = timeout_seconds
        if default_city is not None:
            updates["default_city"] = default_city.strip() or None
        if default_lat is not None or default_lon is not None:
            updates["default_lat"] = default_lat
            updates["default_lon"] = default_lon
        if enabled is not None:
            updates["enabled"] = enabled

        merged = WeatherConfig(**{**current.model_dump(), **updates})
        saved = self.storage.upsert_config(merged)
        # Rebuild client on config change so new key/TTL/timeout take effect immediately
        self._client = None
        return saved

    def get_client(self) -> OpenWeatherClient:
        if self._client is None:
            cfg = self.storage.get_config()
            self._client = OpenWeatherClient(
                api_key=cfg.api_key,
                base_url=cfg.base_url,
                units=cfg.units,
                cache_ttl_seconds=cfg.cache_ttl_seconds,
                timeout_seconds=cfg.timeout_seconds,
                fetcher=self._fetcher,
                time_fn=self._time_fn,
            )
        return self._client

    def create_rule(self, rule: WeatherAlertRule) -> WeatherAlertRule:
        return self.storage.create_rule(rule)

    def list_rules(
        self,
        *,
        enabled_only: bool = False,
        rule_type: Optional[WeatherRuleType] = None,
        entity_id: Optional[str] = None,
    ) -> list[WeatherAlertRule]:
        return self.storage.list_rules(
            enabled_only=enabled_only,
            rule_type=rule_type,
            entity_id=entity_id,
        )

    def delete_rule(self, rule_id: str) -> bool:
        return self.storage.delete_rule(rule_id)

    def evaluate(
        self,
        *,
        entity_id: Optional[str] = None,
        lat: Optional[float] = None,
        lon: Optional[float] = None,
        city: Optional[str] = None,
        units: Optional[WeatherUnits] = None,
        snapshot: Optional[WeatherSnapshot] = None,
        previous_condition: Optional[str] = None,
        force_refresh: bool = False,
        evaluated_at: Optional[datetime] = None,
    ) -> WeatherEvaluationResult:
        """Evaluates threshold and condition-change rules for a fetched or supplied snapshot."""
        eval_dt = _parse_iso_datetime(evaluated_at) or datetime.now(timezone.utc)
        eval_iso = eval_dt.isoformat()

        resolved_lat = lat
        resolved_lon = lon
        resolved_city = city
        location_key: str

        if entity_id:
            loc = self.locations.get_location(entity_id)
            if loc is not None and resolved_lat is None and resolved_lon is None and not resolved_city:
                resolved_lat = loc.lat
                resolved_lon = loc.lon
                resolved_city = loc.city

        if snapshot is None:
            cfg = self.storage.get_config()
            if resolved_lat is None and resolved_lon is None and not resolved_city:
                resolved_lat = cfg.default_lat
                resolved_lon = cfg.default_lon
                resolved_city = cfg.default_city
            if resolved_lat is None and resolved_lon is None and not resolved_city:
                raise ValueError(
                    "No location provided and no default or entity location is configured."
                )
            client = self.get_client()
            obs = client.get_weather(
                lat=resolved_lat,
                lon=resolved_lon,
                city=resolved_city,
                units=units or cfg.units,
                force_refresh=force_refresh,
            )
        else:
            obs = snapshot

        # Build stable location_key for condition transition tracking
        if entity_id:
            location_key = f"entity:{entity_id}"
        elif obs.lat is not None and obs.lon is not None:
            location_key = f"coord:{obs.lat:.4f},{obs.lon:.4f}"
        else:
            location_key = f"city:{obs.location_name.strip().lower()}"

        # Resolve previous condition from explicit override or persisted state
        if previous_condition is not None:
            prev_cond: Optional[str] = previous_condition
        else:
            state = self.storage.get_condition_state(location_key)
            prev_cond = state["last_condition"] if state else None

        condition_changed = bool(
            prev_cond and normalize_condition(prev_cond) != normalize_condition(obs.condition)
        )

        # Fetch applicable enabled rules
        if entity_id:
            rules = self.storage.list_rules(
                enabled_only=True,
                entity_id=entity_id,
                include_global_for_entity=True,
            )
        else:
            rules = [
                r for r in self.storage.list_rules(enabled_only=True) if r.entity_id is None
            ]

        triggered: list[WeatherAlertEvent] = []
        suppressed: list[WeatherAlertEvent] = []

        for rule in rules:
            if rule.rule_type == WeatherRuleType.CONDITION_CHANGE:
                event = self.condition_evaluator.evaluate_rule(
                    rule,
                    previous_condition=prev_cond,
                    current_condition=obs.condition,
                    location_name=obs.location_name,
                    now=eval_dt,
                )
            else:
                event = self.threshold_evaluator.evaluate_rule(rule, obs, now=eval_dt)

            if event.triggered:
                triggered.append(event)
                if rule.id:
                    self.storage.update_rule_last_triggered(rule.id, eval_iso)
            elif event.suppressed_by_cooldown:
                suppressed.append(event)

        # Persist latest observed condition state
        self.storage.upsert_condition_state(
            location_key,
            condition=obs.condition,
            temperature=obs.temperature,
            humidity=obs.humidity,
            observed_at=eval_iso,
        )

        return WeatherEvaluationResult(
            location_key=location_key,
            snapshot=obs,
            previous_condition=prev_cond,
            condition_changed=condition_changed,
            evaluated_rules_count=len(rules),
            triggered_alerts=triggered,
            suppressed_alerts=suppressed,
            evaluated_at=eval_iso,
        )
