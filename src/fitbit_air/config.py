"""Paths and constants."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parents[2]  # the repo (code); never written to in the new layout
DEFAULT_HOME = Path("~/.fitbit-air")             # the user's private home: data, secrets, config, plugins
MIGRATE_STATE_FILE = ".migrate-state.json"       # in the target home while `home migrate` is unfinished

API_BASE = "https://health.googleapis.com/v4"
_SCOPE_PREFIX = "https://www.googleapis.com/auth/googlehealth."
SCOPES = tuple(
    _SCOPE_PREFIX + s
    for s in (
        "activity_and_fitness.readonly",
        "health_metrics_and_measurements.readonly",
        "sleep.readonly",
        "profile.readonly",
        "irn.readonly",
        "settings.readonly",
    )
)

KEYRING_SERVICE = "fitbit-air-mcp"
KEYRING_USER = "google-oauth"
OAUTH_PORT = 8765

HISTORY_FLOOR = datetime(2015, 1, 1, tzinfo=timezone.utc)
OVERLAP = timedelta(days=3)  # every sync re-fetches this much recent history
DEEP_RESCAN = timedelta(days=14)  # once a day, re-fetch this much (late-arriving or revised points)
DEEP_RESCAN_EVERY = timedelta(hours=24)
MAX_BACKFILL_WINDOW_DAYS = 365  # cap for the gallop over consecutive empty backfill windows
REQUESTS_PER_SECOND = 2.0
SYNC_INTERVAL_SECONDS = 7200


@dataclass(frozen=True)
class Paths:
    root: Path
    legacy: bool = False  # True while an old repo-root home is still in use (see `fitbit-air home migrate`)

    @property
    def config_dir(self) -> Path:
        return self.root / "config"

    @property
    def plugins_dir(self) -> Path:
        return self.root / "plugins"

    @property
    def profile_file(self) -> Path:
        return self.config_dir / "profile.toml"

    @property
    def data_dir(self) -> Path:
        return self.root / "data"

    @property
    def db(self) -> Path:
        return self.data_dir / "health.db"

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / "logs"

    @property
    def lock_file(self) -> Path:
        return self.data_dir / ".sync.lock"

    @property
    def client_secret(self) -> Path:
        return self.root / "secrets" / "client_secret.json"


def unique_path(p: Path) -> Path:
    """`p`, or `p` with -2, -3, ... added to its name, whichever does not exist yet (never replace a file)."""
    target, n = p, 2
    while target.exists() or target.is_symlink():
        target, n = p.with_name(f"{p.name}-{n}"), n + 1
    return target


def resolve_home() -> tuple[Path, bool]:
    """FITBIT_AIR_HOME, else ~/.fitbit-air; an old repo-root home with data keeps working until migrated."""
    env = os.environ.get("FITBIT_AIR_HOME")
    if env:
        root = Path(env).expanduser()
        return root, root.resolve() == CODE_ROOT.resolve()
    home = DEFAULT_HOME.expanduser()
    # Judge by data, not by the folder: a stray or empty ~/.fitbit-air must not move us off the live data.
    if (CODE_ROOT / "data" / "health.db").exists() and (
            not (home / "data" / "health.db").exists() or _migrating_into(home)):
        return CODE_ROOT, True
    return home, False


def _migrating_into(home: Path) -> bool:
    """True while `home migrate` into `home` has copied data but not yet switched the background sync: until then
    the old repo-root home is the live one, and the copy is not used."""
    f = home / MIGRATE_STATE_FILE
    if not f.exists():
        return False
    try:
        state = json.loads(f.read_text())
    except (OSError, ValueError):
        return True  # unreadable: stay on the old, known-good home
    return not (isinstance(state, dict) and (state.get("scheduled") or state.get("done")))


def get_paths() -> Paths:
    root, legacy = resolve_home()
    return Paths(root, legacy=legacy)
