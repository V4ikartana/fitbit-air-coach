"""Create a user home from a template, and migrate an old repo-root home."""
from __future__ import annotations

import filecmp
import hashlib
import json
import os
import shlex
import shutil
import sqlite3
import subprocess
import time
from collections.abc import Callable
from datetime import date, datetime
from importlib.resources import files
from importlib.resources.abc import Traversable
from pathlib import Path

from . import schedule
from .config import CODE_ROOT, MIGRATE_STATE_FILE, Paths, unique_path
from .lock import SyncLock

HOME_FOLDERS = ("config", "data", "secrets", "plugins")
TEMPLATES = ("minimal", "runner-10k", "sleep-recovery")


def _copy_tree(src: Traversable, dst: Path, created: list[Path]) -> None:
    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            _copy_tree(item, target, created)
        elif not target.exists():
            target.write_bytes(item.read_bytes())
            created.append(target)


def init_home(paths: Paths, template: str = "minimal") -> list[Path]:
    """Create the home layout and copy the template's config; existing files are never overwritten."""
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; choose one of {', '.join(TEMPLATES)}")
    root = paths.root
    if root.is_dir() and any(root.iterdir()) and not any((root / d).exists() for d in HOME_FOLDERS):
        raise ValueError(f"{root} is not empty and doesn't look like a fitbit-air home (no config, data, secrets or "
                         "plugins folder); choose an empty folder with --home")
    for d in (paths.data_dir, paths.root / "secrets", paths.config_dir, paths.plugins_dir):
        d.mkdir(parents=True, exist_ok=True)
    for d in (paths.root, paths.root / "secrets"):
        os.chmod(d, 0o700)
    created: list[Path] = []
    _copy_tree(files("fitbit_air.templates").joinpath(template, "config"), paths.config_dir, created)
    return created


# --- migrate an old repo-root home --------------------------------------------------------------------------------

class MigrateError(RuntimeError):
    pass


STATE_FILE = MIGRATE_STATE_FILE
_DB_FILES = {"health.db", "health.db-wal", "health.db-shm", "health.db-journal", ".sync.lock", "profile.toml"}


def _system_run(*args, **kwargs):
    """The real subprocess.run; migrate's default `run`, replaced in tests."""
    return subprocess.run(*args, **kwargs)


def _state_file(dst: Paths) -> Path:
    return dst.root / STATE_FILE


def _state(dst: Paths) -> dict:
    f = _state_file(dst)
    return json.loads(f.read_text()) if f.exists() else {}


def _save_state(dst: Paths, state: dict) -> None:
    tmp = _state_file(dst).with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(_state_file(dst))  # atomic: a crash never leaves a half-written state file


def _counts(db: Path) -> dict[str, int]:
    con = sqlite3.connect(db)
    try:
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]
        return {t: con.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0] for t in tables}
    finally:
        con.close()


def _integrity(db: Path) -> str:
    con = sqlite3.connect(db)
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def _backup_db(src_db: Path, dst_db: Path) -> None:
    """A consistent copy through the sqlite3 backup API (includes what is still in the WAL file)."""
    s, d = sqlite3.connect(src_db), sqlite3.connect(dst_db)
    try:
        s.backup(d)
    finally:
        s.close()
        d.close()


def _tree(src_dir: Path, dst_dir: Path, skip_top: set[str] = frozenset()) -> list[tuple[Path, Path]]:
    if not src_dir.is_dir():
        return []
    return [(f, dst_dir / f.relative_to(src_dir)) for f in sorted(src_dir.rglob("*"))
            if f.is_file() and not (f.parent == src_dir and f.name in skip_top)]


def _copy_plan(src: Paths, dst: Paths) -> list[tuple[Path, Path]]:
    """Every file to copy except the database: data/ (minus the db, its side files and the lock), secrets/,
    the profile (data/profile.toml -> config/profile.toml) and, if the source has them, config/ and plugins/."""
    pairs = _tree(src.data_dir, dst.data_dir, _DB_FILES)
    pairs += _tree(src.root / "secrets", dst.root / "secrets")
    pairs += _tree(src.config_dir, dst.config_dir)
    pairs += _tree(src.plugins_dir, dst.plugins_dir)
    old_profile = src.data_dir / "profile.toml"
    if old_profile.is_file() and not src.profile_file.is_file():
        pairs.append((old_profile, dst.profile_file))
    return pairs


def _unique(p: Path, suffix: str) -> Path:
    """`<name>.<suffix>` next to `p`, with -2, -3, ... added until nothing exists there."""
    return unique_path(p.with_name(f"{p.name}.{suffix}"))


def _aside(p: Path, suffix: str) -> Path:
    """`p` renamed to `<name>.<suffix>`, never replacing anything that already exists (adds -2, -3, ...)."""
    target = _unique(p, suffix)
    p.rename(target)
    return target


def _rename_aside(p: Path, stamp: str) -> Path:
    return _aside(p, f"migrated-{stamp}")


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _keep_aside(state: dict, kept: Path, what: str, steps: list[str]) -> None:
    state.setdefault("set_aside", []).append(str(kept))
    steps.append(f"kept {what} as {kept} (it differed from the source and was not migrate's own copy)")


def _copy_files(src: Paths, dst: Paths, state: dict, stamp: str, steps: list[str]) -> None:
    """Copy everything but the db. A target file that differs from the source is set aside first, unless it is
    exactly what an earlier attempt of this migration copied there (then the newer source simply replaces it)."""
    copied = state.setdefault("copied_files", {})  # target path -> sha256 of what migrate wrote there
    for s_file, d_file in _copy_plan(src, dst):
        if (d_file.exists() and not filecmp.cmp(s_file, d_file, shallow=False)
                and copied.get(str(d_file)) != _sha(d_file)):
            _keep_aside(state, _aside(d_file, f"before-migrate-{stamp}"), f"your existing {d_file}", steps)
        d_file.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(s_file, d_file)
        copied[str(d_file)] = _sha(d_file)
    for d in (dst.root / "secrets", dst.root):
        if d.is_dir():
            os.chmod(d, 0o700)
    _save_state(dst, state)


def _db_fingerprint(db: Path) -> str:
    """sha256 of the db file plus any non-empty -wal file: any write to the copy, even an update that keeps the row
    counts, changes it (uncheckpointed writes live in the -wal file)."""
    h = hashlib.sha256()
    for f in (db, Path(f"{db}-wal")):
        if f.exists() and f.stat().st_size:
            h.update(f.name.encode())
            h.update(_sha(f).encode())
    return h.hexdigest()


def _keep_changed_db(dst: Paths, state: dict, stamp: str, steps: list[str]) -> None:
    """Before a re-copy, keep a target db that changed since migrate last copied it (e.g. something wrote to the
    copy after a failed attempt): a consistent backup next to it, so the re-copy loses nothing."""
    if not dst.db.exists():
        return
    try:
        changed = _counts(dst.db) != state.get("db_counts") or _db_fingerprint(dst.db) != state.get("db_fingerprint")
    except sqlite3.Error:
        changed = True
    if not changed:
        return
    kept = _unique(dst.db, f"before-migrate-{stamp}")
    try:
        _backup_db(dst.db, kept)
    except sqlite3.Error:  # unreadable as a database: keep the raw files instead
        for side in ("", "-wal", "-shm"):
            if Path(f"{dst.db}{side}").exists():
                shutil.copy2(f"{dst.db}{side}", f"{kept}{side}")
    _keep_aside(state, kept, f"the database already in {dst.root}", steps)
    _save_state(dst, state)


def _copy_and_verify(src: Paths, dst: Paths, state: dict, stamp: str, steps: list[str]) -> None:
    _copy_files(src, dst, state, stamp, steps)
    _keep_changed_db(dst, state, stamp, steps)
    for _ in range(3):  # a write from the coach during the copy changes the counts; copy again
        _backup_db(src.db, dst.db)
        state["db_counts"] = _counts(dst.db)
        _save_state(dst, state)
        ok = _integrity(dst.db)
        state["db_fingerprint"] = _db_fingerprint(dst.db)  # after migrate's own reads, which don't change it
        _save_state(dst, state)
        if ok != "ok":
            raise MigrateError(f"verification failed: the copy's integrity check says {ok!r}; nothing was switched")
        src_counts = _counts(src.db)
        if src_counts == state["db_counts"]:
            state["src_counts"] = src_counts
            return
    raise MigrateError("verification failed: the copied database's row counts differ; nothing was switched")


def _grown_tables(src: Paths, state: dict) -> list[str]:
    """Tables of the old db with more rows than when it was copied (a client still writing to the old home)."""
    before = state.get("src_counts")
    if not before or not src.db.exists():
        return []
    try:
        now = _counts(src.db)
    except sqlite3.Error:
        return []
    return sorted(t for t, n in now.items() if n > before.get(t, 0))


def migrate_home(src: Paths, dst: Paths, run: Callable | None = None, agents_dir: Path | None = None,
                 uv: str | None = None, claude: str | None = None, today: str | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> list[str]:
    """Move an old repo-root home to `dst`: copy, verify, switch the background sync and Claude Code, then rename
    the old data/ and secrets/ to *.migrated-<date>. Nothing is ever deleted.

    Resumable: progress is kept in <dst>/.migrate-state.json, and re-running after any failure carries on from the
    step that failed. Every system command goes through `run`. Returns the steps done or to do by hand.
    """
    run = run or _system_run
    stamp = today or date.today().isoformat()
    steps: list[str] = []
    state = _state(dst)
    if state.get("done"):
        return [f"already migrated to {dst.root}; nothing to do"]
    src_root, dst_root = src.root.resolve(), dst.root.resolve()
    if src_root == dst_root:
        raise MigrateError(f"the source and the target are the same folder ({src.root})")
    if dst_root.is_relative_to(src_root / "data") or dst_root.is_relative_to(src_root / "secrets"):
        raise MigrateError(f"the target {dst.root} is inside the folders being migrated")
    if dst_root.is_relative_to(CODE_ROOT.resolve()):
        raise MigrateError(f"the target {dst.root} is inside the code folder {CODE_ROOT}; choose a home outside it")
    if state.get("source") and state["source"] != str(src.root):
        raise MigrateError(f"{dst.root} has an unfinished migration from {state['source']}; finish that one first")
    uv = uv or os.environ.get("UV") or shutil.which("uv")
    if not uv:
        raise MigrateError("uv not found on PATH")

    if not state.get("switched"):
        if not src.db.exists():
            raise MigrateError(f"nothing to migrate: no database at {src.db}")
        lock = SyncLock(src.lock_file)
        if not lock.acquire():
            raise MigrateError("a sync is running; try again in a few minutes")
        try:
            _switch(src, dst, state, run, agents_dir, uv, claude, stamp, steps, sleep)
        except schedule.ScheduleError as exc:
            raise MigrateError(str(exc)) from exc
        finally:
            lock.release()  # released before data/ (which holds the lock file) is renamed

    steps.append("Claude Desktop: quit it (Cmd+Q), then run "
                 f"FITBIT_AIR_HOME={shlex.quote(str(dst.root))} python3 "
                 f"{shlex.quote(str(CODE_ROOT / 'scripts' / 'register_claude_desktop.py'))}")
    grown = _grown_tables(src, state)
    if grown:
        steps.append(f"warning: the old database gained rows after the copy, in {', '.join(grown)}; they are not in "
                     f"{dst.root} but stay in {src.root}/data.migrated-{stamp} (a Claude client still using the "
                     "old home wrote them)")
    renamed = state.setdefault("renamed", {})
    for name in ("data", "secrets"):
        p = src.root / name
        if name not in renamed and p.exists():
            renamed[name] = str(_rename_aside(p, stamp))
            _save_state(dst, state)
    state["done"] = True
    _save_state(dst, state)
    if renamed:
        steps.append(f"kept the old folders as {', '.join(Path(v).name for v in renamed.values())} in {src.root} "
                     "(delete them yourself once you are happy)")
    return steps


def _switch(src: Paths, dst: Paths, state: dict, run: Callable, agents_dir: Path | None, uv: str,
            claude: str | None, stamp: str, steps: list[str], sleep: Callable[[float], None]) -> None:
    """The part that runs under the sync lock: copy, verify, launchd, Claude Code."""
    if (dst.db.exists() or dst.db.is_symlink()) and not _state_file(dst).exists():
        raise MigrateError(f"{dst.root} already has a database; refusing to overwrite it")
    dst.root.mkdir(parents=True, exist_ok=True)
    os.chmod(dst.root, 0o700)
    if not state:
        state.update(source=str(src.root), started=datetime.now().isoformat(timespec="seconds"))
        _save_state(dst, state)

    if not state.get("scheduled"):
        # Until the background sync is switched, the old home is still the live one, so (re)copy it now, under
        # the lock: a resume never switches to a copy older than the source.
        dst.data_dir.mkdir(parents=True, exist_ok=True)
        dst_lock = SyncLock(dst.lock_file)
        if not dst_lock.acquire():
            raise MigrateError(f"a sync into {dst.root} is running; try again in a few minutes")
        try:
            _copy_and_verify(src, dst, state, stamp, steps)
        finally:
            dst_lock.release()
        state["copied"] = state["verified"] = True
        _save_state(dst, state)
        steps.append(f"copied data, secrets and profile to {dst.root}")
        steps.append("verified the copy (integrity check and identical row counts per table)")

        agents = agents_dir or schedule.agents_dir_default()
        steps += schedule.retire_other_agents(dst, run, agents, stamp, sleep=sleep)
        schedule.install(dst, run=run, uv=uv, agents_dir=agents, sleep=sleep, retire=False)
        state["scheduled"] = True
        _save_state(dst, state)
        steps.append(f"background sync now runs the code in {CODE_ROOT} with its data in {dst.root}")

    spec = {"type": "stdio", "command": uv,
            "args": ["--directory", str(CODE_ROOT), "run", "--frozen", "fitbit-air", "mcp"],
            "env": {"FITBIT_AIR_HOME": str(dst.root)}}
    add = ["mcp", "add-json", "-s", "user", "fitbit-coach", json.dumps(spec)]  # one JSON argument: unambiguous
    if state.get("claude_code"):
        steps.append("Claude Code registration already updated")
    elif claude:
        for scope in ("user", "local"):
            run([claude, "mcp", "remove", "fitbit-coach", "-s", scope], check=False, capture_output=True)
        run([claude, *add], check=True)
        state["claude_code"] = True
        _save_state(dst, state)
        steps.append("updated the Claude Code registration")
    else:
        steps.append("Claude Code: run  claude mcp remove fitbit-coach -s user; "
                     "claude mcp remove fitbit-coach -s local; " + shlex.join(["claude", *add])
                     + "  (if `claude` is not on your PATH, the Claude Desktop app bundles it)")
    steps.append("if you registered fitbit-coach inside a project, check `claude mcp list` there")
    state["switched"] = True
    _save_state(dst, state)
