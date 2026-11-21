"""Advanced Notification Rules Engine & Storage (Phase 16: F16.1–F16.5).

Provides:
- F16.1: Compiled RegEx matching on notification text payloads (title, body, sender)
         with syntax validation, ReDoS guardrails, and input length protection.
- F16.2: App package source whitelisting with exact, prefix, and glob/wildcard matching
         plus allow_all vs strict whitelist toggle and explicit blocklist support.
- F16.3: Alert classification rules (`critical`, `high`, `normal`, `low`, `ignored`)
         combining custom rules, keywords, regexes, package filters, and priority scoring.
- F16.4: Timezone-aware Silent / Do Not Disturb (DND) schedule evaluator supporting
         overnight wrap-around windows, day-of-week filters, and `critical` bypass.
- F16.5: Standalone SQLite / DatabaseStore storage layer backed by migration
         `0008_notification_history.sql` (no edits required to `store.py`).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator, Literal, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constants & Safety Limits (F16.1)
# ---------------------------------------------------------------------------

MAX_REGEX_PATTERN_LENGTH = 512
MAX_INPUT_TEXT_LENGTH = 4096

# Heuristic patterns that catch common catastrophic backtracking (ReDoS) constructs
# such as (a+)+, (.*)+, (a|aa)+, ([a-z]+)*, (.+?){2,}
_REDOS_NESTED_QUANTIFIER_RE = re.compile(
    r"\([^()]*[+*][^()]*\)\s*(?:[+*]|\{\s*\d+\s*,\s*\d*\s*\})"
)
_REDOS_OVERLAPPING_WILDCARD_RE = re.compile(r"\.\*\s*\.\*")

_MIGRATION_0008_PATH = (
    Path(__file__).resolve().parent.parent
    / "db"
    / "migrations"
    / "0008_notification_history.sql"
)

DAY_NAME_TO_INDEX: dict[str, int] = {
    "mon": 0,
    "monday": 0,
    "tue": 1,
    "tuesday": 1,
    "wed": 2,
    "wednesday": 2,
    "thu": 3,
    "thursday": 3,
    "fri": 4,
    "friday": 4,
    "sat": 5,
    "saturday": 5,
    "sun": 6,
    "sunday": 6,
}
INDEX_TO_DAY_NAME: dict[int, str] = {
    0: "mon",
    1: "tue",
    2: "wed",
    3: "thu",
    4: "fri",
    5: "sat",
    6: "sun",
}
ALL_DAYS: list[str] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]


class AlertLevel(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    NORMAL = "normal"
    LOW = "low"
    IGNORED = "ignored"


ALERT_LEVEL_RANK: dict[AlertLevel, int] = {
    AlertLevel.IGNORED: 0,
    AlertLevel.LOW: 1,
    AlertLevel.NORMAL: 2,
    AlertLevel.HIGH: 3,
    AlertLevel.CRITICAL: 4,
}

ALERT_LEVEL_DEFAULT_SCORE: dict[AlertLevel, int] = {
    AlertLevel.IGNORED: 0,
    AlertLevel.LOW: 25,
    AlertLevel.NORMAL: 50,
    AlertLevel.HIGH: 75,
    AlertLevel.CRITICAL: 100,
}

DEFAULT_CRITICAL_KEYWORDS: tuple[str, ...] = (
    "emergency",
    "critical",
    "sos",
    "intrusion",
    "fire alarm",
    "security breach",
    "urgent alert",
)
DEFAULT_HIGH_KEYWORDS: tuple[str, ...] = (
    "urgent",
    "otp",
    "verification code",
    "kode otp",
    "kode verifikasi",
    "2fa",
    "payment failed",
    "server down",
    "alert",
)
DEFAULT_LOW_KEYWORDS: tuple[str, ...] = (
    "promo",
    "promotion",
    "discount",
    "diskon",
    "newsletter",
    "flash sale",
    "unsubscribe",
)


# ---------------------------------------------------------------------------
# F16.1: Regex Validation, Compilation & Safe Matching
# ---------------------------------------------------------------------------


def validate_regex_pattern(
    pattern: str,
    *,
    case_sensitive: bool = False,
    max_pattern_length: int = MAX_REGEX_PATTERN_LENGTH,
) -> re.Pattern[str]:
    """Validates and compiles a regex pattern with length and ReDoS guardrails."""
    if not isinstance(pattern, str) or not pattern:
        raise ValueError("Regex pattern must be a non-empty string.")
    if len(pattern) > max_pattern_length:
        raise ValueError(
            f"Regex pattern exceeds maximum allowed length of {max_pattern_length} characters."
        )
    if _REDOS_NESTED_QUANTIFIER_RE.search(pattern):
        raise ValueError(
            "Unsafe regex pattern rejected: nested quantifiers may cause catastrophic backtracking."
        )
    if _REDOS_OVERLAPPING_WILDCARD_RE.search(pattern):
        raise ValueError(
            "Unsafe regex pattern rejected: adjacent '.*.*' wildcards are not allowed."
        )
    return _compile_cached_regex(pattern, case_sensitive)


@lru_cache(maxsize=512)
def _compile_cached_regex(pattern: str, case_sensitive: bool) -> re.Pattern[str]:
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"Invalid regular expression '{pattern}': {exc}") from exc


class RegexMatchOutcome(BaseModel):
    matched: bool
    field: str
    pattern: str
    matched_text: Optional[str] = None
    input_truncated: bool = False


def safe_regex_search(
    pattern: str,
    text: Optional[str],
    *,
    field: str = "text",
    case_sensitive: bool = False,
    max_input_length: int = MAX_INPUT_TEXT_LENGTH,
) -> RegexMatchOutcome:
    """Safely runs a validated compiled regex against bounded input text."""
    compiled = validate_regex_pattern(pattern, case_sensitive=case_sensitive)
    raw = text or ""
    truncated = len(raw) > max_input_length
    bounded = raw[:max_input_length] if truncated else raw
    match = compiled.search(bounded)
    return RegexMatchOutcome(
        matched=match is not None,
        field=field,
        pattern=pattern,
        matched_text=match.group(0) if match else None,
        input_truncated=truncated,
    )


# ---------------------------------------------------------------------------
# Data Models (F16.1–F16.5)
# ---------------------------------------------------------------------------


def _normalize_day(day: str | int) -> str:
    if isinstance(day, int):
        if day in INDEX_TO_DAY_NAME:
            return INDEX_TO_DAY_NAME[day]
        raise ValueError(f"Invalid day index '{day}'. Expected integer 0 (mon) through 6 (sun).")
    key = str(day).strip().lower()
    if key in DAY_NAME_TO_INDEX:
        return INDEX_TO_DAY_NAME[DAY_NAME_TO_INDEX[key]]
    raise ValueError(
        f"Invalid day name '{day}'. Expected one of {ALL_DAYS} or integers 0-6."
    )


def _parse_hhmm(value: str) -> time:
    if not isinstance(value, str) or not re.fullmatch(r"\d{2}:\d{2}", value.strip()):
        raise ValueError(f"Invalid time format '{value}'. Expected 'HH:MM' (24-hour).")
    hour_str, minute_str = value.strip().split(":")
    hour, minute = int(hour_str), int(minute_str)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ValueError(f"Time '{value}' out of range (00:00–23:59).")
    return time(hour=hour, minute=minute)


class NotificationPayload(BaseModel):
    """Incoming notification event payload."""

    notification_id: Optional[str] = Field(default=None, max_length=128)
    source_device_id: str = Field(default="unknown", max_length=128)
    package_name: str = Field(..., min_length=1, max_length=256)
    title: str = Field(default="")
    body: str = Field(default="")
    sender: Optional[str] = Field(default=None)
    received_at: Optional[datetime] = Field(default=None)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("package_name")
    @classmethod
    def _clean_package_name(cls, v: str) -> str:
        cleaned = v.strip()
        if not cleaned:
            raise ValueError("package_name must not be empty.")
        return cleaned


class PackageWhitelistConfig(BaseModel):
    """F16.2: App package source whitelisting configuration."""

    mode: Literal["allow_all", "strict"] = Field(
        default="allow_all",
        description="'allow_all' allows any non-blocked package; 'strict' requires a whitelist match.",
    )
    allowed_packages: list[str] = Field(
        default_factory=list,
        description="Allowed package names or glob/prefix patterns (e.g. 'com.whatsapp', 'org.telegram.*').",
    )
    blocked_packages: list[str] = Field(
        default_factory=list,
        description="Explicitly denied package patterns (always rejected even in allow_all mode).",
    )

    @field_validator("allowed_packages", "blocked_packages")
    @classmethod
    def _normalize_patterns(cls, patterns: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for pat in patterns:
            item = pat.strip().lower()
            if item and item not in seen:
                cleaned.append(item)
                seen.add(item)
        return cleaned


class WhitelistCheckResult(BaseModel):
    allowed: bool
    mode: Literal["allow_all", "strict"]
    matched_pattern: Optional[str] = None
    reason: str


def package_matches_pattern(package_name: str, pattern: str) -> bool:
    """Checks exact, prefix, or wildcard/glob package matching (case-insensitive)."""
    pkg = package_name.strip().lower()
    pat = pattern.strip().lower()
    if not pkg or not pat:
        return False
    if pat == "*":
        return True
    if pat.startswith("prefix:"):
        prefix = pat[len("prefix:") :].strip()
        return bool(prefix) and pkg.startswith(prefix)
    if "*" in pat or "?" in pat or "[" in pat:
        return fnmatch.fnmatchcase(pkg, pat)
    if pat.endswith("."):
        return pkg.startswith(pat) or pkg == pat[:-1]
    return pkg == pat


class PackageWhitelistChecker:
    """Evaluates source application packages against whitelist/blocklist policy (F16.2)."""

    def __init__(self, config: Optional[PackageWhitelistConfig] = None):
        self.config = config or PackageWhitelistConfig()

    def check(self, package_name: str) -> WhitelistCheckResult:
        pkg = package_name.strip().lower()
        for blocked in self.config.blocked_packages:
            if package_matches_pattern(pkg, blocked):
                return WhitelistCheckResult(
                    allowed=False,
                    mode=self.config.mode,
                    matched_pattern=blocked,
                    reason=f"Package '{package_name}' matched blocked pattern '{blocked}'.",
                )

        if self.config.mode == "allow_all":
            return WhitelistCheckResult(
                allowed=True,
                mode="allow_all",
                matched_pattern="*",
                reason="Whitelist mode is 'allow_all' and package is not blocked.",
            )

        for allowed in self.config.allowed_packages:
            if package_matches_pattern(pkg, allowed):
                return WhitelistCheckResult(
                    allowed=True,
                    mode="strict",
                    matched_pattern=allowed,
                    reason=f"Package '{package_name}' matched whitelist pattern '{allowed}'.",
                )

        return WhitelistCheckResult(
            allowed=False,
            mode="strict",
            matched_pattern=None,
            reason=f"Package '{package_name}' is not in strict whitelist.",
        )


class NotificationRule(BaseModel):
    """F16.1 + F16.3: Notification matching & classification rule."""

    id: Optional[str] = None
    name: str = Field(..., min_length=1, max_length=128)
    enabled: bool = True
    package_patterns: list[str] = Field(default_factory=list)
    title_regex: Optional[str] = None
    body_regex: Optional[str] = None
    sender_regex: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    regex_case_sensitive: bool = False
    alert_level: AlertLevel = AlertLevel.NORMAL
    priority_score: int = Field(default=50, ge=0, le=1000)
    bypass_silent: bool = False
    stop_on_match: bool = False
    description: Optional[str] = Field(default=None, max_length=512)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None

    @field_validator("package_patterns")
    @classmethod
    def _clean_package_patterns(cls, values: list[str]) -> list[str]:
        return [v.strip().lower() for v in values if v and v.strip()]

    @field_validator("keywords")
    @classmethod
    def _clean_keywords(cls, values: list[str]) -> list[str]:
        return [v.strip() for v in values if v and v.strip()]

    @model_validator(mode="after")
    def _validate_regex_fields(self) -> "NotificationRule":
        for field_name in ("title_regex", "body_regex", "sender_regex"):
            pattern = getattr(self, field_name)
            if pattern is not None:
                if pattern == "":
                    setattr(self, field_name, None)
                else:
                    validate_regex_pattern(
                        pattern, case_sensitive=self.regex_case_sensitive
                    )
        return self


class SilentScheduleWindow(BaseModel):
    """Additional DND time window inside a SilentScheduleConfig."""

    start_time: str = Field(..., description="Start time in HH:MM (24-hour).")
    end_time: str = Field(..., description="End time in HH:MM (24-hour).")
    days_of_week: list[str] = Field(default_factory=lambda: list(ALL_DAYS))
    label: Optional[str] = None

    @field_validator("start_time", "end_time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        _parse_hhmm(v)
        return v.strip()

    @field_validator("days_of_week", mode="before")
    @classmethod
    def _validate_days(cls, values: list[str | int]) -> list[str]:
        if not values:
            return list(ALL_DAYS)
        normalized: list[str] = []
        for item in values:
            day = _normalize_day(item)
            if day not in normalized:
                normalized.append(day)
        return normalized


class SilentScheduleConfig(BaseModel):
    """F16.4: Timezone-aware silent / Do Not Disturb schedule configuration."""

    enabled: bool = False
    start_time: str = Field(default="22:00", description="Primary DND start time (HH:MM).")
    end_time: str = Field(default="07:00", description="Primary DND end time (HH:MM).")
    days_of_week: list[str] = Field(default_factory=lambda: list(ALL_DAYS))
    timezone: str = Field(default="UTC", description="IANA timezone name (e.g. 'Asia/Jakarta').")
    allow_critical_bypass: bool = Field(
        default=True,
        description="When true, 'critical' alerts bypass silent mode.",
    )
    min_bypass_score: Optional[int] = Field(
        default=None,
        ge=0,
        le=1000,
        description="Optional priority score threshold that also bypasses silent mode.",
    )
    extra_windows: list[SilentScheduleWindow] = Field(default_factory=list)

    @field_validator("start_time", "end_time")
    @classmethod
    def _validate_time(cls, v: str) -> str:
        _parse_hhmm(v)
        return v.strip()

    @field_validator("days_of_week", mode="before")
    @classmethod
    def _validate_days(cls, values: list[str | int]) -> list[str]:
        if not values:
            return list(ALL_DAYS)
        normalized: list[str] = []
        for item in values:
            day = _normalize_day(item)
            if day not in normalized:
                normalized.append(day)
        return normalized

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, v: str) -> str:
        tz_name = v.strip()
        try:
            ZoneInfo(tz_name)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Unknown IANA timezone '{tz_name}'.") from exc
        return tz_name


class SilentCheckResult(BaseModel):
    in_silent_window: bool
    silenced: bool
    bypassed: bool
    local_time: str
    timezone: str
    reason: str


class SilentScheduleEvaluator:
    """Evaluates timezone-aware DND windows including overnight wrap-around (F16.4)."""

    @staticmethod
    def _to_local_dt(at_time: Optional[datetime], tz_name: str) -> datetime:
        tz = ZoneInfo(tz_name)
        if at_time is None:
            return datetime.now(tz)
        if at_time.tzinfo is None:
            return at_time.replace(tzinfo=timezone.utc).astimezone(tz)
        return at_time.astimezone(tz)

    @staticmethod
    def _is_window_active(
        local_dt: datetime,
        start_str: str,
        end_str: str,
        days_of_week: list[str],
    ) -> bool:
        day_indices = {DAY_NAME_TO_INDEX[d] for d in days_of_week}
        if not day_indices:
            return False

        start_t = _parse_hhmm(start_str)
        end_t = _parse_hhmm(end_str)
        now_t = local_dt.time().replace(second=0, microsecond=0)
        today_idx = local_dt.weekday()
        yesterday_idx = (local_dt - timedelta(days=1)).weekday()

        if start_t == end_t:
            # Full 24h window on matching days
            return today_idx in day_indices

        if start_t < end_t:
            # Same-day window (e.g. 09:00–17:00)
            return today_idx in day_indices and (start_t <= now_t < end_t)

        # Overnight wrap-around window (e.g. 22:00–07:00)
        if now_t >= start_t:
            return today_idx in day_indices
        if now_t < end_t:
            return yesterday_idx in day_indices
        return False

    def evaluate(
        self,
        config: SilentScheduleConfig,
        *,
        alert_level: AlertLevel,
        priority_score: int = 0,
        rule_bypass_silent: bool = False,
        at_time: Optional[datetime] = None,
    ) -> SilentCheckResult:
        local_dt = self._to_local_dt(at_time, config.timezone)
        local_iso = local_dt.isoformat()

        if not config.enabled:
            return SilentCheckResult(
                in_silent_window=False,
                silenced=False,
                bypassed=False,
                local_time=local_iso,
                timezone=config.timezone,
                reason="Silent schedule is disabled.",
            )

        in_window = self._is_window_active(
            local_dt, config.start_time, config.end_time, config.days_of_week
        )
        matched_window_desc = f"{config.start_time}-{config.end_time}"

        if not in_window:
            for extra in config.extra_windows:
                if self._is_window_active(
                    local_dt, extra.start_time, extra.end_time, extra.days_of_week
                ):
                    in_window = True
                    matched_window_desc = (
                        f"{extra.label} ({extra.start_time}-{extra.end_time})"
                        if extra.label
                        else f"{extra.start_time}-{extra.end_time}"
                    )
                    break

        if not in_window:
            return SilentCheckResult(
                in_silent_window=False,
                silenced=False,
                bypassed=False,
                local_time=local_iso,
                timezone=config.timezone,
                reason="Current local time is outside silent schedule windows.",
            )

        if config.allow_critical_bypass and alert_level == AlertLevel.CRITICAL:
            return SilentCheckResult(
                in_silent_window=True,
                silenced=False,
                bypassed=True,
                local_time=local_iso,
                timezone=config.timezone,
                reason=f"Critical alert bypassed active silent window {matched_window_desc}.",
            )

        if rule_bypass_silent:
            return SilentCheckResult(
                in_silent_window=True,
                silenced=False,
                bypassed=True,
                local_time=local_iso,
                timezone=config.timezone,
                reason=f"Matched rule explicitly bypassed active silent window {matched_window_desc}.",
            )

        if config.min_bypass_score is not None and priority_score >= config.min_bypass_score:
            return SilentCheckResult(
                in_silent_window=True,
                silenced=False,
                bypassed=True,
                local_time=local_iso,
                timezone=config.timezone,
                reason=(
                    f"Priority score {priority_score} >= bypass threshold "
                    f"{config.min_bypass_score} bypassed silent window {matched_window_desc}."
                ),
            )

        return SilentCheckResult(
            in_silent_window=True,
            silenced=True,
            bypassed=False,
            local_time=local_iso,
            timezone=config.timezone,
            reason=f"Silenced by active DND window {matched_window_desc} ({config.timezone}).",
        )


class NotificationEvaluationResult(BaseModel):
    """Complete evaluation decision for an incoming notification."""

    notification_id: str
    source_device_id: str
    package_name: str
    sender: Optional[str] = None
    title: str
    body: str
    alert_level: AlertLevel
    priority_score: int
    whitelisted: bool
    silenced: bool
    bypassed_silent: bool
    deliver: bool
    matched_rule_ids: list[str] = Field(default_factory=list)
    matched_rule_names: list[str] = Field(default_factory=list)
    regex_matches: list[RegexMatchOutcome] = Field(default_factory=list)
    input_truncated: bool = False
    reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    received_at: str
    created_at: str
    history_id: Optional[int] = None


# ---------------------------------------------------------------------------
# F16.5: Standalone Storage Layer (DatabaseStore.get_connection or sqlite3)
# ---------------------------------------------------------------------------


class NotificationStorage:
    """Self-contained persistence layer for notification rules, settings, and history.

    Works with either Nova's `DatabaseStore` (using `store.get_connection()`)
    or a direct SQLite file path (`Path | str`), without modifying `store.py`.
    """

    SETTING_SILENT_SCHEDULE = "silent_schedule"
    SETTING_WHITELIST_CONFIG = "whitelist_config"

    def __init__(self, store_or_path: Any):
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
        """Ensures migration 0008 tables exist (idempotent via CREATE IF NOT EXISTS)."""
        migration_sql = _MIGRATION_0008_PATH.read_text(encoding="utf-8")
        with self._connection() as conn:
            conn.executescript(migration_sql)

    # ---- Rules CRUD ----

    def create_rule(self, rule: NotificationRule) -> NotificationRule:
        now = datetime.now(timezone.utc).isoformat()
        rule_id = rule.id or f"nrule_{os.urandom(8).hex()}"
        stored = rule.model_copy(
            update={"id": rule_id, "created_at": now, "updated_at": now}
        )
        sql = """
        INSERT INTO notification_rules (
            id, name, enabled, package_patterns, title_regex, body_regex, sender_regex,
            keywords, regex_case_sensitive, alert_level, priority_score, bypass_silent,
            stop_on_match, description, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connection() as conn:
            conn.execute(
                sql,
                (
                    stored.id,
                    stored.name,
                    1 if stored.enabled else 0,
                    json.dumps(stored.package_patterns),
                    stored.title_regex,
                    stored.body_regex,
                    stored.sender_regex,
                    json.dumps(stored.keywords),
                    1 if stored.regex_case_sensitive else 0,
                    stored.alert_level.value,
                    stored.priority_score,
                    1 if stored.bypass_silent else 0,
                    1 if stored.stop_on_match else 0,
                    stored.description,
                    stored.created_at,
                    stored.updated_at,
                ),
            )
        return stored

    @staticmethod
    def _row_to_rule(row: Any) -> NotificationRule:
        data = dict(row)
        return NotificationRule(
            id=data["id"],
            name=data["name"],
            enabled=bool(data["enabled"]),
            package_patterns=json.loads(data["package_patterns"] or "[]"),
            title_regex=data["title_regex"],
            body_regex=data["body_regex"],
            sender_regex=data["sender_regex"],
            keywords=json.loads(data["keywords"] or "[]"),
            regex_case_sensitive=bool(data["regex_case_sensitive"]),
            alert_level=AlertLevel(data["alert_level"]),
            priority_score=int(data["priority_score"]),
            bypass_silent=bool(data["bypass_silent"]),
            stop_on_match=bool(data["stop_on_match"]),
            description=data["description"],
            created_at=data["created_at"],
            updated_at=data["updated_at"],
        )

    def get_rule(self, rule_id: str) -> Optional[NotificationRule]:
        sql = "SELECT * FROM notification_rules WHERE id = ?"
        with self._connection() as conn:
            row = conn.execute(sql, (rule_id,)).fetchone()
            return self._row_to_rule(row) if row else None

    def list_rules(self, *, enabled_only: bool = False) -> list[NotificationRule]:
        if enabled_only:
            sql = (
                "SELECT * FROM notification_rules WHERE enabled = 1 "
                "ORDER BY priority_score DESC, created_at ASC"
            )
        else:
            sql = (
                "SELECT * FROM notification_rules "
                "ORDER BY priority_score DESC, created_at ASC"
            )
        with self._connection() as conn:
            rows = conn.execute(sql).fetchall()
            return [self._row_to_rule(r) for r in rows]

    def delete_rule(self, rule_id: str) -> bool:
        existing = self.get_rule(rule_id)
        if existing is None:
            return False
        with self._connection() as conn:
            conn.execute("DELETE FROM notification_rules WHERE id = ?", (rule_id,))
        return True

    # ---- Settings (Silent Schedule & Whitelist) ----

    def _set_setting(self, key: str, value: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat()
        payload = json.dumps(value)
        sql = """
        INSERT INTO notification_settings (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
        """
        with self._connection() as conn:
            conn.execute(sql, (key, payload, now))

    def _get_setting(self, key: str) -> Optional[dict[str, Any]]:
        sql = "SELECT value FROM notification_settings WHERE key = ?"
        with self._connection() as conn:
            row = conn.execute(sql, (key,)).fetchone()
            if not row:
                return None
            return json.loads(dict(row)["value"])

    def save_silent_schedule(self, config: SilentScheduleConfig) -> SilentScheduleConfig:
        self._set_setting(self.SETTING_SILENT_SCHEDULE, config.model_dump())
        return config

    def get_silent_schedule(self) -> SilentScheduleConfig:
        raw = self._get_setting(self.SETTING_SILENT_SCHEDULE)
        if not raw:
            return SilentScheduleConfig()
        return SilentScheduleConfig(**raw)

    def save_whitelist_config(self, config: PackageWhitelistConfig) -> PackageWhitelistConfig:
        self._set_setting(self.SETTING_WHITELIST_CONFIG, config.model_dump())
        return config

    def get_whitelist_config(self) -> PackageWhitelistConfig:
        raw = self._get_setting(self.SETTING_WHITELIST_CONFIG)
        if not raw:
            return PackageWhitelistConfig()
        return PackageWhitelistConfig(**raw)

    # ---- Notification History ----

    def log_notification(self, result: NotificationEvaluationResult) -> int:
        sql = """
        INSERT INTO notification_history (
            notification_id, source_device_id, package_name, sender, title, body,
            alert_level, priority_score, whitelisted, silenced, bypassed_silent,
            deliver, matched_rule_ids, matched_rule_names, reasons, metadata,
            received_at, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """
        with self._connection() as conn:
            cursor = conn.execute(
                sql,
                (
                    result.notification_id,
                    result.source_device_id,
                    result.package_name,
                    result.sender,
                    result.title,
                    result.body,
                    result.alert_level.value,
                    result.priority_score,
                    1 if result.whitelisted else 0,
                    1 if result.silenced else 0,
                    1 if result.bypassed_silent else 0,
                    1 if result.deliver else 0,
                    json.dumps(result.matched_rule_ids),
                    json.dumps(result.matched_rule_names),
                    json.dumps(result.reasons),
                    json.dumps(result.metadata),
                    result.received_at,
                    result.created_at,
                ),
            )
            last_id = getattr(cursor, "lastrowid", None)
            if last_id is None:
                row = conn.execute(
                    "SELECT id FROM notification_history WHERE notification_id = ?",
                    (result.notification_id,),
                ).fetchone()
                last_id = dict(row)["id"] if row else 0
            return int(last_id)

    @staticmethod
    def _row_to_history(row: Any) -> dict[str, Any]:
        data = dict(row)
        data["whitelisted"] = bool(data["whitelisted"])
        data["silenced"] = bool(data["silenced"])
        data["bypassed_silent"] = bool(data["bypassed_silent"])
        data["deliver"] = bool(data["deliver"])
        data["matched_rule_ids"] = json.loads(data["matched_rule_ids"] or "[]")
        data["matched_rule_names"] = json.loads(data["matched_rule_names"] or "[]")
        data["reasons"] = json.loads(data["reasons"] or "[]")
        data["metadata"] = json.loads(data["metadata"] or "{}")
        return data

    def list_history(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        package_name: Optional[str] = None,
        alert_level: Optional[str] = None,
        silenced: Optional[bool] = None,
        source_device_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []

        if package_name:
            clauses.append("LOWER(package_name) = LOWER(?)")
            params.append(package_name)
        if alert_level:
            clauses.append("alert_level = ?")
            params.append(alert_level)
        if silenced is not None:
            clauses.append("silenced = ?")
            params.append(1 if silenced else 0)
        if source_device_id:
            clauses.append("source_device_id = ?")
            params.append(source_device_id)

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = (
            f"SELECT * FROM notification_history {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        )
        params.extend([limit, offset])

        with self._connection() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
            return [self._row_to_history(r) for r in rows]

    def get_history_stats(self) -> dict[str, Any]:
        with self._connection() as conn:
            total_row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM notification_history"
            ).fetchone()
            total = int(dict(total_row)["cnt"]) if total_row else 0

            level_rows = conn.execute(
                "SELECT alert_level, COUNT(*) AS cnt FROM notification_history GROUP BY alert_level"
            ).fetchall()
            by_level = {lvl.value: 0 for lvl in AlertLevel}
            for row in level_rows:
                row_dict = dict(row)
                by_level[row_dict["alert_level"]] = int(row_dict["cnt"])

            flags_row = conn.execute(
                """
                SELECT
                    SUM(CASE WHEN silenced = 1 THEN 1 ELSE 0 END) AS silenced_count,
                    SUM(CASE WHEN bypassed_silent = 1 THEN 1 ELSE 0 END) AS bypassed_count,
                    SUM(CASE WHEN whitelisted = 0 THEN 1 ELSE 0 END) AS blocked_count,
                    SUM(CASE WHEN deliver = 1 THEN 1 ELSE 0 END) AS delivered_count
                FROM notification_history
                """
            ).fetchone()
            flags = dict(flags_row) if flags_row else {}

        return {
            "total": total,
            "by_alert_level": by_level,
            "silenced_count": int(flags.get("silenced_count") or 0),
            "bypassed_silent_count": int(flags.get("bypassed_count") or 0),
            "blocked_whitelist_count": int(flags.get("blocked_count") or 0),
            "delivered_count": int(flags.get("delivered_count") or 0),
        }


# ---------------------------------------------------------------------------
# Complete Engine (F16.1 + F16.2 + F16.3 + F16.4 + F16.5)
# ---------------------------------------------------------------------------


class NotificationRuleEngine:
    """Evaluates, classifies, filters, silences, and logs incoming notifications."""

    def __init__(
        self,
        storage: Optional[NotificationStorage] = None,
        *,
        whitelist_config: Optional[PackageWhitelistConfig] = None,
        silent_schedule: Optional[SilentScheduleConfig] = None,
        use_default_heuristics: bool = True,
        max_input_length: int = MAX_INPUT_TEXT_LENGTH,
    ):
        self.storage = storage
        self.use_default_heuristics = use_default_heuristics
        self.max_input_length = max_input_length
        self.silent_evaluator = SilentScheduleEvaluator()

        # Optional in-memory fallbacks when storage is None
        self._memory_rules: list[NotificationRule] = []
        self._memory_whitelist: PackageWhitelistConfig = (
            whitelist_config or PackageWhitelistConfig()
        )
        self._memory_silent: SilentScheduleConfig = (
            silent_schedule or SilentScheduleConfig()
        )

        if self.storage is not None:
            if whitelist_config is not None:
                self.storage.save_whitelist_config(whitelist_config)
            if silent_schedule is not None:
                self.storage.save_silent_schedule(silent_schedule)

    # ---- Rule & Config Management ----

    def add_rule(self, rule: NotificationRule) -> NotificationRule:
        if self.storage is not None:
            return self.storage.create_rule(rule)
        now = datetime.now(timezone.utc).isoformat()
        created = rule.model_copy(
            update={
                "id": rule.id or f"nrule_{os.urandom(8).hex()}",
                "created_at": now,
                "updated_at": now,
            }
        )
        self._memory_rules.append(created)
        return created

    def list_rules(self, *, enabled_only: bool = False) -> list[NotificationRule]:
        if self.storage is not None:
            return self.storage.list_rules(enabled_only=enabled_only)
        rules = (
            [r for r in self._memory_rules if r.enabled]
            if enabled_only
            else list(self._memory_rules)
        )
        return sorted(rules, key=lambda r: r.priority_score, reverse=True)

    def delete_rule(self, rule_id: str) -> bool:
        if self.storage is not None:
            return self.storage.delete_rule(rule_id)
        for idx, r in enumerate(self._memory_rules):
            if r.id == rule_id:
                self._memory_rules.pop(idx)
                return True
        return False

    def get_whitelist_config(self) -> PackageWhitelistConfig:
        if self.storage is not None:
            return self.storage.get_whitelist_config()
        return self._memory_whitelist

    def set_whitelist_config(
        self, config: PackageWhitelistConfig
    ) -> PackageWhitelistConfig:
        if self.storage is not None:
            return self.storage.save_whitelist_config(config)
        self._memory_whitelist = config
        return config

    def get_silent_schedule(self) -> SilentScheduleConfig:
        if self.storage is not None:
            return self.storage.get_silent_schedule()
        return self._memory_silent

    def set_silent_schedule(
        self, config: SilentScheduleConfig
    ) -> SilentScheduleConfig:
        if self.storage is not None:
            return self.storage.save_silent_schedule(config)
        self._memory_silent = config
        return config

    # ---- Rule Matching Helpers ----

    def _match_rule(
        self, rule: NotificationRule, payload: NotificationPayload
    ) -> tuple[bool, list[RegexMatchOutcome], bool]:
        """Returns (matched, regex_outcomes, any_input_truncated)."""
        # 1. Package pattern filter (if specified on the rule)
        if rule.package_patterns:
            if not any(
                package_matches_pattern(payload.package_name, pat)
                for pat in rule.package_patterns
            ):
                return False, [], False

        regex_outcomes: list[RegexMatchOutcome] = []
        any_truncated = False

        # 2. RegEx matching on title, body, sender (AND semantics when multiple are set)
        regex_checks = (
            ("title", rule.title_regex, payload.title),
            ("body", rule.body_regex, payload.body),
            ("sender", rule.sender_regex, payload.sender or ""),
        )
        for field_name, pattern, text_val in regex_checks:
            if pattern is None:
                continue
            outcome = safe_regex_search(
                pattern,
                text_val,
                field=field_name,
                case_sensitive=rule.regex_case_sensitive,
                max_input_length=self.max_input_length,
            )
            if outcome.input_truncated:
                any_truncated = True
            if not outcome.matched:
                return False, [], any_truncated
            regex_outcomes.append(outcome)

        # 3. Keyword filter (OR semantics: at least one keyword present in combined text)
        if rule.keywords:
            combined_raw = f"{payload.title} {payload.body} {payload.sender or ''}"
            if len(combined_raw) > self.max_input_length:
                any_truncated = True
                combined = combined_raw[: self.max_input_length]
            else:
                combined = combined_raw
            haystack = combined if rule.regex_case_sensitive else combined.lower()
            matched_kw = any(
                (kw if rule.regex_case_sensitive else kw.lower()) in haystack
                for kw in rule.keywords
            )
            if not matched_kw:
                return False, [], any_truncated

        return True, regex_outcomes, any_truncated

    def _classify_heuristic(
        self, payload: NotificationPayload
    ) -> tuple[AlertLevel, int, str]:
        combined = f"{payload.title} {payload.body} {payload.sender or ''}"[
            : self.max_input_length
        ].lower()

        for kw in DEFAULT_CRITICAL_KEYWORDS:
            if kw in combined:
                return (
                    AlertLevel.CRITICAL,
                    95,
                    f"Heuristic matched critical keyword '{kw}'.",
                )
        for kw in DEFAULT_HIGH_KEYWORDS:
            if kw in combined:
                return (
                    AlertLevel.HIGH,
                    75,
                    f"Heuristic matched high-priority keyword '{kw}'.",
                )
        for kw in DEFAULT_LOW_KEYWORDS:
            if kw in combined:
                return (
                    AlertLevel.LOW,
                    20,
                    f"Heuristic matched low-priority keyword '{kw}'.",
                )
        return (
            AlertLevel.NORMAL,
            ALERT_LEVEL_DEFAULT_SCORE[AlertLevel.NORMAL],
            "Default classification applied (no specific rule matched).",
        )

    # ---- Primary Evaluation & Ingestion ----

    def evaluate(
        self,
        payload: NotificationPayload,
        *,
        record_history: bool = True,
        now: Optional[datetime] = None,
    ) -> NotificationEvaluationResult:
        """Evaluates an incoming notification through F16.1–F16.5 pipeline."""
        eval_dt = now or payload.received_at or datetime.now(timezone.utc)
        if eval_dt.tzinfo is None:
            eval_dt = eval_dt.replace(tzinfo=timezone.utc)

        created_iso = datetime.now(timezone.utc).isoformat()
        received_iso = (
            payload.received_at.replace(tzinfo=timezone.utc).isoformat()
            if payload.received_at and payload.received_at.tzinfo is None
            else (payload.received_at.isoformat() if payload.received_at else eval_dt.isoformat())
        )
        notif_id = payload.notification_id or f"notif_{os.urandom(8).hex()}"

        reasons: list[str] = []

        # Step 1 (F16.2): Package whitelist check
        whitelist_checker = PackageWhitelistChecker(self.get_whitelist_config())
        wl_res = whitelist_checker.check(payload.package_name)
        reasons.append(wl_res.reason)

        if not wl_res.allowed:
            result = NotificationEvaluationResult(
                notification_id=notif_id,
                source_device_id=payload.source_device_id,
                package_name=payload.package_name,
                sender=payload.sender,
                title=payload.title[: self.max_input_length],
                body=payload.body[: self.max_input_length],
                alert_level=AlertLevel.IGNORED,
                priority_score=0,
                whitelisted=False,
                silenced=False,
                bypassed_silent=False,
                deliver=False,
                matched_rule_ids=[],
                matched_rule_names=[],
                regex_matches=[],
                input_truncated=(
                    len(payload.title) > self.max_input_length
                    or len(payload.body) > self.max_input_length
                    or len(payload.sender or "") > self.max_input_length
                ),
                reasons=reasons,
                metadata=payload.metadata,
                received_at=received_iso,
                created_at=created_iso,
            )
            if record_history and self.storage is not None:
                result.history_id = self.storage.log_notification(result)
            return result

        # Step 2 (F16.1 + F16.3): Match enabled classification rules
        rules = self.list_rules(enabled_only=True)
        matched_rules: list[NotificationRule] = []
        all_regex_matches: list[RegexMatchOutcome] = []
        any_truncated = (
            len(payload.title) > self.max_input_length
            or len(payload.body) > self.max_input_length
            or len(payload.sender or "") > self.max_input_length
        )

        for rule in rules:
            is_match, outcomes, truncated = self._match_rule(rule, payload)
            if truncated:
                any_truncated = True
            if is_match:
                matched_rules.append(rule)
                all_regex_matches.extend(outcomes)
                if rule.stop_on_match:
                    break

        if matched_rules:
            # Rules are sorted by priority_score DESC; tie-break by alert level rank DESC
            winning_rule = max(
                matched_rules,
                key=lambda r: (r.priority_score, ALERT_LEVEL_RANK[r.alert_level]),
            )
            alert_level = winning_rule.alert_level
            priority_score = winning_rule.priority_score
            rule_bypass_silent = any(r.bypass_silent for r in matched_rules)
            reasons.append(
                f"Classified as '{alert_level.value}' (score {priority_score}) "
                f"by rule '{winning_rule.name}'."
            )
        elif self.use_default_heuristics:
            alert_level, priority_score, heuristic_reason = self._classify_heuristic(
                payload
            )
            rule_bypass_silent = False
            reasons.append(heuristic_reason)
        else:
            alert_level = AlertLevel.NORMAL
            priority_score = ALERT_LEVEL_DEFAULT_SCORE[AlertLevel.NORMAL]
            rule_bypass_silent = False
            reasons.append("No custom rule matched; defaulted to 'normal'.")

        if any_truncated:
            reasons.append(
                f"Input text exceeded {self.max_input_length} chars and was truncated for regex safety."
            )

        # Step 3 (F16.4): Silent / Do Not Disturb schedule evaluation
        silent_config = self.get_silent_schedule()
        silent_res = self.silent_evaluator.evaluate(
            silent_config,
            alert_level=alert_level,
            priority_score=priority_score,
            rule_bypass_silent=rule_bypass_silent,
            at_time=eval_dt,
        )
        reasons.append(silent_res.reason)

        deliver = (alert_level != AlertLevel.IGNORED) and (not silent_res.silenced)

        result = NotificationEvaluationResult(
            notification_id=notif_id,
            source_device_id=payload.source_device_id,
            package_name=payload.package_name,
            sender=payload.sender,
            title=payload.title[: self.max_input_length],
            body=payload.body[: self.max_input_length],
            alert_level=alert_level,
            priority_score=priority_score,
            whitelisted=True,
            silenced=silent_res.silenced,
            bypassed_silent=silent_res.bypassed,
            deliver=deliver,
            matched_rule_ids=[r.id for r in matched_rules if r.id],
            matched_rule_names=[r.name for r in matched_rules],
            regex_matches=all_regex_matches,
            input_truncated=any_truncated,
            reasons=reasons,
            metadata=payload.metadata,
            received_at=received_iso,
            created_at=created_iso,
        )

        # Step 4 (F16.5): Persist to notification_history
        if record_history and self.storage is not None:
            result.history_id = self.storage.log_notification(result)

        return result
