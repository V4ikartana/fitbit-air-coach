import os
import stat

import pytest
from typer.testing import CliRunner

from fitbit_air import cli
from fitbit_air.config import Paths
from fitbit_air.home import TEMPLATES, init_home
from fitbit_air.userconfig import check_all

runner = CliRunner()


@pytest.mark.parametrize("template", TEMPLATES)
def test_each_template_initialises_and_passes_check(tmp_path, template):
    paths = Paths(tmp_path / template)
    created = init_home(paths, template)
    assert paths.profile_file.exists() and (paths.root / "data").is_dir() and (paths.root / "secrets").is_dir()
    assert paths.plugins_dir.is_dir() and created
    assert check_all(paths) == []
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700


def test_init_never_overwrites(tmp_path):
    paths = Paths(tmp_path)
    init_home(paths, "minimal")
    paths.profile_file.write_text('sex = "female"\n')
    init_home(paths, "runner-10k")
    assert paths.profile_file.read_text() == 'sex = "female"\n'


def test_unknown_template(tmp_path):
    with pytest.raises(ValueError, match="minimal"):
        init_home(Paths(tmp_path), "nope")


def test_runner_template_has_a_race_and_a_weekly_review_override(tmp_path):
    from fitbit_air.analysis.profile import load_profile
    from fitbit_air.coach.prompt_files import load_prompt_files

    paths = Paths(tmp_path)
    init_home(paths, "runner-10k")
    prof = load_profile(paths.profile_file)
    assert prof.has_race and prof.race_date == "2027-10-10" and prof.race_distance_km == 10.0
    prompts, problems = load_prompt_files(paths)
    assert problems == [] and prompts["weekly_review"].args == ["week_ending"]
    assert "{week_ending}" in prompts["weekly_review"].text and "{next_week}" in prompts["weekly_review"].text


def test_cli_init(isolated, tmp_path):
    r = runner.invoke(cli.app, ["init", "--template", "runner-10k", "--home", str(tmp_path / "h")])
    assert r.exit_code == 0 and (tmp_path / "h" / "config" / "coach" / "persona.md").exists()
    assert "client_secret.json" in r.output and "fitbit-air auth" in r.output


def test_cli_init_unknown_template(isolated, tmp_path):
    r = runner.invoke(cli.app, ["init", "--template", "nope", "--home", str(tmp_path / "h")])
    assert r.exit_code == 1 and "minimal" in r.output


def test_secrets_folder_is_private_even_under_a_permissive_umask(tmp_path):
    old = os.umask(0o022)
    try:
        paths = Paths(tmp_path / "h")
        init_home(paths, "minimal")
        (paths.root / "secrets").chmod(0o755)
        init_home(paths, "minimal")
    finally:
        os.umask(old)
    assert stat.S_IMODE((paths.root / "secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE(paths.root.stat().st_mode) == 0o700


def test_init_refuses_a_folder_that_is_not_a_home(tmp_path):
    other = tmp_path / "docs"
    other.mkdir()
    other.chmod(0o755)
    (other / "notes.txt").write_text("mine")
    with pytest.raises(ValueError, match="not empty"):
        init_home(Paths(other), "minimal")
    assert sorted(p.name for p in other.iterdir()) == ["notes.txt"]
    assert stat.S_IMODE(other.stat().st_mode) == 0o755
    (other / "config").mkdir()  # a folder that already looks like a home is fine
    init_home(Paths(other), "minimal")
    empty = tmp_path / "empty"
    empty.mkdir()
    init_home(Paths(empty), "minimal")


def test_templates_are_self_explanatory():
    from importlib.resources import files

    root = files("fitbit_air.templates")
    minimal = root.joinpath("minimal", "config", "profile.toml").read_text()
    rule = 'body age is only calculated when sex = "male" (its reference data is for men)'
    assert rule in minimal.lower()
    sleep = root.joinpath("sleep-recovery", "config", "profile.toml").read_text()
    assert "male | female | unspecified" in sleep and "metric | imperial" in sleep
    assert rule in sleep.lower()
    dash = root.joinpath("minimal", "config", "dashboard.toml").read_text()
    assert 'today_cards = ["sleep_debt", "activity", "body"]' in dash


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """A fake code repo and default home; nothing here can reach the real ones."""
    from fitbit_air import config

    code, default = tmp_path / "repo", tmp_path / "default-home"
    code.mkdir()
    for mod in (config, cli):
        monkeypatch.setattr(mod, "CODE_ROOT", code)
        monkeypatch.setattr(mod, "DEFAULT_HOME", default)
    monkeypatch.delenv("FITBIT_AIR_HOME", raising=False)
    return code, default


def test_cli_init_without_home_uses_the_default_home_and_skips_the_export_hint(isolated):
    _, default = isolated
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 0 and (default / "config" / "profile.toml").exists()
    assert "FITBIT_AIR_HOME" not in r.output


def test_cli_init_without_home_honours_the_env_var_and_prints_the_export_first(isolated, tmp_path, monkeypatch):
    _, default = isolated
    target = tmp_path / "elsewhere"
    monkeypatch.setenv("FITBIT_AIR_HOME", str(target))
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 0 and (target / "config" / "profile.toml").exists() and not default.exists()
    assert f"export FITBIT_AIR_HOME={target}" in r.output
    assert r.output.index("export FITBIT_AIR_HOME") < r.output.index("client_secret.json")


def test_cli_init_with_home_prints_the_export_first(isolated, tmp_path):
    r = runner.invoke(cli.app, ["init", "--home", str(tmp_path / "h")])
    assert r.exit_code == 0
    assert r.output.index("export FITBIT_AIR_HOME") < r.output.index("client_secret.json")


def _old_repo_home(code):
    (code / "data").mkdir()
    (code / "data" / "health.db").write_bytes(b"")


def test_cli_init_refuses_while_the_data_is_in_the_old_repo_home(isolated):
    code, default = isolated
    _old_repo_home(code)  # no FITBIT_AIR_HOME and no data in the default home: this is the legacy home
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 1 and "run `fitbit-air home migrate` instead" in r.output
    assert "your data is still in the old repo-root home" in r.output
    assert not default.exists()


def test_cli_init_refuses_next_to_an_old_repo_database_unless_home_is_given(isolated, tmp_path, monkeypatch):
    code, default = isolated
    _old_repo_home(code)
    (default / "data").mkdir(parents=True)
    (default / "data" / "health.db").write_bytes(b"")  # the default home has data too, so it is not "legacy"
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 1 and "home migrate" in r.output and not (default / "config").exists()
    other = tmp_path / "other"
    monkeypatch.setenv("FITBIT_AIR_HOME", str(other))  # a different home is selected via the environment
    r = runner.invoke(cli.app, ["init"])
    assert r.exit_code == 1 and not other.exists()


def test_cli_init_refuses_the_code_folder_as_a_home(isolated):
    code, _ = isolated
    r = runner.invoke(cli.app, ["init", "--home", str(code)])
    assert r.exit_code == 1 and "code folder" in r.output
    assert not (code / "config").exists() and not (code / "secrets").exists()
    link = code.parent / "link"
    link.symlink_to(code)
    r = runner.invoke(cli.app, ["init", "--home", str(link)])
    assert r.exit_code == 1 and not (code / "config").exists()


def test_cli_init_refuses_a_folder_that_is_not_a_home(isolated, tmp_path):
    other = tmp_path / "docs"
    other.mkdir()
    (other / "notes.txt").write_text("mine")
    r = runner.invoke(cli.app, ["init", "--home", str(other)])
    assert r.exit_code == 1 and "not empty" in r.output and not (other / "config").exists()
