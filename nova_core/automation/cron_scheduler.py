"""Time & Cron Scheduler Trigger Engine (Phase 15: F15.1–F15.3).

Provides:
- F15.1: Lightweight 5-field cron expression parser & evaluator with `next_run_at`.
- F15.2: DST & Timezone offset calculator using `zoneinfo.ZoneInfo`, including
  ambiguous (fall-back) and non-existent (spring-forward) transition handling.
- F15.3: Overlap prevention & TTL-based execution locking (`acquire_lock` / `release_lock`)
  plus `CronScheduler` orchestration with `DatabaseStore` execution logging.
"""

from __future__ import annotations

import calendar
import inspect
import logging
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger("nova.scheduler")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class CronParseError(ValueError):
    """Raised when a 5-field cron expression is syntactically or semantically invalid."""


class TimezoneError(ValueError):
    """Raised when an IANA timezone name is invalid or a DST transition cannot be resolved."""


# ---------------------------------------------------------------------------
# F15.2: DST & Timezone Offset Calculator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ResolvedDateTime:
    """Result of resolving a local wall-clock datetime within an IANA timezone."""
    local_dt: datetime
    utc_dt: datetime
    timezone_name: str
    transition_type: str  # "normal" | "ambiguous" | "non_existent"
    utc_offset_seconds: int
    dst_offset_seconds: int
    is_dst: bool
    fold: int
    adjusted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "timezone": self.timezone_name,
            "local_iso": self.local_dt.isoformat(),
            "utc_iso": self.utc_dt.isoformat(),
            "transition_type": self.transition_type,
            "utc_offset_seconds": self.utc_offset_seconds,
            "utc_offset_str": format_utc_offset(self.utc_offset_seconds),
            "dst_offset_seconds": self.dst_offset_seconds,
            "is_dst": self.is_dst,
            "fold": self.fold,
            "adjusted": self.adjusted,
        }


def format_utc_offset(offset_seconds: int) -> str:
    """Formats an offset in seconds as '+HH:MM' or '-HH:MM'."""
    sign = "+" if offset_seconds >= 0 else "-"
    total_minutes = abs(offset_seconds) // 60
    hours, minutes = divmod(total_minutes, 60)
    return f"{sign}{hours:02d}:{minutes:02d}"


def validate_timezone(tz_name: str | ZoneInfo) -> ZoneInfo:
    """Validates and returns a `ZoneInfo` instance for an IANA timezone name."""
    if isinstance(tz_name, ZoneInfo):
        return tz_name
    if not isinstance(tz_name, str) or not tz_name.strip():
        raise TimezoneError("Timezone name must be a non-empty string.")
    name = tz_name.strip()
    if name.upper() == "UTC":
        return ZoneInfo("UTC")
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError as exc:
        raise TimezoneError(f"Unknown IANA timezone: '{name}'.") from exc
    except Exception as exc:
        raise TimezoneError(f"Invalid timezone '{name}': {exc}") from exc


def _wall_clock_tuple(dt: datetime) -> tuple[int, int, int, int, int, int, int]:
    return (dt.year, dt.month, dt.day, dt.hour, dt.minute, dt.second, dt.microsecond)


def resolve_local_datetime(
    naive_dt: datetime,
    tz_name: str | ZoneInfo,
    ambiguous_strategy: str = "prefer_first",
    non_existent_strategy: str = "shift_forward",
) -> ResolvedDateTime:
    """Resolves a naive local wall-clock datetime into an aware datetime with DST handling.

    Args:
        naive_dt: Local wall-clock datetime (if aware, its tzinfo is stripped first).
        tz_name: IANA timezone name or ZoneInfo instance.
        ambiguous_strategy: How to handle fall-back ambiguous times:
            - "prefer_first": choose fold=0 (earlier UTC instant / DST)
            - "prefer_second": choose fold=1 (later UTC instant / standard time)
            - "raise": raise TimezoneError
        non_existent_strategy: How to handle spring-forward gap times:
            - "shift_forward": shift to the equivalent valid instant after the gap
            - "raise": raise TimezoneError
    """
    tz = validate_timezone(tz_name)
    tz_key = tz.key or str(tz_name)
    naive = naive_dt.replace(tzinfo=None)

    dt_fold0 = naive.replace(tzinfo=tz, fold=0)
    dt_fold1 = naive.replace(tzinfo=tz, fold=1)

    utc_fold0 = dt_fold0.astimezone(timezone.utc)
    utc_fold1 = dt_fold1.astimezone(timezone.utc)

    # Check non-existent (spring-forward gap): round-tripping through UTC changes wall clock.
    roundtrip0 = utc_fold0.astimezone(tz)
    if _wall_clock_tuple(roundtrip0) != _wall_clock_tuple(naive):
        if non_existent_strategy == "raise":
            raise TimezoneError(
                f"Local datetime '{naive.isoformat()}' does not exist in timezone '{tz_key}' (DST gap)."
            )
        if non_existent_strategy != "shift_forward":
            raise TimezoneError(f"Unsupported non_existent_strategy: '{non_existent_strategy}'.")

        # Round-trip produces the shifted-forward valid local datetime after the DST gap.
        resolved_local = roundtrip0
        resolved_utc = resolved_local.astimezone(timezone.utc)
        offset_sec = int((resolved_local.utcoffset() or timedelta(0)).total_seconds())
        dst_sec = int((resolved_local.dst() or timedelta(0)).total_seconds())
        return ResolvedDateTime(
            local_dt=resolved_local,
            utc_dt=resolved_utc,
            timezone_name=tz_key,
            transition_type="non_existent",
            utc_offset_seconds=offset_sec,
            dst_offset_seconds=dst_sec,
            is_dst=dst_sec != 0,
            fold=resolved_local.fold,
            adjusted=True,
        )

    # Check ambiguous (fall-back fold): fold=0 and fold=1 map to different UTC instants.
    if utc_fold0 != utc_fold1:
        if ambiguous_strategy == "raise":
            raise TimezoneError(
                f"Local datetime '{naive.isoformat()}' is ambiguous in timezone '{tz_key}' (DST fold)."
            )
        if ambiguous_strategy == "prefer_first":
            resolved_local = dt_fold0
            resolved_utc = utc_fold0
        elif ambiguous_strategy == "prefer_second":
            resolved_local = dt_fold1
            resolved_utc = utc_fold1
        else:
            raise TimezoneError(f"Unsupported ambiguous_strategy: '{ambiguous_strategy}'.")

        offset_sec = int((resolved_local.utcoffset() or timedelta(0)).total_seconds())
        dst_sec = int((resolved_local.dst() or timedelta(0)).total_seconds())
        return ResolvedDateTime(
            local_dt=resolved_local,
            utc_dt=resolved_utc,
            timezone_name=tz_key,
            transition_type="ambiguous",
            utc_offset_seconds=offset_sec,
            dst_offset_seconds=dst_sec,
            is_dst=dst_sec != 0,
            fold=resolved_local.fold,
            adjusted=False,
        )

    # Normal unambiguous wall-clock time.
    offset_sec = int((dt_fold0.utcoffset() or timedelta(0)).total_seconds())
    dst_sec = int((dt_fold0.dst() or timedelta(0)).total_seconds())
    return ResolvedDateTime(
        local_dt=dt_fold0,
        utc_dt=utc_fold0,
        timezone_name=tz_key,
        transition_type="normal",
        utc_offset_seconds=offset_sec,
        dst_offset_seconds=dst_sec,
        is_dst=dst_sec != 0,
        fold=0,
        adjusted=False,
    )


def get_timezone_offset_info(dt: datetime, tz_name: str | ZoneInfo) -> dict[str, Any]:
    """Calculates UTC offset, DST status, and transition metadata for a datetime in `tz_name`."""
    tz = validate_timezone(tz_name)
    tz_key = tz.key or str(tz_name)
    if dt.tzinfo is None:
        return resolve_local_datetime(dt, tz).to_dict()

    local_dt = dt.astimezone(tz)
    utc_dt = local_dt.astimezone(timezone.utc)
    offset_sec = int((local_dt.utcoffset() or timedelta(0)).total_seconds())
    dst_sec = int((local_dt.dst() or timedelta(0)).total_seconds())

    # Detect if this aware instant sits inside an ambiguous local wall-clock hour.
    naive = local_dt.replace(tzinfo=None)
    u0 = naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
    u1 = naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
    transition_type = "ambiguous" if u0 != u1 else "normal"

    return {
        "timezone": tz_key,
        "local_iso": local_dt.isoformat(),
        "utc_iso": utc_dt.isoformat(),
        "transition_type": transition_type,
        "utc_offset_seconds": offset_sec,
        "utc_offset_str": format_utc_offset(offset_sec),
        "dst_offset_seconds": dst_sec,
        "is_dst": dst_sec != 0,
        "fold": local_dt.fold,
        "adjusted": False,
    }


def to_utc(
    dt: datetime,
    tz_name: str | ZoneInfo = "UTC",
    ambiguous_strategy: str = "prefer_first",
    non_existent_strategy: str = "shift_forward",
) -> datetime:
    """Converts a naive or timezone-aware datetime into UTC."""
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc)
    return resolve_local_datetime(
        dt,
        tz_name,
        ambiguous_strategy=ambiguous_strategy,
        non_existent_strategy=non_existent_strategy,
    ).utc_dt


def from_utc(dt: datetime, tz_name: str | ZoneInfo) -> datetime:
    """Converts a UTC (or aware) datetime into the target timezone."""
    tz = validate_timezone(tz_name)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


# ---------------------------------------------------------------------------
# F15.1: 5-Field Cron Expression Parser & Evaluator
# ---------------------------------------------------------------------------

_CRON_MACROS: dict[str, str] = {
    "@yearly": "0 0 1 1 *",
    "@annually": "0 0 1 1 *",
    "@monthly": "0 0 1 * *",
    "@weekly": "0 0 * * 0",
    "@daily": "0 0 * * *",
    "@midnight": "0 0 * * *",
    "@hourly": "0 * * * *",
}

_MONTH_NAMES: dict[str, int] = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4,
    "MAY": 5, "JUN": 6, "JUL": 7, "AUG": 8,
    "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

_DOW_NAMES: dict[str, int] = {
    "SUN": 0, "MON": 1, "TUE": 2, "WED": 3,
    "THU": 4, "FRI": 5, "SAT": 6,
}


def _parse_atom(
    token: str,
    min_val: int,
    max_val: int,
    names: dict[str, int] | None = None,
    allow_sunday_7: bool = False,
) -> int:
    upper = token.strip().upper()
    if names and upper in names:
        return names[upper]
    try:
        val = int(upper)
    except ValueError as exc:
        raise CronParseError(f"Invalid cron value '{token}'.") from exc
    if allow_sunday_7 and val == 7:
        return 7
    if val < min_val or val > max_val:
        raise CronParseError(
            f"Cron value {val} out of bounds [{min_val}..{max_val}]."
        )
    return val


def _parse_field(
    field_str: str,
    min_val: int,
    max_val: int,
    field_name: str,
    names: dict[str, int] | None = None,
    allow_sunday_7: bool = False,
) -> tuple[frozenset[int], bool]:
    """Parses a single cron field into (sorted_set_of_values, is_wildcard)."""
    raw = field_str.strip()
    if not raw:
        raise CronParseError(f"Empty cron field for '{field_name}'.")

    is_wildcard = raw == "*"
    values: set[int] = set()

    for part in raw.split(","):
        part = part.strip()
        if not part:
            raise CronParseError(f"Empty list element in '{field_name}' field '{field_str}'.")

        step = 1
        if "/" in part:
            pieces = part.split("/")
            if len(pieces) != 2 or not pieces[1]:
                raise CronParseError(f"Invalid step syntax '{part}' in '{field_name}'.")
            base, step_str = pieces[0].strip(), pieces[1].strip()
            try:
                step = int(step_str)
            except ValueError as exc:
                raise CronParseError(f"Invalid step value '{step_str}' in '{field_name}'.") from exc
            if step <= 0:
                raise CronParseError(f"Step must be > 0 in '{field_name}', got {step}.")
        else:
            base = part

        if base == "*":
            start, end = min_val, max_val
        elif "-" in base:
            bounds = base.split("-")
            if len(bounds) != 2 or not bounds[0] or not bounds[1]:
                raise CronParseError(f"Invalid range '{base}' in '{field_name}'.")
            start = _parse_atom(bounds[0], min_val, max_val, names, allow_sunday_7)
            end = _parse_atom(bounds[1], min_val, max_val, names, allow_sunday_7)
            if start > end:
                raise CronParseError(
                    f"Reversed range '{base}' ({start} > {end}) in '{field_name}'."
                )
        else:
            val = _parse_atom(base, min_val, max_val, names, allow_sunday_7)
            if "/" in part:
                # Standard cron: "a/n" means from a through max_val stepping by n
                start, end = val, max_val
            else:
                start, end = val, val

        for v in range(start, end + 1, step):
            values.add(0 if (allow_sunday_7 and v == 7) else v)

    if not values:
        raise CronParseError(f"Field '{field_name}' produced no valid values.")
    return frozenset(values), is_wildcard


class CronExpression:
    """Parsed 5-field cron expression (`minute hour dom month dow`)."""

    def __init__(self, expression: str):
        if not isinstance(expression, str) or not expression.strip():
            raise CronParseError("Cron expression must be a non-empty string.")

        self.raw_expression = expression.strip()
        expanded = _CRON_MACROS.get(self.raw_expression.lower(), self.raw_expression)
        fields = expanded.split()
        if len(fields) != 5:
            raise CronParseError(
                f"Expected 5 cron fields (minute hour dom month dow), got {len(fields)} in '{self.raw_expression}'."
            )

        self.minutes, _ = _parse_field(fields[0], 0, 59, "minute")
        self.hours, _ = _parse_field(fields[1], 0, 23, "hour")
        self.days_of_month, self.dom_is_wildcard = _parse_field(fields[2], 1, 31, "day_of_month")
        self.months, _ = _parse_field(fields[3], 1, 12, "month", names=_MONTH_NAMES)
        self.days_of_week, self.dow_is_wildcard = _parse_field(
            fields[4], 0, 6, "day_of_week", names=_DOW_NAMES, allow_sunday_7=True
        )

        self._sorted_minutes = sorted(self.minutes)
        self._sorted_hours = sorted(self.hours)
        self._sorted_months = sorted(self.months)

    @staticmethod
    def _cron_dow(dt: datetime) -> int:
        """Converts Python weekday (Mon=0..Sun=6) to cron DOW (Sun=0..Sat=6)."""
        return (dt.weekday() + 1) % 7

    def _day_matches(self, dt: datetime) -> bool:
        dom_match = dt.day in self.days_of_month
        dow_match = self._cron_dow(dt) in self.days_of_week
        if not self.dom_is_wildcard and not self.dow_is_wildcard:
            # Standard Vixie cron: match if EITHER dom or dow matches when both are restricted.
            return dom_match or dow_match
        return dom_match and dow_match

    def matches(self, dt: datetime, tz: str | ZoneInfo | None = None) -> bool:
        """Returns True if `dt` (optionally converted to `tz`) matches this cron expression."""
        target = dt
        if tz is not None:
            zone = validate_timezone(tz)
            if target.tzinfo is None:
                target = resolve_local_datetime(target, zone).local_dt
            else:
                target = target.astimezone(zone)

        return (
            target.month in self.months
            and self._day_matches(target)
            and target.hour in self.hours
            and target.minute in self.minutes
        )

    def next_run_at(
        self,
        after: datetime | None = None,
        tz: str | ZoneInfo = "UTC",
        max_search_years: int = 5,
    ) -> datetime:
        """Computes the next timezone-aware datetime strictly after `after` matching this cron.

        Handles DST transitions cleanly:
        - Non-existent (spring-forward) wall-clock matches are shifted forward to the first
          valid post-gap instant (if strictly after `after`).
        - Ambiguous (fall-back) wall-clock matches resolve to the earliest valid instant
          strictly after `after` (fold=0 first, then fold=1 if `after` is already past fold=0).
        """
        zone = validate_timezone(tz)
        if after is None:
            after_utc = datetime.now(timezone.utc)
        elif after.tzinfo is None:
            after_utc = resolve_local_datetime(after, zone).utc_dt
        else:
            after_utc = after.astimezone(timezone.utc)

        after_local = after_utc.astimezone(zone)

        # Start candidate at the next whole minute in local wall clock.
        base_minute = after_local.replace(second=0, microsecond=0)
        # If we're in an ambiguous fall-back hour at fold=0, the same wall-clock minute at fold=1
        # could be strictly after `after_utc` by 1 hour; handle by checking candidate >= base_minute.
        candidate = datetime(
            base_minute.year,
            base_minute.month,
            base_minute.day,
            base_minute.hour,
            base_minute.minute,
        )
        max_year = candidate.year + max_search_years

        while candidate.year <= max_year:
            # 1. Advance month if needed
            if candidate.month not in self.months:
                next_months = [m for m in self._sorted_months if m > candidate.month]
                if next_months:
                    candidate = datetime(candidate.year, next_months[0], 1, 0, 0)
                else:
                    candidate = datetime(candidate.year + 1, self._sorted_months[0], 1, 0, 0)
                continue

            # 2. Advance day if needed
            if not self._day_matches(candidate):
                days_in_month = calendar.monthrange(candidate.year, candidate.month)[1]
                if candidate.day < days_in_month:
                    candidate = datetime(candidate.year, candidate.month, candidate.day + 1, 0, 0)
                else:
                    if candidate.month == 12:
                        candidate = datetime(candidate.year + 1, 1, 1, 0, 0)
                    else:
                        candidate = datetime(candidate.year, candidate.month + 1, 1, 0, 0)
                continue

            # 3. Advance hour if needed
            if candidate.hour not in self.hours:
                next_hours = [h for h in self._sorted_hours if h > candidate.hour]
                if next_hours:
                    candidate = datetime(
                        candidate.year, candidate.month, candidate.day, next_hours[0], 0
                    )
                else:
                    candidate = (
                        datetime(candidate.year, candidate.month, candidate.day, 0, 0)
                        + timedelta(days=1)
                    )
                continue

            # 4. Advance minute if needed
            if candidate.minute not in self.minutes:
                next_mins = [m for m in self._sorted_minutes if m > candidate.minute]
                if next_mins:
                    candidate = datetime(
                        candidate.year,
                        candidate.month,
                        candidate.day,
                        candidate.hour,
                        next_mins[0],
                    )
                else:
                    candidate = (
                        datetime(
                            candidate.year, candidate.month, candidate.day, candidate.hour, 0
                        )
                        + timedelta(hours=1)
                    )
                continue

            # 5. Candidate wall-clock matches all 5 cron fields; resolve DST & verify > after_utc
            resolved = resolve_local_datetime(
                candidate,
                zone,
                ambiguous_strategy="prefer_first",
                non_existent_strategy="shift_forward",
            )
            if resolved.utc_dt > after_utc:
                return resolved.local_dt

            # If ambiguous and fold=0 was <= after_utc, check fold=1
            if resolved.transition_type == "ambiguous":
                resolved_second = resolve_local_datetime(
                    candidate,
                    zone,
                    ambiguous_strategy="prefer_second",
                    non_existent_strategy="shift_forward",
                )
                if resolved_second.utc_dt > after_utc:
                    return resolved_second.local_dt

            # Advance by 1 minute and keep searching
            candidate = candidate + timedelta(minutes=1)

        raise CronParseError(
            f"No matching run found for '{self.raw_expression}' within {max_search_years} years."
        )

    def next_n_runs(
        self,
        count: int,
        after: datetime | None = None,
        tz: str | ZoneInfo = "UTC",
    ) -> list[datetime]:
        """Returns the next `count` scheduled run datetimes in timezone `tz`."""
        if count <= 0:
            return []
        runs: list[datetime] = []
        cursor = after
        for _ in range(count):
            nxt = self.next_run_at(after=cursor, tz=tz)
            runs.append(nxt)
            cursor = nxt
        return runs


def parse_cron(expression: str) -> CronExpression:
    """Parses and validates a 5-field cron expression."""
    return CronExpression(expression)


def next_run_at(
    expression: str,
    after: datetime | None = None,
    tz: str | ZoneInfo = "UTC",
) -> datetime:
    """Convenience helper to compute the next run datetime for a cron expression."""
    return CronExpression(expression).next_run_at(after=after, tz=tz)


# ---------------------------------------------------------------------------
# F15.3: Overlap Check & Execution Lock Manager
# ---------------------------------------------------------------------------

@dataclass
class LockEntry:
    rule_id: str
    owner_id: str
    token: str
    acquired_at: float
    expires_at: float


@dataclass(frozen=True)
class LockAcquisitionResult:
    acquired: bool
    rule_id: str
    token: Optional[str] = None
    owner_id: Optional[str] = None
    expires_at: Optional[float] = None
    reason: Optional[str] = None


class ExecutionLockManager:
    """Thread-safe in-process execution lock with TTL to prevent overlapping job runs."""

    def __init__(self, clock: Callable[[], float] | None = None):
        self._locks: dict[str, LockEntry] = {}
        self._mutex = threading.RLock()
        self._clock = clock or time.monotonic

    def _evict_if_expired(self, rule_id: str, now: float) -> None:
        entry = self._locks.get(rule_id)
        if entry is not None and now >= entry.expires_at:
            del self._locks[rule_id]

    def acquire_lock(
        self,
        rule_id: str,
        owner_id: str | None = None,
        ttl_seconds: float = 300.0,
    ) -> LockAcquisitionResult:
        """Attempts to acquire an exclusive execution lock for `rule_id` with a TTL."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0.")
        now = self._clock()
        with self._mutex:
            self._evict_if_expired(rule_id, now)
            existing = self._locks.get(rule_id)
            if existing is not None:
                remaining = round(max(0.0, existing.expires_at - now), 3)
                return LockAcquisitionResult(
                    acquired=False,
                    rule_id=rule_id,
                    owner_id=existing.owner_id,
                    expires_at=existing.expires_at,
                    reason=(
                        f"Rule '{rule_id}' is already locked by '{existing.owner_id}' "
                        f"({remaining}s remaining)."
                    ),
                )

            token = uuid.uuid4().hex
            owner = owner_id or f"worker-{uuid.uuid4().hex[:8]}"
            expires_at = now + float(ttl_seconds)
            self._locks[rule_id] = LockEntry(
                rule_id=rule_id,
                owner_id=owner,
                token=token,
                acquired_at=now,
                expires_at=expires_at,
            )
            return LockAcquisitionResult(
                acquired=True,
                rule_id=rule_id,
                token=token,
                owner_id=owner,
                expires_at=expires_at,
            )

    def release_lock(self, rule_id: str, token: str | None = None) -> bool:
        """Releases an execution lock. If `token` is provided, requires exact token match."""
        now = self._clock()
        with self._mutex:
            self._evict_if_expired(rule_id, now)
            existing = self._locks.get(rule_id)
            if existing is None:
                return False
            if token is not None and existing.token != token:
                return False
            del self._locks[rule_id]
            return True

    def extend_lock(self, rule_id: str, token: str, ttl_seconds: float) -> bool:
        """Extends the TTL of an active lock if the caller holds the matching `token`."""
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be greater than 0.")
        now = self._clock()
        with self._mutex:
            self._evict_if_expired(rule_id, now)
            existing = self._locks.get(rule_id)
            if existing is None or existing.token != token:
                return False
            existing.expires_at = now + float(ttl_seconds)
            return True

    def is_locked(self, rule_id: str) -> bool:
        """Returns True if `rule_id` currently holds a non-expired lock."""
        now = self._clock()
        with self._mutex:
            self._evict_if_expired(rule_id, now)
            return rule_id in self._locks

    def get_lock_info(self, rule_id: str) -> dict[str, Any] | None:
        """Returns lock metadata for `rule_id`, or None if unlocked/expired."""
        now = self._clock()
        with self._mutex:
            self._evict_if_expired(rule_id, now)
            entry = self._locks.get(rule_id)
            if entry is None:
                return None
            return {
                "rule_id": entry.rule_id,
                "owner_id": entry.owner_id,
                "token": entry.token,
                "remaining_seconds": round(max(0.0, entry.expires_at - now), 3),
            }


# ---------------------------------------------------------------------------
# CronScheduler Orchestrator
# ---------------------------------------------------------------------------

class CronScheduler:
    """Evaluates scheduled rules, prevents overlapping runs, and logs executions."""

    def __init__(
        self,
        store: Any,
        lock_manager: ExecutionLockManager | None = None,
    ):
        self.store = store
        self.lock_manager = lock_manager or ExecutionLockManager()

    async def evaluate_due_rules(
        self,
        now: datetime | None = None,
        dry_run: bool = False,
        rule_id: str | None = None,
        executor: Callable[[dict[str, Any], datetime], Any] | None = None,
    ) -> dict[str, Any]:
        """Evaluates enabled scheduled rules at `now` (UTC) and executes due rules."""
        eval_utc = (
            now.astimezone(timezone.utc)
            if now is not None and now.tzinfo is not None
            else (now.replace(tzinfo=timezone.utc) if now is not None else datetime.now(timezone.utc))
        )

        if rule_id is not None:
            single = self.store.get_scheduled_rule(rule_id)
            rules = [single] if single and single.get("enabled") else []
        else:
            rules = self.store.list_scheduled_rules(enabled=True)

        results: list[dict[str, Any]] = []
        due_count = 0
        executed_count = 0
        skipped_overlap_count = 0
        failed_count = 0

        for rule in rules:
            rid = rule["id"]
            cron_str = rule["cron_expression"]
            tz_name = rule.get("timezone", "UTC")

            try:
                cron = parse_cron(cron_str)
                local_now = from_utc(eval_utc, tz_name)
                is_due = cron.matches(local_now)
                next_dt = cron.next_run_at(after=eval_utc, tz=tz_name)
            except Exception as exc:
                failed_count += 1
                results.append({
                    "rule_id": rid,
                    "rule_name": rule["name"],
                    "due": False,
                    "status": "error",
                    "error": str(exc),
                })
                continue

            if not is_due:
                results.append({
                    "rule_id": rid,
                    "rule_name": rule["name"],
                    "due": False,
                    "status": "not_due",
                    "next_run_at": next_dt.astimezone(timezone.utc).isoformat(),
                })
                continue

            due_count += 1
            scheduled_for_iso = eval_utc.isoformat()

            if dry_run:
                results.append({
                    "rule_id": rid,
                    "rule_name": rule["name"],
                    "due": True,
                    "status": "dry_run",
                    "scheduled_for": scheduled_for_iso,
                    "next_run_at": next_dt.astimezone(timezone.utc).isoformat(),
                    "locked": self.lock_manager.is_locked(rid),
                })
                continue

            prevent_overlap = bool(rule.get("prevent_overlap", True))
            lock_ttl = float(rule.get("lock_ttl_seconds", 300))
            lock_token: str | None = None

            if prevent_overlap:
                lock_res = self.lock_manager.acquire_lock(rid, ttl_seconds=lock_ttl)
                if not lock_res.acquired:
                    skipped_overlap_count += 1
                    started_iso = datetime.now(timezone.utc).isoformat()
                    self.store.log_scheduler_execution(
                        rule_id=rid,
                        rule_name=rule["name"],
                        cron_expression=cron_str,
                        timezone_name=tz_name,
                        scheduled_for=scheduled_for_iso,
                        started_at=started_iso,
                        finished_at=started_iso,
                        status="skipped_overlap",
                        lock_token=None,
                        duration_ms=0.0,
                        result={"skipped": True, "reason": lock_res.reason},
                        error_message=lock_res.reason,
                    )
                    results.append({
                        "rule_id": rid,
                        "rule_name": rule["name"],
                        "due": True,
                        "status": "skipped_overlap",
                        "reason": lock_res.reason,
                    })
                    continue
                lock_token = lock_res.token

            started_dt = datetime.now(timezone.utc)
            perf_start = time.perf_counter()
            status = "success"
            exec_result: dict[str, Any] = {}
            error_msg: str | None = None

            try:
                if executor is not None:
                    out = executor(rule, eval_utc)
                    if inspect.isawaitable(out):
                        out = await out
                    exec_result = out if isinstance(out, dict) else {"output": out}
                else:
                    exec_result = {
                        "dispatched": True,
                        "action_template": rule.get("action_template", {}),
                    }
                executed_count += 1
            except Exception as exc:
                status = "failed"
                failed_count += 1
                error_msg = str(exc)
                exec_result = {"dispatched": False}
                logger.error("Scheduled rule '%s' (%s) failed: %s", rule["name"], rid, exc)
            finally:
                if lock_token is not None:
                    self.lock_manager.release_lock(rid, token=lock_token)

            duration_ms = round((time.perf_counter() - perf_start) * 1000.0, 3)
            finished_iso = datetime.now(timezone.utc).isoformat()

            self.store.log_scheduler_execution(
                rule_id=rid,
                rule_name=rule["name"],
                cron_expression=cron_str,
                timezone_name=tz_name,
                scheduled_for=scheduled_for_iso,
                started_at=started_dt.isoformat(),
                finished_at=finished_iso,
                status=status,
                lock_token=lock_token,
                duration_ms=duration_ms,
                result=exec_result,
                error_message=error_msg,
            )
            self.store.update_scheduled_rule(
                rule_id=rid,
                last_run_at=started_dt.isoformat(),
                next_run_at=next_dt.astimezone(timezone.utc).isoformat(),
            )
            results.append({
                "rule_id": rid,
                "rule_name": rule["name"],
                "due": True,
                "status": status,
                "duration_ms": duration_ms,
                "next_run_at": next_dt.astimezone(timezone.utc).isoformat(),
                "error": error_msg,
            })

        return {
            "evaluated_at": eval_utc.isoformat(),
            "dry_run": dry_run,
            "total_rules": len(rules),
            "due_count": due_count,
            "executed_count": executed_count,
            "skipped_overlap_count": skipped_overlap_count,
            "failed_count": failed_count,
            "results": results,
        }
