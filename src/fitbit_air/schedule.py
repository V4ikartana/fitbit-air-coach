"""Install/uninstall the launchd agent that runs `fitbit-air sync` every 2 hours.

Code and data are separate: the job runs the code in `CODE_ROOT` (the repo) against the home in `FITBIT_AIR_HOME`.
"""
from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import time
from collections.abc import Callable
from datetime import date
from pathlib import Path

from .config import CODE_ROOT, SYNC_INTERVAL_SECONDS, Paths, unique_path

LABEL = "io.github.fitbit-air.sync"


class ScheduleError(RuntimeError):
    pass


def agents_dir_default() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def plist_path(agents_dir: Path | None = None) -> Path:
    return (agents_dir or agents_dir_default()) / f"{LABEL}.plist"


def build_plist(uv: str, paths: Paths) -> dict:
    return {
        "Label": LABEL,
        "ProgramArguments": [uv, "run", "--frozen", "--quiet", "--project", str(CODE_ROOT), "fitbit-air", "sync"],
        "StartInterval": SYNC_INTERVAL_SECONDS,
        "RunAtLoad": True,
        "WorkingDirectory": str(CODE_ROOT),
        "StandardOutPath": "/dev/null",  # sync.log has everything
        "StandardErrorPath": str(paths.logs_dir / "launchd.err.log"),
        "EnvironmentVariables": {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "FITBIT_AIR_HOME": str(paths.root),
        },
    }


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _plist_label(path: Path) -> str:
    try:
        label = plistlib.loads(path.read_bytes()).get("Label")
    except Exception:  # unreadable or not a plist: fall back to the file name
        label = None
    return label if isinstance(label, str) and label else path.stem


def retire_other_agents(paths: Paths, run: Callable, scan_dir: Path, stamp: str | None = None,
                        sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Boot out every `*fitbit-air.sync.plist` in `scan_dir` other than ours, check launchd no longer has it, and
    move it into <home>/launchd-backup/ (kept, never overwritten) so it never loads again. Raises ScheduleError,
    before moving anything, if launchd still reports the job loaded. Returns what was done, one line each."""
    steps: list[str] = []
    for old in sorted(scan_dir.glob("*fitbit-air.sync.plist")):
        if old.name == f"{LABEL}.plist":
            continue
        label = _plist_label(old)
        run(["launchctl", "bootout", f"{_domain()}/{label}"], check=False, capture_output=True)
        for attempt in range(5):  # bootout is asynchronous: give launchd a few seconds
            res = run(["launchctl", "print", f"{_domain()}/{label}"], check=False, capture_output=True)
            loaded = getattr(res, "returncode", None) == 0  # `print` succeeds only while the job is loaded
            if not loaded:
                break
            if attempt < 4:
                sleep(1)
        if loaded:
            raise ScheduleError(f"the old background sync {label} is still loaded after `launchctl bootout`; "
                                f"stop it with `launchctl bootout {_domain()}/{label}` and try again")
        backup = paths.root / "launchd-backup"
        backup.mkdir(parents=True, exist_ok=True)
        kept = backup / old.name
        if kept.exists():
            kept = unique_path(backup / f"{old.name}.{stamp or date.today().isoformat()}")
        shutil.move(old, kept)
        steps.append(f"stopped the old background sync {label} (its plist is kept as {kept})")
    return steps


def install(paths: Paths, run: Callable = subprocess.run, uv: str | None = None, target: Path | None = None,
            sleep: Callable[[float], None] = time.sleep, agents_dir: Path | None = None, retire: bool = True) -> Path:
    uv = uv or os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise RuntimeError("uv not found on PATH")
    target = target or plist_path(agents_dir)
    target.parent.mkdir(parents=True, exist_ok=True)
    paths.logs_dir.mkdir(parents=True, exist_ok=True)
    if retire:  # an old-label job would sync alongside this one (migrate retires them itself, to report it)
        retire_other_agents(paths, run, agents_dir or target.parent, sleep=sleep)
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True)  # replace if loaded
    target.write_bytes(plistlib.dumps(build_plist(uv, paths)))
    # bootout is asynchronous, so an immediate bootstrap can fail transiently; retry briefly.
    attempts = 5
    try:
        for attempt in range(attempts):
            try:
                run(["launchctl", "bootstrap", _domain(), str(target)], check=True)
                break
            except subprocess.CalledProcessError as exc:
                if attempt == attempts - 1:
                    raise ScheduleError(f"launchctl bootstrap failed ({exc}); background sync is now off. Fix the "
                                        "cause and run the command again") from exc
                sleep(1)
    except BaseException:
        target.unlink(missing_ok=True)  # never leave a plist that launchd would load at the next login
        raise
    return target


def uninstall(run: Callable = subprocess.run, target: Path | None = None, agents_dir: Path | None = None,
              paths: Paths | None = None, sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Stop and remove our job. With `paths`, also stop every other `*fitbit-air.sync` job (e.g. an old label) and
    keep its plist in <home>/launchd-backup/, so no background sync is left running."""
    target = target or plist_path(agents_dir)
    steps = retire_other_agents(paths, run, agents_dir or target.parent, sleep=sleep) if paths else []
    run(["launchctl", "bootout", f"{_domain()}/{LABEL}"], check=False, capture_output=True)
    target.unlink(missing_ok=True)
    return steps
