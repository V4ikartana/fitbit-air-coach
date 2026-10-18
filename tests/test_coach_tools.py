import logging
import time
from datetime import date, datetime, timezone

import pytest

from coach_seed import seed_analysis, seed_sleep, seed_sync
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.coach import tools
from fitbit_air.coach.journal import ValidationError
from fitbit_air.coach.tools import Ctx
from fitbit_air.config import Paths
from fitbit_air.store import Store

REG = load_registry()
NOW = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)


@pytest.fixture
def ctx(tmp_path):
    paths = Paths(tmp_path)  # the store and query_sql must use the same file: paths.db
    s = Store(paths.db)
    yield Ctx(store=s, reg=REG, now=NOW, today=date(2026, 10, 17), paths=paths)
    s.close()


def test_freshness_fresh_stale_and_never(ctx):
    assert tools.freshness(ctx.store, NOW) == {"last_sync_finished_utc": None, "hours_since_sync": None,
                                               "stale": True}
    seed_sync(ctx.store, "2026-10-17T04:00:00Z")
    assert tools.freshness(ctx.store, NOW) == {"last_sync_finished_utc": "2026-10-17T04:00:00Z",
                                               "hours_since_sync": 2.0, "stale": False}
    seed_sync(ctx.store, "2026-10-16T22:00:00Z")  # newer run id, older finish: still the latest run
    assert tools.freshness(ctx.store, NOW)["stale"] is True


def test_freshness_clamps_a_sync_finished_in_the_future(ctx):
    seed_sync(ctx.store, "2026-10-17T06:05:00Z")  # clock skew: finished after "now"
    out = tools.freshness(ctx.store, NOW)
    assert out["hours_since_sync"] == 0.0 and out["stale"] is False


def test_every_result_carries_freshness_and_errors_are_dicts(ctx):
    seed_analysis(ctx.store)
    out = tools.get_overview(ctx)
    assert out["freshness"]["stale"] is True and out["date"] == "2026-10-17"
    bad = tools.get_overview(ctx, date="17-10-2026")
    assert set(bad) >= {"error", "hint"} and "YYYY-MM-DD" in bad["error"]


def test_overview_safety_first_scores_and_metrics(ctx):
    seed_analysis(ctx.store)
    out = tools.get_overview(ctx, date="2026-10-17")
    assert out["safety"][0]["title"] == "Resting HR high"
    assert out["insights"][0]["kind"] == "safety"  # safety sorts first among insights too
    scores = {s["score"]: s for s in out["scores"]}
    assert scores["recovery"]["confidence"] == 0.9 and scores["body_age"]["display"] == "34 (range 26–41)"
    assert out["metrics"]["steps_7d"] == 8900.0 and out["metrics"]["rhr"] == 69.0
    assert scores["recovery"]["evidence_ids"] == ["plews2013_hrv"]
    assert scores["recovery"]["components"] == {"hrv": {"score": 60}}
    assert scores["race"]["components"]["safe_by_race_km"] == 7.8 and "sri" not in tools.OVERVIEW_METRICS


def test_overview_scores_tolerate_non_dict_components(ctx):
    seed_analysis(ctx.store)
    ctx.store.conn.execute("UPDATE scores_daily SET components_json = '[1]' WHERE score = 'recovery'")
    scores = {s["score"]: s for s in tools.get_overview(ctx, date="2026-10-17")["scores"]}
    assert scores["recovery"]["components"] == {} and scores["recovery"]["reason"] is None


def test_overview_missing_day_explains_coverage(ctx):
    seed_analysis(ctx.store)
    out = tools.get_overview(ctx, date="2026-09-01")
    assert out["error"] == "no analysis for 2026-09-01" and "2026-10-08" in out["hint"]
    after = tools.get_overview(ctx, date="2026-10-20")
    assert after["hint"] == "latest analysis is 2026-10-17; run refresh_data or ask for 2026-10-17"
    ctx.store.conn.execute("DELETE FROM scores_daily")
    ctx.store.conn.execute("DELETE FROM insights")
    ctx.store.conn.execute("DELETE FROM metric_daily")
    assert "refresh_data" in tools.get_overview(ctx)["hint"]


def test_date_range_defaults_validation_and_cap():
    today = date(2026, 10, 17)
    assert tools.date_range(None, None, today) == ("2026-10-11", "2026-10-17")
    assert tools.date_range(None, None, today, default_days=30)[0] == "2026-09-18"
    with pytest.raises(tools.ToolError, match="after"):
        tools.date_range("2026-10-17", "2026-10-01", today)
    with pytest.raises(tools.ToolError, match="maximum"):
        tools.date_range("2025-01-01", "2026-10-17", today)
    assert tools.date_range("2025-10-17", "2026-10-17", today) == ("2025-10-17", "2026-10-17")  # 366 days
    with pytest.raises(tools.ToolError, match="maximum"):
        tools.date_range("2025-10-16", "2026-10-17", today)  # 367 days


def test_trends_metrics_scores_and_unknown(ctx):
    ds = seed_analysis(ctx.store)
    out = tools.get_trends(ctx, ["rhr", "score:recovery", "vo2_max"], start=ds[-3])
    assert [p["value"] for p in out["series"]["rhr"]] == [67.0, 68.0, 69.0]
    assert out["series"]["rhr"][0]["baseline"] == 60.0
    assert [p["value"] for p in out["series"]["score:recovery"]] == [63.0, 62.0, 61.0]
    assert out["unknown"] == ["vo2_max"] and "score:sleep" in out["available"]
    assert "error" in tools.get_trends(ctx, [])


def test_trends_all_known_has_no_available_and_accepts_a_single_name(ctx):
    seed_analysis(ctx.store)
    out = tools.get_trends(ctx, "rhr")
    assert "available" not in out and out["unknown"] == [] and len(out["series"]["rhr"]) == 10
    assert "available" not in tools.get_trends(ctx, ["rhr", "score:sleep"])


def test_sleep_nights_scores_and_local_times(ctx, monkeypatch):
    monkeypatch.setenv("TZ", "<+0930>-09:30")  # a fixed +09:30 offset, no named zone
    time.tzset()
    try:
        seed_analysis(ctx.store)
        seed_sleep(ctx.store, "2026-10-17", "2026-10-16T13:30:00Z", "2026-10-16T21:00:00Z", 400)
        out = tools.get_sleep(ctx, start="2026-10-17", end="2026-10-17")
    finally:
        monkeypatch.undo()
        time.tzset()
    night = out["nights"][0]
    assert night["asleep_min"] == 400 and night["main"] is True and night["nap"] is False
    assert night["bed_local"] == "2026-10-16T23:00" and night["wake_local"] == "2026-10-17T06:30"
    assert out["scores"][0]["value"] == 80.0 and out["scores"][0]["subscores"] == {"duration": 90}
    assert out["debt_h"] == {"2026-10-17": 3.0}
    assert out["scores"][0]["sri"] == 73.0 and out["scores"][0]["reason"] is None
    assert out["sri"] == {"2026-10-17": 73.0}  # from the sleep score's components (never in metric_daily)


def test_sleep_sri_map_and_reason_for_unscored_nights(ctx):
    ds = seed_analysis(ctx.store)
    ctx.store.conn.execute("UPDATE scores_daily SET status = 'insufficient_data', value = NULL, "
                           "components_json = '{\"reason\": \"short night\"}' WHERE score = 'sleep' "
                           "AND local_date = ?", (ds[-1],))
    out = tools.get_sleep(ctx, start=ds[-2])
    assert out["sri"] == {ds[-2]: 74.0}  # nights without an SRI are left out
    assert out["scores"][-1]["reason"] == "short night" and out["scores"][-1]["sri"] is None


def test_sleep_tolerates_non_dict_components(ctx):
    seed_analysis(ctx.store)
    ctx.store.conn.execute("UPDATE scores_daily SET components_json = '[1]' WHERE score = 'sleep'")
    out = tools.get_sleep(ctx)
    assert out["scores"][0]["subscores"] is None and out["scores"][0]["sri"] is None and out["sri"] == {}


def test_activity_days_and_sessions(ctx):
    ds = seed_analysis(ctx.store)
    out = tools.get_activity(ctx, start=ds[-2])
    assert [d["date"] for d in out["days"]] == ds[-2:] and out["days"][-1]["mvpa_week"] == 200.0
    assert out["weigh_ins"] == [] and out["sessions"] == []


def test_running_runs_and_plan(ctx):
    seed_analysis(ctx.store)
    out = tools.get_running(ctx)
    assert out["runs"][0]["distance_km"] == 3.0
    assert out["plan"]["status"] == "no_runs_behind" and out["plan"]["components"]["safe_by_race_km"] == 7.8
    assert out["plan"]["evidence_ids"] == ["riegel1981"]
    assert "type Run" in out["note"]


def test_running_plan_status_shows_no_race(ctx):
    ctx.store.replace_analysis("2026-10-17", [], [
        {"local_date": "2026-10-17", "score": "race", "value": None, "band": None, "confidence": None,
         "status": "insufficient_data", "evidence_ids": ["riegel1981"],
         "components": {"status": "no_race", "reason": "no race configured"}}], [], [])
    assert tools.get_running(ctx)["plan"]["status"] == "no_race"


def test_body_age_latest_on_or_before(ctx):
    seed_analysis(ctx.store)
    out = tools.get_body_age(ctx, date="2026-10-20")
    assert out["as_of"] == "2026-10-17" and out["value"] == 34.0 and out["details"]["display"].startswith("34")
    assert "error" in tools.get_body_age(ctx, date="2026-10-01")


def test_evidence_known_and_unknown(ctx):
    out = tools.get_evidence(ctx, ["tudorlocke2019_cadence", "nope"])
    assert out["sources"]["tudorlocke2019_cadence"]["doi"] == "10.1186/s12966-019-0769-6"
    assert out["unknown"] == ["nope"]
    single = tools.get_evidence(ctx, "tudorlocke2019_cadence")
    assert list(single["sources"]) == ["tudorlocke2019_cadence"] and single["unknown"] == []


def test_data_status(ctx):
    seed_sync(ctx.store, "2026-10-17T04:00:00Z")
    ctx.store.set_meta("auth_completed_at", "2026-10-12T06:00:00Z")
    out = tools.data_status(ctx)
    assert out["last_sync"]["finished_at"] == "2026-10-17T04:00:00Z"
    assert out["auth"]["days_since"] == 5.0 and "7 days" in out["auth"]["note"]
    assert out["wear_days_last_14"] == 0 and "journal" in out["row_counts"]


def test_data_status_malformed_auth_time_gives_none(ctx):
    ctx.store.set_meta("auth_completed_at", "not a timestamp")
    out = tools.data_status(ctx)
    assert "error" not in out and out["auth"]["days_since"] is None
    assert out["auth"]["completed_at"] == "not a timestamp" and "7 days after `auth`" in out["auth"]["note"]


def test_describe_schema_has_purposes(ctx):
    out = tools.describe_schema(ctx)
    tables = {t["name"]: t for t in out["tables"]}
    assert tables["metric_daily"]["purpose"] and tables["journal"]["columns"]


def test_internal_errors_become_error_dicts(ctx, monkeypatch):
    monkeypatch.setattr(ctx.store, "analysis_for_day", lambda day: 1 / 0)
    out = tools.get_overview(ctx)
    assert out["error"].startswith("internal error") and "ZeroDivisionError" in out["error"]
    assert out["hint"] == "see data/logs/coach.log; try again or use query_sql"
    assert "stale" in out["freshness"]


def test_internal_value_errors_are_logged_not_reported_as_user_errors(ctx, caplog):
    seed_analysis(ctx.store)
    ctx.store.conn.execute("UPDATE scores_daily SET components_json = '{bad' WHERE score = 'recovery'")
    with caplog.at_level(logging.ERROR, logger="fitbit_air.coach.tools"):
        out = tools.get_overview(ctx, date="2026-10-17")
    assert out["error"].startswith("internal error") and "JSONDecodeError" in out["error"]
    assert any("get_overview" in r.getMessage() for r in caplog.records)


def test_validation_errors_are_user_errors_and_not_logged(ctx, caplog):
    @tools.coach_tool
    def check(_ctx):
        raise ValidationError("feeling must be 1-5")

    with caplog.at_level(logging.ERROR, logger="fitbit_air.coach.tools"):
        out = check(ctx)
    assert out["error"] == "feeling must be 1-5" and out["hint"] is None and not caplog.records


def test_freshness_failure_keeps_the_result(ctx, monkeypatch, caplog):
    seed_analysis(ctx.store)
    monkeypatch.setattr(ctx.store, "last_finished_run", lambda: 1 / 0)
    with caplog.at_level(logging.ERROR, logger="fitbit_air.coach.tools"):
        out = tools.get_overview(ctx)
    assert out["date"] == "2026-10-17" and "freshness" not in out
    assert any("freshness failed" in r.getMessage() for r in caplog.records)


def test_query_sql_tool_wraps_errors(ctx):
    seed_analysis(ctx.store)
    ctx.store.conn.commit()
    ok = tools.query_sql(ctx, "SELECT COUNT(*) AS n FROM scores_daily")
    assert ok["rows"][0][0] > 0
    bad = tools.query_sql(ctx, "DELETE FROM runs")
    assert "error" in bad and "describe_schema" in bad["hint"]


def test_date_range_days_relative_to_end():
    today = date(2026, 10, 17)
    assert tools.date_range(None, None, today, days=14) == ("2026-10-04", "2026-10-17")
    assert tools.date_range(None, "2026-10-10", today, days=1) == ("2026-10-10", "2026-10-10")
    assert tools.date_range(None, None, today, days=366)[0] == "2025-10-17"
    with pytest.raises(tools.ToolError, match="start or days"):
        tools.date_range("2026-10-01", None, today, days=7)
    for bad in (0, 367, -3, True, "7", 7.0):
        with pytest.raises(tools.ToolError, match="days"):
            tools.date_range(None, None, today, days=bad)


def test_read_tools_accept_days(ctx):
    ds = seed_analysis(ctx.store)
    out = tools.get_trends(ctx, ["rhr"], days=3)
    assert (out["start"], out["end"]) == (ds[-3], ds[-1]) and len(out["series"]["rhr"]) == 3
    assert tools.get_sleep(ctx, days=14)["start"] == "2026-10-04"
    assert tools.get_activity(ctx, days=2)["start"] == ds[-2]
    assert tools.get_running(ctx, days=90)["start"] == "2026-07-20"
    assert "start or days" in tools.get_trends(ctx, ["rhr"], start=ds[0], days=3)["error"]
