from typer.testing import CliRunner

from fitbit_air import cli
from fitbit_air.config import Paths, get_paths
from fitbit_air.userconfig import check_all

runner = CliRunner()


def _write(rel, text):
    p = get_paths().root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)


def test_check_clean_and_dirty(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    assert runner.invoke(cli.app, ["config", "check"]).exit_code == 0
    _write("config/thresholds.toml", '[params.safety_rhr_nights]\nvalue = 6\nreason = "x"\n')
    _write("config/dashboard.toml", 'tabs = ["nope"]\n')
    _write("config/coach/prompts/bad.md", "Hi {tomorrow}")
    _write("config/profile.toml", 'units = "furlongs"\n')
    r = runner.invoke(cli.app, ["config", "check"])
    assert r.exit_code == 1
    for needle in ("thresholds.toml", "dashboard.toml", "bad.md", "profile.toml", "stricter", "nope", "tomorrow",
                   "units"):
        assert needle in r.output, needle


def test_show_marks_sources(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    _write("config/thresholds.toml", '[params.steps_benchmark]\nvalue = 8000\nreason = "doctor"\n')
    _write("config/coach/persona.md", "Be brief.")
    r = runner.invoke(cli.app, ["config", "show"])
    assert r.exit_code == 0
    assert "steps_benchmark = 8000" in r.output and "doctor" in r.output
    assert "Coach persona: " in r.output and "persona.md" in r.output
    assert "sex = unspecified  (default)" in r.output


def problem_text(problems):
    return " ".join(str(p) for p in problems)


def test_check_reports_unreadable_persona(tmp_path):
    persona = tmp_path / "config" / "coach" / "persona.md"
    persona.parent.mkdir(parents=True)
    persona.write_bytes(b"\xff\xfe not utf-8 \x80")
    problems = check_all(Paths(tmp_path))
    assert len(problems) == 1
    assert problems[0].file == str(persona)
    assert "persona" in problems[0].message and "default" in problem_text(problems)


def test_check_reports_half_set_race(tmp_path):
    paths = Paths(tmp_path)
    cfg = tmp_path / "config"
    cfg.mkdir()
    cases = (('race_date = "2027-10-10"\n', "race_distance_km"), ("race_distance_km = 10.0\n", "race_date"))
    for body, missing in cases:
        (cfg / "profile.toml").write_text(body)
        problems = check_all(paths)
        assert len(problems) == 1, problems
        text = str(problems[0])
        assert "profile.toml" in text and missing in text
        assert "set both race_date and race_distance_km to get a race plan" in text
    (cfg / "profile.toml").write_text('race_date = "2027-10-10"\nrace_distance_km = 10.0\n')
    assert check_all(paths) == []
    (cfg / "profile.toml").write_text('sex = "unspecified"\n')
    assert check_all(paths) == []


def test_check_on_an_empty_home_creates_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    assert runner.invoke(cli.app, ["config", "check"]).exit_code == 0
    assert not (tmp_path / "config" / "profile.toml").exists()
    runner.invoke(cli.app, ["config", "show"])
    assert not (tmp_path / "config" / "profile.toml").exists()


def test_profile_syntax_error_reports_file_and_line(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "profile.toml").write_text('sex = "male"\nunits = = "metric"\n')
    problems = check_all(Paths(tmp_path))
    assert len(problems) == 1 and str(problems[0]).startswith(str(tmp_path / "config" / "profile.toml") + ":2:")


def test_show_is_honest_when_files_are_broken(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    _write("config/profile.toml", 'sex = "male"\nunits = "furlongs"\n')
    _write("config/thresholds.toml", '[params.safety_rhr_nights]\nvalue = 6\nreason = "x"\n')  # loosens a rail
    _write("config/coach/persona.md", "  \n")
    _write("config/dashboard.toml", 'tabs = ["nope"]\n')
    r = runner.invoke(cli.app, ["config", "show"])
    assert r.exit_code == 0
    out = r.output
    assert "Profile: invalid, showing built-in defaults (run fitbit-air config check)" in out
    field_lines = [ln for ln in out.splitlines() if ln.startswith("  ") and " = " in ln and "(" in ln
                   and "default " not in ln]
    assert field_lines and all(ln.endswith("(default)") for ln in field_lines), field_lines
    assert "sex = unspecified" in out
    assert "Threshold overrides: none (file has problems; not applied; run config check)" in out
    assert "Coach persona: built-in default" in out and "persona.md" not in out
    assert "dashboard.toml" in out and "(file has problems; not applied; run config check)" in out.split(
        "Dashboard")[1]
    assert "Plugins: disabled" in out


def test_show_names_used_files_and_counts_plugins(tmp_path, monkeypatch):
    monkeypatch.setenv("FITBIT_AIR_HOME", str(tmp_path))
    _write("config/profile.toml", 'sex = "male"\n\n[plugins]\nenabled = true\n')
    _write("config/dashboard.toml", "default_range_days = 90\n")
    _write("plugins/ok.py", "def register(api):\n    pass\n")
    _write("plugins/bad.py", "raise RuntimeError('boom')\n")
    out = runner.invoke(cli.app, ["config", "show"]).output
    assert "Dashboard (" in out and "dashboard.toml): default_range_days=90" in out
    assert "Plugins: 1 loaded, 1 problems" in out
    assert "sex = male  (profile.toml)" in out


def test_show_notes_a_legacy_home(tmp_path):
    from fitbit_air.userconfig import show_all

    assert "fitbit-air home migrate" in show_all(Paths(tmp_path, legacy=True))[0]
    assert "migrate" not in show_all(Paths(tmp_path))[0]
