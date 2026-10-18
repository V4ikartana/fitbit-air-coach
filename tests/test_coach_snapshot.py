import json
from datetime import date, datetime, timezone

import pytest

from coach_seed import seed_analysis, seed_sync
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.coach import snapshot
from fitbit_air.coach.tools import Ctx
from fitbit_air.config import Paths
from fitbit_air.store import Store

NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
SECTIONS = {"status", "overview", "trends30", "trends90", "sleep14", "activity30", "running90", "bodyage", "targets"}


@pytest.fixture
def ctx(tmp_path):
    paths = Paths(tmp_path)
    s = Store(paths.db)
    yield Ctx(store=s, reg=load_registry(), now=NOW, today=date(2026, 10, 17), paths=paths)
    s.close()


def test_snapshot_has_exactly_the_dashboard_sections(ctx):
    seed_analysis(ctx.store)
    seed_sync(ctx.store, "2026-10-17T04:00:00Z")
    snap = snapshot.build_snapshot(ctx)
    assert snap["schema"] == 1 and snap["as_of"] == "2026-10-17" and snap["generated_at"] == "2026-10-17T06:00:00Z"
    assert set(snap) == SECTIONS | {"schema", "generated_at", "as_of"}
    assert snap["overview"]["safety"][0]["title"] == "Resting HR high"
    assert snap["trends30"]["start"] == "2026-09-18" and snap["trends90"]["start"] == "2026-07-20"
    assert set(snap["trends30"]["series"]) >= {"rhr", "steps_7d", "score:recovery"}
    assert snap["sleep14"]["start"] == "2026-10-04" and snap["running90"]["start"] == "2026-07-20"
    assert snap["bodyage"]["value"] == 34.0
    json.dumps(snap, allow_nan=False)  # strict JSON
    assert "freshness" in snap["status"]
    assert [k for k, v in snap.items() if isinstance(v, dict) and "freshness" in v] == ["status"]


def test_snapshot_on_empty_database_keeps_errors_in_sections(ctx):
    snap = snapshot.build_snapshot(ctx)
    assert "error" in snap["overview"] and "error" in snap["bodyage"]
    assert snap["targets"]["notes"] == []
    json.dumps(snap, allow_nan=False)


def test_write_snapshot_is_atomic_and_capped(ctx, tmp_path, monkeypatch):
    seed_analysis(ctx.store)
    out = tmp_path / "dash" / "snapshot.json"
    path = snapshot.write_snapshot(ctx, out)
    assert path == out and json.loads(out.read_text())["schema"] == 1
    assert not list(out.parent.glob("*.tmp"))
    monkeypatch.setattr(snapshot, "MAX_BYTES", 10)
    with pytest.raises(ValueError, match="too large"):
        snapshot.write_snapshot(ctx, out)
    assert json.loads(out.read_text())["schema"] == 1  # previous file untouched


def test_snapshot_never_includes_journal_or_sql(ctx):
    ctx.store.add_journal("2026-10-17", {"notes": "private words"})
    text = json.dumps(snapshot.build_snapshot(ctx))
    assert "private words" not in text


def test_failed_write_leaves_no_temp_file_and_keeps_the_old_one(ctx, tmp_path, monkeypatch):
    seed_analysis(ctx.store)
    out = tmp_path / "dash" / "snapshot.json"
    snapshot.write_snapshot(ctx, out)

    def boom(src, dst):
        raise OSError("disk full")
    monkeypatch.setattr(snapshot.os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        snapshot.write_snapshot(ctx, out)
    assert not list(out.parent.glob("*.tmp"))
    assert json.loads(out.read_text())["schema"] == 1


def _add_plugin_metric(store, name="plugin:x", end="2026-10-17", n=10):
    from coach_seed import days
    with store.conn:
        store.conn.executemany(
            "INSERT INTO metric_daily (local_date, metric, value, quality) VALUES (?, ?, 1.5, 'good')",
            [(d, name) for d in days(end, n)])


def _dashboard_toml(ctx, text):
    ctx.paths.config_dir.mkdir(parents=True, exist_ok=True)
    (ctx.paths.config_dir / "dashboard.toml").write_text(text)


def test_snapshot_trends_follow_the_configured_metrics_and_plugin_metrics(ctx):
    seed_analysis(ctx.store)
    _add_plugin_metric(ctx.store)
    _dashboard_toml(ctx, 'trend_metrics = ["plugin:x", "rhr"]\n')
    series = snapshot.build_snapshot(ctx)["trends30"]["series"]
    assert set(series) >= {"plugin:x", "rhr"} and series["plugin:x"]
    assert "score:recovery" not in series  # not configured, so not stored
    # the Activity tab draws steps, brisk minutes, sedentary time and weight from the snapshot whatever the layout says
    assert set(series) >= {"steps_7d", "mvpa_week"}


def test_snapshot_uses_default_metrics_when_the_dashboard_config_has_problems(ctx):
    seed_analysis(ctx.store)
    _dashboard_toml(ctx, 'trend_metrics = ["nope"]\n')
    series = snapshot.build_snapshot(ctx)["trends30"]["series"]
    assert set(series) >= {"rhr", "steps_7d", "score:recovery"}
    _dashboard_toml(ctx, "this is = not toml [")
    assert set(snapshot.build_snapshot(ctx)["trends30"]["series"]) >= {"rhr", "steps_7d"}


def test_snapshot_without_a_dashboard_config_is_unchanged(ctx):
    seed_analysis(ctx.store)
    assert set(snapshot.build_snapshot(ctx)["trends30"]["series"]) >= {"rhr", "steps_7d", "mvpa_week", "score:recovery"}


def test_snapshot_accepts_a_loaded_plugin_metric_that_has_no_rows_yet(ctx):
    from fitbit_air.plugins import LoadedPlugins

    seed_analysis(ctx.store)
    _add_plugin_metric(ctx.store, "plugin:stored")
    loaded = LoadedPlugins()
    loaded.metrics["fresh"] = {"fn": lambda d, c: None, "unit": "", "description": "", "file": "f.py"}
    ctx.plugins = loaded
    _dashboard_toml(ctx, 'trend_metrics = ["plugin:fresh", "plugin:stored", "score:sleep"]\n')
    assert snapshot.trend_metrics(ctx)[:3] == ["plugin:fresh", "plugin:stored", "score:sleep"]  # not the defaults


def test_snapshot_with_twelve_trend_metrics_stays_under_the_size_cap(ctx, tmp_path):
    from fitbit_air.dashboard_build import METRICS

    seed_analysis(ctx.store, n=365)
    names = [f"plugin:p{i}" for i in range(12 - len(METRICS))]
    for name in names:
        _add_plugin_metric(ctx.store, name, n=365)
    _dashboard_toml(ctx, "trend_metrics = " + json.dumps(list(METRICS) + names) + "\n")
    assert len(snapshot.trend_metrics(ctx)) >= 12
    out = snapshot.write_snapshot(ctx, tmp_path / "snap.json")
    assert out.stat().st_size < snapshot.MAX_BYTES


def test_more_than_twelve_trend_metrics_is_a_config_problem(ctx):
    from fitbit_air.dashboard_build import load_dashboard_config

    names = [f"plugin:p{i}" for i in range(13)]
    _dashboard_toml(ctx, "trend_metrics = " + json.dumps(names) + "\n")
    cfg, problems = load_dashboard_config(ctx.paths, {n.removeprefix("plugin:") for n in names})
    assert problems and "at most 12" in str(problems[0])
