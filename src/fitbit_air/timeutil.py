"""Parsing helpers for the time structures used by the Google Health API."""
from __future__ import annotations

import re
from datetime import datetime, timezone

UTC = timezone.utc
_TS = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def parse_ts(value: str) -> datetime:
    """Parse an RFC-3339 timestamp (up to nanosecond precision) into an aware UTC datetime."""
    m = _TS.match(value)
    if not m:
        raise ValueError(f"not an RFC-3339 timestamp: {value!r}")
    base, frac, tz = m.groups()
    frac = (frac or "")[:7]  # '.' plus at most 6 digits
    tz = "+00:00" if tz == "Z" else tz
    return datetime.fromisoformat(base + frac + tz).astimezone(UTC)


def require_aware(moment: datetime) -> datetime:
    """Return `moment` unchanged, or raise if it has no timezone (naive datetimes are ambiguous)."""
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"naive datetime not allowed: {moment!r}")
    return moment


def iso_utc(moment: datetime) -> str:
    return require_aware(moment).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def canonical_ts(value: str) -> str:
    """Normalise an RFC-3339 timestamp to UTC, 'YYYY-MM-DDTHH:MM:SSZ' (plus microseconds only if non-zero)."""
    moment = parse_ts(value)
    fmt = "%Y-%m-%dT%H:%M:%S.%fZ" if moment.microsecond else "%Y-%m-%dT%H:%M:%SZ"
    return moment.strftime(fmt)


def date_str(d: dict) -> str:
    """Google `Date` {year, month, day} -> 'YYYY-MM-DD'."""
    return f"{d['year']:04d}-{d['month']:02d}-{d['day']:02d}"


def civil_str(c: dict) -> str:
    """Google `CivilDateTime` {date, time} -> 'YYYY-MM-DDTHH:MM:SS' (zero fields may be omitted in JSON)."""
    t = c.get("time") or {}
    return f"{date_str(c['date'])}T{t.get('hours', 0):02d}:{t.get('minutes', 0):02d}:{t.get('seconds', 0):02d}"


def minutes_between(start: str, end: str) -> float:
    return (parse_ts(end) - parse_ts(start)).total_seconds() / 60


def parse_duration_s(value: str) -> float:
    """Google `Duration` JSON ('1800s') -> seconds."""
    if not value.endswith("s"):
        raise ValueError(f"not a duration: {value!r}")
    return float(value[:-1])


def num(value) -> float | None:
    """API numbers arrive as JSON numbers or int64-as-string; None stays None."""
    return None if value is None else float(value)
