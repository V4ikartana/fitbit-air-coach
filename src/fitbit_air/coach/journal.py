"""Validation for journal entries and coach notes (the store only checks column names)."""
from __future__ import annotations

from datetime import date

RANGES = {"feeling": (1, 5), "energy": (1, 5), "soreness": (0, 10), "session_rpe": (0, 10), "alcohol_units": (0, 50)}
INT_FIELDS = frozenset({"feeling", "energy", "soreness", "session_rpe"})
NOTE_KINDS = ("plan", "target", "observation")
NOTE_STATUSES = ("active", "done", "dropped")
MAX_TEXT = 2000


class ValidationError(ValueError):
    """Bad user input for a journal entry or coach note; reported to Claude as a plain error."""


def _text(name: str, value: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be text")
    text = value.strip()
    if len(text) > MAX_TEXT:
        raise ValidationError(f"{name} is longer than {MAX_TEXT} characters")
    return text


def _iso(name: str, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value).isoformat()
    except (TypeError, ValueError):
        raise ValidationError(f"{name} must be a date YYYY-MM-DD, got {value!r}") from None


def validate_journal(fields: dict) -> dict:
    """Drop None values, range-check the rest and normalise types; at least one field must remain."""
    out: dict = {}
    for name, value in fields.items():
        if value is None:
            continue
        if name in RANGES:
            lo, hi = RANGES[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
                raise ValidationError(f"{name} must be a number from {lo} to {hi}, got {value!r}")
            if name in INT_FIELDS and value != int(value):
                raise ValidationError(f"{name} must be a whole number, got {value!r}")
            out[name] = int(value) if name in INT_FIELDS else float(value)
        elif name == "illness":
            if not isinstance(value, bool):
                raise ValidationError(f"illness must be true or false, got {value!r}")
            out[name] = int(value)
        elif name == "notes":
            text = _text("notes", value)
            if text:
                out[name] = text
        else:
            raise ValidationError(f"unknown journal field {name!r}")
    if not out:
        raise ValidationError("log at least one field (feeling, energy, soreness, session_rpe, illness, alcohol_units, "
                              "notes)")
    return out


def validate_note(kind: str, text: str, review_on: str | None) -> tuple[str, str, str | None]:
    """Check a new coach note; returns (kind, trimmed text, ISO review date or None)."""
    if kind not in NOTE_KINDS:
        raise ValidationError(f"kind must be one of {', '.join(NOTE_KINDS)}, got {kind!r}")
    body = _text("text", text)
    if not body:
        raise ValidationError("text is empty")
    return kind, body, _iso("review_on", review_on)


def validate_note_update(status: str | None = None, text: str | None = None,
                         review_on: str | None = None) -> dict:
    """Check a partial note update; returns only the fields to change."""
    changes: dict = {}
    if status is not None:
        if status not in NOTE_STATUSES:
            raise ValidationError(f"status must be one of {', '.join(NOTE_STATUSES)}, got {status!r}")
        changes["status"] = status
    if text is not None:
        body = _text("text", text)
        if not body:
            raise ValidationError("text is empty")
        changes["text"] = body
    if review_on is not None:
        changes["review_on"] = _iso("review_on", review_on)
    if not changes:
        raise ValidationError("nothing to update: give status, text or review_on")
    return changes
