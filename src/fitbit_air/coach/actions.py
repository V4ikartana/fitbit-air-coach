"""Coach write tools: local journal, coach notes, profile edits and a manual refresh. Nothing is written to Google."""
from __future__ import annotations

import dataclasses
import logging
import os
import subprocess
import sys

from ..analysis import runner
from ..analysis.profile import Profile, profile_path, set_value
from ..lock import SyncLock
from .journal import NOTE_STATUSES, validate_journal, validate_note, validate_note_update
from .tools import Ctx, ToolError, coach_tool, date_range, parse_day

subprocess_run = subprocess.run  # replaced in tests
log = logging.getLogger(__name__)
# plugins_enabled is deliberately not settable here: plugins run arbitrary code, so only the user may switch them on
# (by editing the profile file or running `fitbit-air profile set` themselves), never the model or a prompt injection.
PROFILE_KEYS = tuple(f.name for f in dataclasses.fields(Profile) if f.name != "plugins_enabled")
SYNC_TIMEOUT_S = 900
OUTPUT_TAIL = 4000


@coach_tool
def log_journal(ctx: Ctx, date: str | None = None, feeling: int | None = None, energy: int | None = None,
                soreness: int | None = None, session_rpe: int | None = None, illness: bool | None = None,
                alcohol_units: float | None = None, notes: str | None = None) -> dict:
    day = parse_day(date, ctx.today)
    fields = validate_journal({"feeling": feeling, "energy": energy, "soreness": soreness,
                               "session_rpe": session_rpe, "illness": illness, "alcohol_units": alcohol_units,
                               "notes": notes})
    return {"entry": ctx.store.add_journal(day, fields)}


@coach_tool
def get_journal(ctx: Ctx, start: str | None = None, end: str | None = None, days: int | None = None) -> dict:
    s, e = date_range(start, end, ctx.today, days=days)
    return {"start": s, "end": e, "entries": ctx.store.journal_between(s, e)}


@coach_tool
def save_coach_note(ctx: Ctx, kind: str, text: str, review_on: str | None = None) -> dict:
    return {"note": ctx.store.add_note(*validate_note(kind, text, review_on))}


@coach_tool
def get_coach_notes(ctx: Ctx, status: str | None = "active", due_by: str | None = None) -> dict:
    if status is not None and status not in NOTE_STATUSES:
        raise ToolError(f"status must be one of {', '.join(NOTE_STATUSES)} (or null for every note), got {status!r}")
    due = None if due_by is None else parse_day(due_by, ctx.today, "due_by")
    return {"today": ctx.today.isoformat(), "notes": ctx.store.notes(status=status, due_by=due)}


@coach_tool
def update_coach_note(ctx: Ctx, id: int, status: str | None = None, text: str | None = None,
                      review_on: str | None = None) -> dict:
    note = ctx.store.update_note(id, validate_note_update(status=status, text=text, review_on=review_on))
    if note is None:
        raise ToolError(f"unknown note id {id}", "call get_coach_notes with status=None to list every note")
    return {"note": note}


@coach_tool
def set_profile(ctx: Ctx, key: str, value: str | int | float | list[str]) -> dict:
    keys_hint = f"valid keys: {', '.join(PROFILE_KEYS)}"
    if key not in PROFILE_KEYS:
        raise ToolError(f"unknown key {key!r}", keys_hint)
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # JSON clients may send 190.0 for an integer key such as hrmax_override
    raw = ", ".join(map(str, value)) if isinstance(value, list) else str(value)
    path = profile_path(ctx.paths)
    try:
        profile = set_value(path, key, raw)
    except ValueError as exc:  # a bad value (or a damaged profile file): a user error, not an internal one
        raise ToolError(str(exc), f"check {path}") from None
    with SyncLock(ctx.paths.lock_file) as acquired:
        if not acquired:
            analysis = ("a sync is running; the new value is used by the next analysis (the sync's own, if it hasn't "
                        "started analysing yet; otherwise run analyze)")
        else:
            try:
                analysis = str(runner.run_analysis(ctx.store, ctx.paths))
            except Exception as exc:  # noqa: BLE001 - the profile is saved; report the failed re-analysis
                log.exception("analysis after set_profile failed")
                analysis = f"failed: {exc!r} (profile saved; run `fitbit-air analyze` or refresh_data)"
    return {"profile": dataclasses.asdict(profile), "analysis": analysis}


@coach_tool
def refresh_data(ctx: Ctx) -> dict:
    lock = SyncLock(ctx.paths.lock_file)
    if not lock.acquire():
        return {"status": "already_running", "hint": "a sync is in progress; try again in a few minutes"}
    lock.release()  # the sync subprocess takes the lock itself
    try:
        proc = subprocess_run([sys.executable, "-m", "fitbit_air", "sync"], capture_output=True, text=True,
                              timeout=SYNC_TIMEOUT_S, env=dict(os.environ), stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired as exc:
        raise ToolError("sync took longer than 15 minutes", f"check {ctx.paths.logs_dir / 'sync.log'}") from exc
    output = ((proc.stdout or "") + (proc.stderr or ""))[-OUTPUT_TAIL:]
    if "Sync already running" in output:
        return {"status": "already_running", "hint": "a sync started meanwhile; try again in a few minutes"}
    if proc.returncode == 2:
        raise ToolError("Google login needs attention",
                        "run `uv run fitbit-air auth` (in OAuth Testing mode the login expires after 7 days)")
    return {"status": "ok" if proc.returncode == 0 else "partial", "exit_code": proc.returncode,
            "output": output, "analysis": ctx.store.last_analysis()}
