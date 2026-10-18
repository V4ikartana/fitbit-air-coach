"""`fitbit-air home migrate` end to end through the CLI, in a fake code repo and fake default home. System calls go
to a fake `run` (home._system_run), LaunchAgents is a temporary folder, and uv/claude are fake paths."""
import json
import plistlib
import sqlite3
import subprocess

import pytest
from typer.testing import CliRunner

from fitbit_air import cli, config, home, schedule
from fitbit_air.config import Paths, get_paths
from fitbit_air.store import Store

runner = CliRunner()
OLD = "com.example.fitbit-air.sync"


class Run:
    def __init__(self):
        self.calls, self.fail = [], None

    def __call__(self, args, **kw):
        self.calls.append(list(args))
        if self.fail:
            exc = self.fail(args)
            if exc:
                raise exc


@pytest.fixture
def env(tmp_path, monkeypatch):
    code, default, agents = tmp_path / "repo", tmp_path / "default-home", tmp_path / "LaunchAgents"
    for mod in (config, cli, home, schedule):
        monkeypatch.setattr(mod, "CODE_ROOT", code)
    for mod in (config, cli):
        monkeypatch.setattr(mod, "DEFAULT_HOME", default)
    monkeypatch.delenv("FITBIT_AIR_HOME", raising=False)
    monkeypatch.setattr(schedule, "agents_dir_default", lambda: agents)
    run = Run()
    monkeypatch.setattr(home, "_system_run", run)
    monkeypatch.setattr(cli.shutil, "which", {"uv": "/usr/bin/uv", "claude": "/fake/claude"}.get)
    src = Paths(code, legacy=True)
    s = Store(src.db)
    s.save_snapshot("profile", {"x": 1})
    s.close()
    (src.data_dir / "profile.toml").write_text('sex = "male"\n')
    (code / "secrets").mkdir()
    (code / "secrets" / "client_secret.json").write_text("{}")
    agents.mkdir()
    (agents / f"{OLD}.plist").write_bytes(plistlib.dumps({"Label": OLD}))
    return code, default, agents, run


def _migrate(*extra, input=None):
    return runner.invoke(cli.app, ["home", "migrate", *extra] if input else ["home", "migrate", "--yes", *extra],
                         input=input)


def _write(db, kind):
    s = Store(db)
    s.save_snapshot(kind, {"k": kind})
    s.close()


def _kinds(db):
    con = sqlite3.connect(db)
    try:
        return {r[0] for r in con.execute("SELECT kind FROM snapshots")}
    finally:
        con.close()


def _done(code, default, today_glob="data.migrated-*"):
    state = json.loads((default / ".migrate-state.json").read_text())
    assert state["done"] is True
    assert not (code / "data").exists() and len(list(code.glob(today_glob))) == 1
    p = get_paths()
    assert (p.root, p.legacy) == (default, False)


def test_nothing_to_migrate_for_a_new_home(env):
    code, default, _, run = env
    (code / "data" / "health.db").rename(code / "data" / "elsewhere.db")
    r = _migrate()
    assert r.exit_code == 0 and "Nothing to migrate" in r.output and run.calls == []


def test_resume_after_failed_verification(env, monkeypatch):
    code, default, agents, run = env
    real = home._counts
    with monkeypatch.context() as m:
        m.setattr(home, "_counts", lambda db: {**real(db), "x": 1} if str(db).startswith(str(default)) else real(db))
        r = _migrate()
    assert r.exit_code == 1 and "verification failed" in r.output and "safe to re-run" in r.output
    assert (default / "data" / "health.db").exists()  # the unverified copy exists ...
    assert get_paths().root == code and get_paths().legacy  # ... but the repo home stays the live one
    assert run.calls == [] and (agents / f"{OLD}.plist").exists()
    _write(code / "data" / "health.db", "after-failure")  # the old home keeps syncing meanwhile
    r = _migrate()
    assert r.exit_code == 0, r.output
    _done(code, default)
    assert "after-failure" in _kinds(default / "data" / "health.db")


def test_resume_after_failed_bootstrap(env):
    code, default, agents, run = env
    run.fail = lambda a: FileNotFoundError("launchctl") if a[1] == "bootstrap" else None
    r = _migrate()
    assert r.exit_code == 1 and "safe to re-run" in r.output
    assert get_paths().root == code and get_paths().legacy
    assert not (agents / "io.github.fitbit-air.sync.plist").exists()
    _write(code / "data" / "health.db", "after-failure")
    run.fail = None
    r = _migrate()
    assert r.exit_code == 0, r.output
    _done(code, default)
    assert "after-failure" in _kinds(default / "data" / "health.db")
    assert (agents / "io.github.fitbit-air.sync.plist").exists()


def test_resume_after_failed_claude_add_keeps_writes_to_the_new_home(env):
    code, default, agents, run = env
    run.fail = lambda a: subprocess.CalledProcessError(1, a) if a[:3] == ["/fake/claude", "mcp", "add-json"] else None
    r = _migrate()
    assert r.exit_code == 1 and "safe to re-run" in r.output
    assert (code / "data").exists()  # nothing renamed yet
    p = get_paths()
    assert (p.root, p.legacy) == (default, False)  # launchd switched: the new home is live now
    _write(default / "data" / "health.db", "written-to-new-home")
    run.fail, run.calls[:] = None, []
    r = _migrate()  # get_paths() is no longer legacy, but the state file names the source
    assert r.exit_code == 0, r.output
    assert "Resuming" in r.output
    _done(code, default)
    assert "written-to-new-home" in _kinds(default / "data" / "health.db")  # not re-copied over
    assert [c[2] for c in run.calls if c[0] == "/fake/claude"] == ["remove", "remove", "add-json"]
    assert not [c for c in run.calls if c[0] == "launchctl"]


def test_asks_for_confirmation_and_shows_what_it_will_do(env):
    code, default, _, run = env
    r = _migrate(input="n\n")
    assert r.exit_code == 1 and run.calls == [] and not default.exists()
    for text in (str(code), str(default), "Quit Claude Desktop (Cmd+Q)", "close Claude Code sessions"):
        assert text in r.output
    r = _migrate(input="y\n")
    assert r.exit_code == 0, r.output
    _done(code, default)


def test_refuses_a_linked_worktree_unless_forced(env):
    code, default, _, run = env
    (code / ".git").write_text("gitdir: /somewhere/else\n")
    r = _migrate()
    assert r.exit_code == 1 and "worktree" in r.output and "--force" in r.output and not default.exists()
    r = _migrate("--force")
    assert r.exit_code == 0, r.output
    _done(code, default)


def test_refuses_a_source_other_than_this_checkout_unless_forced(env, tmp_path, monkeypatch):
    _, _, _, run = env
    other = Paths(tmp_path / "other-home")
    _write(other.db, "other")
    monkeypatch.setenv("FITBIT_AIR_HOME", str(other.root))
    r = _migrate("--to", str(tmp_path / "moved"))
    assert r.exit_code == 1 and "not this checkout" in r.output and run.calls == []
    r = _migrate("--to", str(tmp_path / "moved"), "--force")
    assert r.exit_code == 0, r.output
    assert "other" in _kinds(tmp_path / "moved" / "data" / "health.db")


def test_to_is_made_absolute_and_may_not_be_inside_the_code_folder(env, tmp_path, monkeypatch):
    code, _, _, run = env
    monkeypatch.chdir(tmp_path)
    r = _migrate("--to", "relative-home")
    assert r.exit_code == 0, r.output
    state = json.loads((tmp_path / "relative-home" / ".migrate-state.json").read_text())
    assert state["done"] is True
    plist = plistlib.loads((tmp_path / "LaunchAgents" / "io.github.fitbit-air.sync.plist").read_bytes())
    assert plist["EnvironmentVariables"]["FITBIT_AIR_HOME"] == str(tmp_path / "relative-home")


def test_refuses_a_target_inside_the_code_folder(env):
    code, _, _, run = env
    r = _migrate("--to", str(code / "home"))
    assert r.exit_code == 1 and "code folder" in r.output and run.calls == [] and not (code / "home").exists()


def test_database_errors_are_reported_not_raised(env, monkeypatch):
    def broken(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(home, "migrate_home", broken)
    r = _migrate()
    assert r.exit_code == 1 and "malformed" in r.output and "safe to re-run" in r.output
