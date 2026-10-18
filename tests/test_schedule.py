import os
import plistlib
import subprocess

import pytest

from fitbit_air import config, schedule
from fitbit_air.config import Paths


def test_build_plist(tmp_path):
    pl = schedule.build_plist("/opt/homebrew/bin/uv", Paths(tmp_path))
    assert pl["Label"] == schedule.LABEL == "io.github.fitbit-air.sync"
    # code (the repo) and data (the home) are separate: --project and the working directory are the code
    assert pl["ProgramArguments"] == [
        "/opt/homebrew/bin/uv", "run", "--frozen", "--quiet", "--project", str(config.CODE_ROOT), "fitbit-air", "sync"]
    assert pl["WorkingDirectory"] == str(config.CODE_ROOT)
    assert pl["StandardOutPath"] == "/dev/null"
    assert pl["StartInterval"] == 7200 and pl["RunAtLoad"] is True
    assert pl["StandardErrorPath"] == str(tmp_path / "data" / "logs" / "launchd.err.log")
    assert pl["EnvironmentVariables"]["FITBIT_AIR_HOME"] == str(tmp_path)
    assert "PATH" in pl["EnvironmentVariables"]


def test_install_writes_plist_and_bootstraps(tmp_path):
    calls = []
    target = tmp_path / "agents" / "agent.plist"
    schedule.install(Paths(tmp_path), run=lambda args, **kw: calls.append(args), uv="/uv", target=target)
    assert plistlib.loads(target.read_bytes())["Label"] == schedule.LABEL
    assert calls[0] == ["launchctl", "bootout", f"gui/{os.getuid()}/{schedule.LABEL}"]
    assert calls[1] == ["launchctl", "bootstrap", f"gui/{os.getuid()}", str(target)]


def test_install_retries_bootstrap(tmp_path):
    calls, sleeps = [], []

    def run(args, **kw):
        calls.append(args)
        if args[1] == "bootstrap" and len([c for c in calls if c[1] == "bootstrap"]) < 3:
            raise subprocess.CalledProcessError(5, args)

    schedule.install(Paths(tmp_path), run=run, uv="/uv", target=tmp_path / "a.plist", sleep=sleeps.append)
    assert len([c for c in calls if c[1] == "bootstrap"]) == 3
    assert sleeps == [1, 1]


def test_install_gives_up_after_five_bootstrap_attempts(tmp_path):
    calls, sleeps = [], []

    def run(args, **kw):
        calls.append(args)
        if args[1] == "bootstrap":
            raise subprocess.CalledProcessError(5, args)

    with pytest.raises(schedule.ScheduleError, match="background sync is now off"):
        schedule.install(Paths(tmp_path), run=run, uv="/uv", target=tmp_path / "a.plist", sleep=sleeps.append)
    assert len([c for c in calls if c[1] == "bootstrap"]) == 5
    assert len(sleeps) == 4
    assert not (tmp_path / "a.plist").exists()  # no half-installed job left behind


def test_install_removes_its_plist_on_any_bootstrap_failure(tmp_path):
    def run(args, **kw):
        if args[1] == "bootstrap":
            raise FileNotFoundError("launchctl")

    with pytest.raises(FileNotFoundError):
        schedule.install(Paths(tmp_path), run=run, uv="/uv", target=tmp_path / "a.plist")
    assert not (tmp_path / "a.plist").exists()


def test_install_prefers_uv_env_var(tmp_path, monkeypatch):
    monkeypatch.setenv("UV", "/custom/uv")
    target = tmp_path / "a.plist"
    schedule.install(Paths(tmp_path), run=lambda args, **kw: None, target=target)
    assert plistlib.loads(target.read_bytes())["ProgramArguments"][0] == "/custom/uv"


def test_uninstall_removes_plist(tmp_path):
    calls = []
    target = tmp_path / "agent.plist"
    target.write_text("x")
    schedule.uninstall(run=lambda args, **kw: calls.append(args), target=target)
    assert not target.exists()
    assert calls == [["launchctl", "bootout", f"gui/{os.getuid()}/{schedule.LABEL}"]]


def test_plist_path_and_install_honour_agents_dir(tmp_path):
    agents = tmp_path / "LaunchAgents"
    assert schedule.plist_path(agents) == agents / "io.github.fitbit-air.sync.plist"
    path = schedule.install(Paths(tmp_path / "home"), run=lambda args, **kw: None, uv="/uv", agents_dir=agents)
    assert path == agents / "io.github.fitbit-air.sync.plist" and path.exists()
    schedule.uninstall(run=lambda args, **kw: None, agents_dir=agents)
    assert not path.exists()


def test_install_retires_every_other_sync_agent_like_migrate_does(tmp_path):
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    old = agents / "com.example.fitbit-air.sync.plist"
    old.write_bytes(plistlib.dumps({"Label": "com.example.fitbit-air.sync"}))
    other = agents / "com.other.thing.plist"
    other.write_text("unrelated")
    calls = []
    home = tmp_path / "home"
    path = schedule.install(Paths(home), run=lambda args, **kw: calls.append(args), uv="/uv", agents_dir=agents)
    dom = f"gui/{os.getuid()}"
    assert ["launchctl", "bootout", f"{dom}/com.example.fitbit-air.sync"] in calls
    assert not old.exists() and (home / "launchd-backup" / old.name).exists()
    assert other.read_text() == "unrelated"
    assert path.exists() and ["launchctl", "bootstrap", dom, str(path)] in calls
    assert calls.index(["launchctl", "bootout", f"{dom}/com.example.fitbit-air.sync"]) < calls.index(
        ["launchctl", "bootstrap", dom, str(path)])
    schedule.install(Paths(home), run=lambda args, **kw: calls.append(args), uv="/uv", agents_dir=agents)
    assert path.exists() and len(list((home / "launchd-backup").iterdir())) == 1  # nothing left to retire


def _old_agent(agents, name="com.example.fitbit-air.sync"):
    agents.mkdir(exist_ok=True)
    old = agents / f"{name}.plist"
    old.write_bytes(plistlib.dumps({"Label": name}))
    return old


def test_retire_checks_the_old_agent_is_really_gone(tmp_path):
    from types import SimpleNamespace

    agents, home = tmp_path / "LaunchAgents", Paths(tmp_path / "home")
    old = _old_agent(agents)
    calls = []

    def still_loaded(args, **kw):
        calls.append(args)
        return SimpleNamespace(returncode=0)  # bootout "succeeds" but print still finds the job
    sleeps = []
    with pytest.raises(schedule.ScheduleError, match="still loaded"):
        schedule.retire_other_agents(home, still_loaded, agents, sleep=sleeps.append)
    assert old.exists() and not (home.root / "launchd-backup").exists()  # nothing moved
    assert calls[-1] == ["launchctl", "print", f"gui/{os.getuid()}/com.example.fitbit-air.sync"]
    assert [c[1] for c in calls].count("print") == 5 and sleeps == [1, 1, 1, 1]  # bootout is asynchronous: retried
    with pytest.raises(schedule.ScheduleError):
        schedule.install(home, run=still_loaded, uv="/uv", agents_dir=agents, sleep=sleeps.append)
    assert not schedule.plist_path(agents).exists()  # the new job is not installed either

    gone = lambda args, **kw: SimpleNamespace(returncode=113 if args[1] == "print" else 0)  # noqa: E731
    steps = schedule.retire_other_agents(home, gone, agents)
    assert not old.exists() and len(steps) == 1


def test_retire_never_overwrites_a_kept_plist(tmp_path):
    agents, home = tmp_path / "LaunchAgents", Paths(tmp_path / "home")
    for n in range(3):
        _old_agent(agents).write_bytes(plistlib.dumps({"Label": "com.example.fitbit-air.sync", "n": n}))
        schedule.retire_other_agents(home, lambda args, **kw: None, agents, stamp="2026-10-17")
    kept = sorted(p.name for p in (home.root / "launchd-backup").iterdir())
    assert len(kept) == 3
    assert sorted(plistlib.loads((home.root / "launchd-backup" / k).read_bytes())["n"] for k in kept) == [0, 1, 2]


def test_retire_waits_for_an_asynchronous_bootout(tmp_path):
    from types import SimpleNamespace

    agents, home = tmp_path / "LaunchAgents", Paths(tmp_path / "home")
    old = _old_agent(agents)
    prints, sleeps = [], []

    def run(args, **kw):
        if args[1] == "print":
            prints.append(args)
            return SimpleNamespace(returncode=0 if len(prints) == 1 else 113)  # loaded once, then not found
        return SimpleNamespace(returncode=0)
    steps = schedule.retire_other_agents(home, run, agents, sleep=sleeps.append)
    assert len(prints) == 2 and sleeps == [1] and not old.exists() and len(steps) == 1


def test_uninstall_also_stops_and_keeps_other_sync_agents(tmp_path):
    agents, home = tmp_path / "LaunchAgents", Paths(tmp_path / "home")
    old = _old_agent(agents)
    ours = schedule.plist_path(agents)
    ours.write_text("x")
    calls = []
    schedule.uninstall(run=lambda args, **kw: calls.append(args), agents_dir=agents, paths=home)
    dom = f"gui/{os.getuid()}"
    assert ["launchctl", "bootout", f"{dom}/com.example.fitbit-air.sync"] in calls
    assert ["launchctl", "bootout", f"{dom}/{schedule.LABEL}"] in calls
    assert not old.exists() and not ours.exists() and (home.root / "launchd-backup" / old.name).exists()


def test_cli_schedule_commands_refuse_or_retire_for_a_legacy_home(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fitbit_air import cli, config

    code = tmp_path / "repo"
    (code / "data").mkdir(parents=True)
    (code / "data" / "health.db").write_bytes(b"")
    for mod in (config, cli):
        monkeypatch.setattr(mod, "CODE_ROOT", code)
        monkeypatch.setattr(mod, "DEFAULT_HOME", tmp_path / "default-home")
    monkeypatch.delenv("FITBIT_AIR_HOME", raising=False)
    monkeypatch.setattr(schedule, "install", lambda *a, **k: pytest.fail("must not install"))
    r = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert r.exit_code == 1 and "fitbit-air home migrate" in r.output


def test_cli_install_message_states_the_schedule(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from fitbit_air import cli

    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(schedule, "install", lambda paths: tmp_path / "job.plist")
    r = CliRunner().invoke(cli.app, ["schedule", "install"])
    assert r.exit_code == 0, r.output
    assert "every 2 hours, at login, and after wake if a run was missed" in r.output
