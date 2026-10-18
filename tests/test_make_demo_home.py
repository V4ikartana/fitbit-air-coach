"""The demo home: fake data for a fictional runner, stored the way sync stores real data."""
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load():
    spec = importlib.util.spec_from_file_location("make_demo_home", ROOT / "scripts" / "make_demo_home.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["make_demo_home"] = mod
    spec.loader.exec_module(mod)
    return mod


DEMO = _load()
END = date(2026, 10, 18)  # a Sunday: the long-run day, which the rough last night turns into a rest day
OFFSET = -4 * 3600  # the runner lives at UTC-4, whatever the test machine's zone
TZ = "DEMO+04"  # the same zone for the machine (POSIX TZ signs are inverted: +04 is 4 hours behind UTC)


@pytest.fixture(scope="module")
def built(tmp_path_factory):
    home = tmp_path_factory.mktemp("demo") / "home"
    DEMO.build(home, end=END, days=21, seed=7, offset_s=OFFSET)
    return home


@pytest.fixture(scope="module")
def facts(built, tmp_path_factory):
    """The facts file, written with the machine's zone set to the runner's (local times come out in it)."""
    out = tmp_path_factory.mktemp("facts") / "facts.json"
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("TZ", TZ)
            time.tzset()
            DEMO.write_facts(built, out, today=END)
    finally:
        time.tzset()  # the context has put the machine's TZ back
    return out.read_text(), json.loads(out.read_text())


def _q(home, sql):
    con = sqlite3.connect(home / "data" / "health.db")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def test_clean_tables_are_filled(built):
    counts = dict(_q(built, "SELECT 'sleep', COUNT(*) FROM sleep_sessions UNION ALL "
                            "SELECT 'runs', COUNT(*) FROM exercise_sessions WHERE exercise_type = 'RUNNING' "
                            "UNION ALL SELECT 'hr', COUNT(*) FROM heart_rate UNION ALL "
                            "SELECT 'daily', COUNT(DISTINCT local_date) FROM daily_metrics"))
    assert counts["sleep"] == 21 and counts["daily"] == 21
    assert counts["runs"] >= 8 and counts["hr"] > 21 * 1000


def test_analysis_ran_and_scored_today(built):
    scores = {s for (s,) in _q(built, f"SELECT score FROM scores_daily WHERE local_date = '{END.isoformat()}'")}
    assert {"sleep", "recovery", "race", "body_age"} <= scores


def test_profile_is_the_fictional_runner(built):
    text = (built / "config" / "profile.toml").read_text()
    assert f'race_date = "{(END + timedelta(days=70)).isoformat()}"' in text
    assert 'sex = "male"' in text


def test_a_finished_sync_is_recorded(built):
    assert _q(built, "SELECT COUNT(*) FROM sync_runs WHERE finished_at IS NOT NULL AND types_failed = 0") == [(1,)]


def test_no_run_or_weigh_in_starts_while_asleep(built):
    nights = _q(built, "SELECT start_utc, end_utc FROM sleep_sessions")
    starts = [s for (s,) in _q(built, "SELECT start_utc FROM exercise_sessions UNION ALL "
                                      "SELECT ts_utc FROM samples WHERE metric = 'weight_kg'")]
    assert len(starts) > 10
    assert not [t for t in starts for a, b in nights if a <= t < b]


def test_heart_rate_follows_the_sleep_sessions(built):
    (asleep,), = _q(built, "SELECT AVG(h.bpm) FROM heart_rate h JOIN sleep_sessions s "
                           "ON h.ts_utc >= s.start_utc AND h.ts_utc < s.end_utc")
    (overall,), = _q(built, "SELECT AVG(bpm) FROM heart_rate")
    assert asleep < 56 < overall


def test_the_last_day_is_a_rough_rest_day(built):
    end = END.isoformat()
    assert _q(built, f"SELECT COUNT(*) FROM exercise_sessions WHERE local_date = '{end}'") == [(0,)]
    (band,), = _q(built, f"SELECT band FROM scores_daily WHERE local_date = '{end}' AND score = 'recovery'")
    findings = _q(built, f"SELECT kind, severity FROM insights WHERE local_date = '{end}'")
    assert band == "yellow" and ("strain", "watch") in findings
    assert not [k for k, _s in findings if k == "safety"]


def test_refuses_a_non_empty_folder(tmp_path):
    (tmp_path / "x").write_text("keep")
    with pytest.raises(DEMO.DemoError, match="not empty"):
        DEMO.build(tmp_path, end=END, days=3, seed=7, offset_s=OFFSET)
    assert (tmp_path / "x").read_text() == "keep"


def test_refuses_a_file(tmp_path):
    (tmp_path / "f").write_text("keep")
    with pytest.raises(DEMO.DemoError, match="not a folder"):
        DEMO.build(tmp_path / "f", end=END, days=3, seed=7, offset_s=OFFSET)
    assert (tmp_path / "f").read_text() == "keep"


@pytest.fixture
def fake_real(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    monkeypatch.setattr(DEMO, "REAL_HOME", real)
    return real


def test_refuses_the_real_default_home(fake_real):
    with pytest.raises(DEMO.DemoError, match="real home"):
        DEMO.build(fake_real, end=END, days=3, seed=7, offset_s=OFFSET)


def test_refuses_a_folder_inside_the_real_home(fake_real):
    with pytest.raises(DEMO.DemoError, match="real home"):
        DEMO.build(fake_real / "demo", end=END, days=3, seed=7, offset_s=OFFSET)
    assert not (fake_real / "demo").exists()


def test_refuses_a_symlink_to_the_real_home(fake_real, tmp_path):
    link = tmp_path / "link"
    link.symlink_to(fake_real)
    with pytest.raises(DEMO.DemoError, match="real home"):
        DEMO.build(link / "demo", end=END, days=3, seed=7, offset_s=OFFSET)
    with pytest.raises(DEMO.DemoError, match="real home"):
        DEMO.write_facts(link, tmp_path / "facts.json", today=END)
    assert list(fake_real.iterdir()) == []


def test_refuses_facts_inside_the_real_home(built, fake_real):
    with pytest.raises(DEMO.DemoError, match="real home"):
        DEMO.write_facts(built, fake_real / "facts.json", today=END)
    assert list(fake_real.iterdir()) == []


def test_refuses_the_real_home_spelled_in_another_case(fake_real, tmp_path):
    other = tmp_path / "REAL"
    if not other.exists():
        pytest.skip("this filesystem is case-sensitive")
    with pytest.raises(DEMO.DemoError, match="real home"):
        DEMO.build(other / "demo", end=END, days=3, seed=7, offset_s=OFFSET)
    with pytest.raises(DEMO.DemoError, match="real home"):
        DEMO.write_facts(tmp_path / "home", other / "data" / "health.db", today=END)
    assert list(fake_real.iterdir()) == []


def test_facts_never_overwrite_a_file_that_is_not_json(built, tmp_path):
    keep = tmp_path / "notes.txt"
    keep.write_text("keep")
    with pytest.raises(DEMO.DemoError, match="not a .json file"):
        DEMO.write_facts(built, keep, today=END)
    assert keep.read_text() == "keep"


def _raw(home):
    return _q(home, "SELECT data_type, point_key, local_date, payload FROM raw_points ORDER BY 1, 2")


def test_same_seed_same_data_and_another_seed_differs(tmp_path):
    a, b, c = tmp_path / "a", tmp_path / "b", tmp_path / "c"
    for home, seed in ((a, 7), (b, 7), (c, 8)):
        DEMO.build(home, end=END, days=5, seed=seed, offset_s=OFFSET, analyse=False)
    assert _raw(a) == _raw(b)
    assert _raw(a) != _raw(c)


def test_facts_file_holds_real_tool_output(facts):
    _text, data = facts
    assert set(data) >= {"report", "overview", "sleep", "snapshot", "init_output"}
    assert "Sleep" in data["report"] and data["overview"]["date"] == END.isoformat()
    assert data["snapshot"]["as_of"] == END.isoformat()
    assert "Home ready at ~/.fitbit-air-demo" in data["init_output"]


def test_facts_hold_no_local_paths_and_no_zone(facts, built, tmp_path):
    text, _data = facts
    for needle in (str(built.parent), str(tmp_path), "/Users/", "/private/", "/var/folders"):
        assert needle not in text
    for needle in (f"{OFFSET}s", "utc_offset", "utcOffset", "DEMO", "UTC-", "-04:00"):
        assert needle not in text


def test_facts_sleep_reads_as_local_nights(facts):
    _text, data = facts
    nights = data["sleep"]["nights"]
    assert len(nights) == 7
    for n in nights:
        assert 5 <= int(n["wake_local"][11:13]) <= 8, n
        if n["wake_date"] != END.isoformat():  # the rough last night is a late bedtime
            assert 21 <= int(n["bed_local"][11:13]) <= 23, n
    rough = [n for n in nights if n["wake_date"] == END.isoformat()]
    assert len(rough) == 1 and 0 <= int(rough[0]["bed_local"][11:13]) <= 2 and rough[0]["asleep_min"] < 300


def test_facts_data_is_fresh_and_today_has_a_finding(facts):
    _text, data = facts
    assert data["overview"]["freshness"]["stale"] is False
    assert [i for i in data["overview"]["insights"] if i["severity"] == "watch"] and not data["overview"]["safety"]
    assert "Your body is showing strain signals" in data["report"]


def test_main_writes_a_home_and_facts(tmp_path, capsys):
    home, out = tmp_path / "home", tmp_path / "facts.json"
    assert DEMO.main([str(home), "--days", "14", "--facts", str(out)]) == 0
    assert json.loads(out.read_text())["overview"]["date"] == date.today().isoformat()
    assert "demo home ready" in capsys.readouterr().out


def test_main_exits_2_on_a_non_empty_folder(tmp_path, capsys):
    (tmp_path / "x").write_text("keep")
    assert DEMO.main([str(tmp_path)]) == 2
    assert "not empty" in capsys.readouterr().err


def test_main_rejects_too_few_days(tmp_path):
    with pytest.raises(SystemExit) as exc:
        DEMO.main([str(tmp_path / "home"), "--days", "13"])
    assert exc.value.code == 2
