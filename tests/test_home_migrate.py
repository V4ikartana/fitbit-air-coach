"""`home migrate`: copy, verify, switch, then rename (never delete). Every system call goes through a fake `run`,
and every path (homes, LaunchAgents) is a temporary one."""
import json
import plistlib
import sqlite3

import pytest

from fitbit_air import home
from fitbit_air.config import CODE_ROOT, Paths
from fitbit_air.home import MigrateError, migrate_home
from fitbit_air.lock import SyncLock
from fitbit_air.store import Store

NEW_PLIST = "io.github.fitbit-air.sync.plist"
OLD_PLIST = "com.example.fitbit-air.sync.plist"


def _legacy(tmp_path):
    src = Paths(tmp_path / "repo", legacy=True)
    s = Store(src.db)
    s.save_snapshot("profile", {"x": 1})
    s.close()
    (src.data_dir / "profile.toml").write_text('sex = "male"\n')
    (src.root / "secrets").mkdir()
    (src.root / "secrets" / "client_secret.json").write_text("{}")
    (src.data_dir / "logs").mkdir(exist_ok=True)
    (src.data_dir / "logs" / "sync.log").write_text("log")
    return src


class Run:
    """A fake subprocess.run: records every call, and raises when `fail(args)` is true."""

    def __init__(self, fail=None):
        self.calls: list[list[str]] = []
        self.fail = fail

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if self.fail and self.fail(args):
            raise RuntimeError(f"injected failure: {args[:3]}")


def _agents(tmp_path, old=True):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir(exist_ok=True)
    if old:
        (agents / OLD_PLIST).write_bytes(plistlib.dumps({"Label": "com.example.fitbit-air.sync"}))
    return agents


def _migrate(src, dst, run, agents, **kw):
    kw.setdefault("today", "2026-10-17")
    kw.setdefault("sleep", lambda s: None)
    return migrate_home(src, dst, run=run, agents_dir=agents, uv="/usr/bin/uv", claude=kw.pop("claude", None), **kw)


def _assert_untouched(src, agents):
    assert (src.root / "data").is_dir() and src.db.exists() and (src.root / "secrets").is_dir()
    assert not list(src.root.glob("*.migrated-*"))


# --- the plan's tests ---------------------------------------------------------------------------------------------

def test_migrate_copies_verifies_switches_and_renames(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    calls = []
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    (agents / "com.example.fitbit-air.sync.plist").write_text("old")
    steps = migrate_home(src, dst, run=lambda *a, **k: calls.append(a[0]), agents_dir=agents, uv="/usr/bin/uv",
                         claude=None, today="2026-10-17")
    assert (dst.db).exists() and dst.profile_file.read_text() == 'sex = "male"\n'
    assert (dst.root / "secrets" / "client_secret.json").exists()
    con = sqlite3.connect(dst.db)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert not (src.root / "data").exists() and (src.root / "data.migrated-2026-10-17").exists()
    assert not (agents / "com.example.fitbit-air.sync.plist").exists()
    assert (agents / "io.github.fitbit-air.sync.plist").exists()
    assert any("bootstrap" in c for c in calls)
    assert "claude mcp add" in " ".join(steps) and "register_claude_desktop.py" in " ".join(steps)
    assert json.loads((dst.root / ".migrate-state.json").read_text())["done"] is True


def test_migrate_refuses_while_syncing_or_into_existing_home(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    with SyncLock(src.lock_file):
        with pytest.raises(MigrateError, match="sync is running"):
            migrate_home(src, dst, run=lambda *a, **k: None, agents_dir=tmp_path, uv="/usr/bin/uv", claude=None)
    other = Paths(tmp_path / "other")
    Store(other.db).close()
    with pytest.raises(MigrateError, match="already has"):
        migrate_home(src, other, run=lambda *a, **k: None, agents_dir=tmp_path, uv="/usr/bin/uv", claude=None)


def test_migrate_resumes_after_partial_failure(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")

    def boom(*a, **k):
        raise RuntimeError("launchctl failed")
    with pytest.raises(RuntimeError):
        migrate_home(src, dst, run=boom, agents_dir=tmp_path, uv="/usr/bin/uv", claude=None)
    assert (src.root / "data").exists()  # nothing renamed before every switch succeeded
    migrate_home(src, dst, run=lambda *a, **k: None, agents_dir=tmp_path, uv="/usr/bin/uv", claude=None,
                 today="2026-10-17")
    assert (src.root / "data.migrated-2026-10-17").exists()


# --- what is copied, and how -------------------------------------------------------------------------------------

def test_copy_uses_backup_so_wal_content_is_included(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    s = Store(src.db)  # WAL mode; keep it open so the last write may still sit in the -wal file
    s.save_snapshot("profile", {"x": 2})
    try:
        _migrate(src, dst, Run(), _agents(tmp_path))
    finally:
        s.close()
    con = sqlite3.connect(dst.db)
    assert con.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    con.close()
    assert home._counts(dst.db) == home._counts(src.root / "data.migrated-2026-10-17" / "health.db")


def test_copies_the_rest_of_data_but_not_lock_or_db_side_files(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    (src.data_dir / "dashboard").mkdir()
    (src.data_dir / "dashboard" / "snapshot.json").write_text("{}")
    (src.data_dir / "notes.txt").write_text("keep me")
    _migrate(src, dst, Run(), _agents(tmp_path))
    assert (dst.data_dir / "dashboard" / "snapshot.json").read_text() == "{}"
    assert (dst.data_dir / "notes.txt").read_text() == "keep me"
    assert (dst.data_dir / "logs" / "sync.log").read_text() == "log"
    assert not (dst.data_dir / "profile.toml").exists()  # it moved to config/
    assert not (dst.data_dir / "health.db-wal").exists() or (dst.data_dir / "health.db-wal").stat().st_size == 0
    assert (dst.root.stat().st_mode & 0o777) == 0o700 and ((dst.root / "secrets").stat().st_mode & 0o777) == 0o700


def test_source_profile_wins_over_a_template_profile_which_is_kept_aside(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    real = 'sex = "male"\nrace_distance_km = 10.0\nrace_date = "2030-05-01"\n'
    (src.data_dir / "profile.toml").write_text(real)
    home.init_home(dst, "minimal")  # someone ran `fitbit-air init` first
    template = dst.profile_file.read_text()
    assert template != real
    steps = _migrate(src, dst, Run(), _agents(tmp_path))
    assert dst.profile_file.read_text() == real
    assert (dst.config_dir / "profile.toml.before-migrate-2026-10-17").read_text() == template
    assert sum("profile.toml.before-migrate-2026-10-17" in s for s in steps) == 1


def test_other_differing_target_files_are_kept_aside_too(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    (dst.root / "secrets").mkdir(parents=True)
    (dst.root / "secrets" / "client_secret.json").write_text('{"other": 1}')
    _migrate(src, dst, Run(), _agents(tmp_path))
    assert (dst.root / "secrets" / "client_secret.json").read_text() == "{}"
    assert (dst.root / "secrets" / "client_secret.json.before-migrate-2026-10-17").read_text() == '{"other": 1}'


def test_an_identical_file_in_the_target_is_fine(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    dst.config_dir.mkdir(parents=True)
    dst.profile_file.write_text('sex = "male"\n')
    _migrate(src, dst, Run(), _agents(tmp_path))
    assert json.loads((dst.root / ".migrate-state.json").read_text())["done"] is True


def test_refuses_without_a_source_database_or_into_itself(tmp_path):
    with pytest.raises(MigrateError, match="nothing to migrate"):
        _migrate(Paths(tmp_path / "empty"), Paths(tmp_path / "home"), Run(), _agents(tmp_path))
    assert not (tmp_path / "empty").exists()  # not even the lock's folder was created
    src = _legacy(tmp_path)
    with pytest.raises(MigrateError, match="same folder"):
        _migrate(src, Paths(src.root), Run(), _agents(tmp_path))
    with pytest.raises(MigrateError, match="inside"):
        _migrate(src, Paths(src.data_dir / "new"), Run(), _agents(tmp_path))


# --- the switches ------------------------------------------------------------------------------------------------

def test_launchd_switch_unloads_old_labels_by_scanning_and_keeps_their_plists(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    agents = _agents(tmp_path)
    (agents / "com.other.thing.plist").write_text("unrelated")
    run = Run()
    _migrate(src, dst, run, agents)
    old_bootout = ["launchctl", "bootout", f"gui/{home.os.getuid()}/com.example.fitbit-air.sync"]
    assert run.calls.count(old_bootout) == 1  # the old agents are retired once, not again inside install
    assert (agents / "com.other.thing.plist").read_text() == "unrelated"
    assert not (agents / OLD_PLIST).exists()
    assert (dst.root / "launchd-backup" / OLD_PLIST).exists()  # moved, not deleted
    new = plistlib.loads((agents / NEW_PLIST).read_bytes())
    assert new["EnvironmentVariables"]["FITBIT_AIR_HOME"] == str(dst.root)
    assert new["WorkingDirectory"] == str(CODE_ROOT) and new["ProgramArguments"][0] == "/usr/bin/uv"
    assert ["launchctl", "bootstrap", f"gui/{home.os.getuid()}", str(agents / NEW_PLIST)] in run.calls
    assert all(c[0] == "launchctl" for c in run.calls)  # claude=None: no claude call, only a printed command


def _spec(dst):
    return {"type": "stdio", "command": "/usr/bin/uv",
            "args": ["--directory", str(CODE_ROOT), "run", "--frozen", "fitbit-air", "mcp"],
            "env": {"FITBIT_AIR_HOME": str(dst.root)}}


def test_printed_claude_code_command_uses_add_json_with_home_and_absolute_uv(tmp_path):
    import shlex

    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    steps = " ".join(_migrate(src, dst, Run(), _agents(tmp_path)))
    assert shlex.join(["claude", "mcp", "add-json", "-s", "user", "fitbit-coach", json.dumps(_spec(dst))]) in steps
    assert "claude mcp remove fitbit-coach -s user" in steps and "claude mcp remove fitbit-coach -s local" in steps
    assert "Claude Desktop app bundles it" in steps and "claude mcp list" in steps
    assert f"FITBIT_AIR_HOME={dst.root} python3 {CODE_ROOT}/scripts/register_claude_desktop.py" in steps
    assert "Cmd+Q" in steps


def test_claude_code_registration_goes_through_run(tmp_path):
    src, dst = _legacy(tmp_path), Paths(tmp_path / "home")
    run = Run()
    steps = _migrate(src, dst, run, _agents(tmp_path), claude="/fake/claude")
    claude_calls = [c for c in run.calls if c[0] == "/fake/claude"]
    assert claude_calls[0] == ["/fake/claude", "mcp", "remove", "fitbit-coach", "-s", "user"]
    assert claude_calls[1] == ["/fake/claude", "mcp", "remove", "fitbit-coach", "-s", "local"]
    add = claude_calls[2]
    assert add[:6] == ["/fake/claude", "mcp", "add-json", "-s", "user", "fitbit-coach"] and len(add) == 7
    assert json.loads(add[6]) == _spec(dst)  # one JSON argument: nothing can swallow the server name
    assert any("updated the Claude Code registration" in s for s in steps)
    assert any("claude mcp list" in s for s in steps)


def test_refuses_a_target_inside_the_code_folder(tmp_path, monkeypatch):
    src = _legacy(tmp_path)
    monkeypatch.setattr(home, "CODE_ROOT", tmp_path)
    with pytest.raises(MigrateError, match="code folder"):
        _migrate(src, Paths(tmp_path / "home"), Run(), _agents(tmp_path))


def test_an_old_agent_that_will_not_stop_aborts_before_anything_is_switched(tmp_path):
    from types import SimpleNamespace

    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    run = lambda args, **kw: SimpleNamespace(returncode=0)  # noqa: E731 - launchctl print keeps finding it
    with pytest.raises(MigrateError, match="still loaded"):
        _migrate(src, dst, run, agents)
    assert (agents / OLD_PLIST).exists() and not (agents / NEW_PLIST).exists()
    _assert_untouched(src, agents)
    _finish(src, dst, agents)


def test_resume_sets_aside_target_files_and_db_changed_since_the_copy(tmp_path):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        _migrate(src, dst, Run(fail=lambda a: a[1] == "bootstrap"), agents)
    (src.data_dir / "logs" / "sync.log").write_text("log, then more")  # changed at the source only: no set-aside
    dst.profile_file.write_text('sex = "female"\n')                  # changed in the target after the copy
    s = Store(dst.db)                                                  # e.g. a coach that wrote to the copy
    s.save_snapshot("written-to-the-copy", {"z": 1})
    s.close()
    steps = _migrate(src, dst, Run(), agents)
    assert dst.profile_file.read_text() == 'sex = "male"\n'
    assert (dst.config_dir / "profile.toml.before-migrate-2026-10-17").read_text() == 'sex = "female"\n'
    assert not list((dst.data_dir / "logs").glob("*.before-migrate-*"))
    assert (dst.data_dir / "logs" / "sync.log").read_text() == "log, then more"
    kept_db = dst.data_dir / "health.db.before-migrate-2026-10-17"
    assert home._counts(kept_db) != home._counts(dst.db)  # the copy's extra row is kept aside, not lost
    con = sqlite3.connect(kept_db)
    assert con.execute("SELECT COUNT(*) FROM snapshots WHERE kind = 'written-to-the-copy'").fetchone()[0] == 1
    con.close()
    state = json.loads((dst.root / ".migrate-state.json").read_text())
    assert set(state["set_aside"]) == {str(dst.config_dir / "profile.toml.before-migrate-2026-10-17"), str(kept_db)}
    assert sum("before-migrate" in s for s in steps) == 2


def test_an_unchanged_target_db_is_not_set_aside_on_resume(tmp_path):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    with pytest.raises(RuntimeError):
        _migrate(src, dst, Run(fail=lambda a: a[1] == "bootstrap"), agents)
    _finish(src, dst, agents)
    assert not list(dst.data_dir.glob("health.db.before-migrate-*"))


def test_warns_when_the_old_database_grew_after_the_copy(tmp_path):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)

    def run(args, **kw):
        if args[:3] == ["/fake/claude", "mcp", "add-json"]:  # a coach still on the old home writes meanwhile
            s = Store(src.db)
            s.save_snapshot("late", {"y": 1})
            s.close()
    steps = _migrate(src, dst, run, agents, claude="/fake/claude")
    warning = [s for s in steps if s.startswith("warning")]
    assert len(warning) == 1 and "snapshots" in warning[0] and "data.migrated-2026-10-17" in warning[0]


# --- failure injection at each stage, then a clean resume ---------------------------------------------------------

def _finish(src, dst, agents, run=None):
    run = run or Run()
    _migrate(src, dst, run, agents)
    assert json.loads((dst.root / ".migrate-state.json").read_text())["done"] is True
    assert (src.root / "data.migrated-2026-10-17" / "health.db").exists()
    assert (src.root / "secrets.migrated-2026-10-17" / "client_secret.json").exists()
    assert (agents / NEW_PLIST).exists() and not (agents / OLD_PLIST).exists()
    return run


def test_failure_during_copy_switches_nothing_and_resumes(tmp_path, monkeypatch):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)

    def broken(*a):
        raise OSError("disk full")
    monkeypatch.setattr(home, "_backup_db", broken)
    run = Run()
    with pytest.raises(OSError, match="disk full"):
        _migrate(src, dst, run, agents)
    assert run.calls == [] and (agents / OLD_PLIST).exists()
    _assert_untouched(src, agents)
    monkeypatch.undo()
    _finish(src, dst, agents)


def test_failed_verification_switches_nothing_and_resumes(tmp_path, monkeypatch):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    real = home._counts
    monkeypatch.setattr(home, "_counts", lambda db: {**real(db), "x": 1} if db == dst.db else real(db))
    run = Run()
    with pytest.raises(MigrateError, match="verification failed"):
        _migrate(src, dst, run, agents)
    assert run.calls == [] and (agents / OLD_PLIST).exists() and not (agents / NEW_PLIST).exists()
    _assert_untouched(src, agents)
    monkeypatch.undo()
    _finish(src, dst, agents)


def test_failed_integrity_check_switches_nothing(tmp_path, monkeypatch):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    monkeypatch.setattr(home, "_integrity", lambda db: "*** corrupt page")
    run = Run()
    with pytest.raises(MigrateError, match="integrity"):
        _migrate(src, dst, run, agents)
    assert run.calls == []
    _assert_untouched(src, agents)


def test_a_write_to_the_source_between_attempts_is_recopied(tmp_path):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    with pytest.raises(RuntimeError):
        _migrate(src, dst, Run(fail=lambda a: a[1] == "bootstrap"), agents)
    s = Store(src.db)  # e.g. the coach saved a note while the switch was failing
    s.save_snapshot("later", {"y": 1})
    s.close()
    _finish(src, dst, agents)
    assert home._counts(dst.db) == home._counts(src.root / "data.migrated-2026-10-17" / "health.db")


@pytest.mark.parametrize("stage", ["bootout_old", "bootout_new", "bootstrap"])
def test_launchd_failure_renames_nothing_and_resumes(tmp_path, stage):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    fail = {
        "bootout_old": lambda a: a[1] == "bootout" and a[2].endswith("com.example.fitbit-air.sync"),
        "bootout_new": lambda a: a[1] == "bootout" and a[2].endswith("io.github.fitbit-air.sync"),
        "bootstrap": lambda a: a[1] == "bootstrap",
    }[stage]
    with pytest.raises(RuntimeError, match="injected"):
        _migrate(src, dst, Run(fail=fail), agents)
    _assert_untouched(src, agents)
    _finish(src, dst, agents)


def test_claude_code_failure_resumes_without_redoing_copy_or_launchd(tmp_path, monkeypatch):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        _migrate(src, dst, Run(fail=lambda a: a[0] == "/fake/claude" and a[2] == "add-json"), agents,
                 claude="/fake/claude")
    _assert_untouched(src, agents)
    assert (agents / NEW_PLIST).exists()  # launchd already switched
    copies = []
    monkeypatch.setattr(home, "_backup_db", lambda *a: copies.append(a))
    run = Run()
    _migrate(src, dst, run, agents, claude="/fake/claude")
    assert copies == [] and [c for c in run.calls if c[0] == "launchctl"] == []
    assert [c[:3] for c in run.calls] == [["/fake/claude", "mcp", "remove"], ["/fake/claude", "mcp", "remove"],
                                          ["/fake/claude", "mcp", "add-json"]]
    assert (src.root / "data.migrated-2026-10-17").exists()


def test_rename_failure_resumes_with_only_the_renames(tmp_path, monkeypatch):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    real = home._rename_aside

    def flaky(p, stamp):
        if p.name == "secrets":
            raise OSError("busy")
        return real(p, stamp)
    monkeypatch.setattr(home, "_rename_aside", flaky)
    with pytest.raises(OSError, match="busy"):
        _migrate(src, dst, Run(), agents)
    assert (src.root / "data.migrated-2026-10-17").exists() and (src.root / "secrets").exists()
    monkeypatch.undo()
    run = Run()
    _migrate(src, dst, run, agents)
    assert run.calls == []  # the switches were already done: no launchctl, no lock folder recreated
    assert not (src.root / "data").exists() and (src.root / "secrets.migrated-2026-10-17").exists()


def test_rename_never_replaces_an_existing_folder(tmp_path):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    (src.root / "data.migrated-2026-10-17").mkdir()  # empty: a plain rename would silently replace it
    _migrate(src, dst, Run(), agents)
    assert (src.root / "data.migrated-2026-10-17").is_dir()
    assert (src.root / "data.migrated-2026-10-17-2" / "health.db").exists()


def test_running_again_after_success_does_nothing(tmp_path):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    _migrate(src, dst, Run(), agents)
    run = Run()
    steps = _migrate(src, dst, run, agents)
    assert run.calls == [] and "already" in steps[0]
    assert not (src.root / "data").exists()  # the lock was not taken, so no empty data/ was recreated


def test_migrate_retires_old_agents_exactly_once(tmp_path, monkeypatch):
    from fitbit_air import schedule

    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    real, calls = schedule.retire_other_agents, []

    def spy(*a, **k):
        calls.append(a)
        return real(*a, **k)
    monkeypatch.setattr(schedule, "retire_other_agents", spy)
    _migrate(src, dst, Run(), agents)
    assert len(calls) == 1


def test_an_update_only_change_to_the_copy_is_also_kept_aside(tmp_path):
    src, dst, agents = _legacy(tmp_path), Paths(tmp_path / "home"), _agents(tmp_path)
    with pytest.raises(RuntimeError):
        _migrate(src, dst, Run(fail=lambda a: a[1] == "bootstrap"), agents)
    con = sqlite3.connect(dst.db)  # same row counts, different content
    with con:
        con.execute("UPDATE snapshots SET payload = '{\"edited\": true}' WHERE kind = 'profile'")
    con.close()
    _finish(src, dst, agents)
    kept = dst.data_dir / "health.db.before-migrate-2026-10-17"
    con = sqlite3.connect(kept)
    assert con.execute("SELECT payload FROM snapshots WHERE kind = 'profile'").fetchone()[0] == '{"edited": true}'
    con.close()
