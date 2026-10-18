from dataclasses import replace
from datetime import date, datetime, timedelta, timezone

import pytest

from analysis_seed import LOCAL_OFFSET_S, days, history, iso, seed_daily, seed_hr_minutes
from fitbit_air.analysis import bodyage
from fitbit_air.analysis.daily import analyze
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.profile import Profile
from profiles import RUNNER

REG = load_registry()
UTC = timezone.utc


def seed_steps(store, ds, per_day=8000.0):
    for d in ds:
        store.replace_clean("steps", [d], {"activity_intervals": [
            {"start_utc": f"{d}T03:00:00Z", "end_utc": f"{d}T03:01:00Z", "local_date": d, "metric": "steps",
             "value": per_day}]})


def seed_run(store, d, km=5.0, minutes=30.0, avg_hr=150.0, sid=None, start=None):
    start = start or datetime.fromisoformat(f"{d}T01:00:00+00:00")
    store.replace_clean("exercise", [d], {"exercise_sessions": [
        {"id": sid or f"run-{d}", "exercise_type": "RUNNING", "display_name": "Run", "start_utc": iso(start),
         "end_utc": iso(start + timedelta(minutes=minutes)), "local_date": d, "duration_min": minutes,
         "active_min": minutes, "avg_hr": avg_hr, "calories_kcal": 300.0, "steps": 4000.0,
         "distance_m": km * 1000, "azm": 20.0}]})


def run_analysis(store, since, today, profile=None):
    store.save_snapshot("profile", {"age": 26})
    return analyze(store, profile or RUNNER, REG, date.fromisoformat(since), date.fromisoformat(today))


def scores(store, d):
    return {s["score"]: s for s in store.analysis_for_day(d)["scores"]}


def metrics(store, d):
    return {m["metric"]: m for m in store.analysis_for_day(d)["metrics"]}


def test_body_age_and_activity_rows_after_a_month(store):
    ds = history(store, "2026-09-01", 40, rhr=66, asleep_h=7.5)
    seed_steps(store, ds, 6500.0)
    store.replace_clean("weight", [ds[-3]], {"samples": [
        {"ts_utc": f"{ds[-3]}T02:00:00Z", "local_date": ds[-3], "metric": "weight_kg", "value": 80.0}]})
    store.replace_clean("height", [ds[0]], {"samples": [
        {"ts_utc": f"{ds[0]}T02:00:00Z", "local_date": ds[0], "metric": "height_m", "value": 1.74}]})
    summary = run_analysis(store, ds[-1], ds[-1])
    s = scores(store, ds[-1])
    ba = s["body_age"]
    assert ba["status"] in ("ok", "provisional") and ba["value"] is not None
    assert set(ba["components"]["components"]) >= {"rhr", "steps", "sleep_min", "bmi"}
    assert ba["components"]["display"] and ba["components"]["caveat"]
    m = metrics(store, ds[-1])
    assert m["steps_7d"]["value"] == pytest.approx(6500.0)
    assert m["weight_trend_kg"]["value"] == pytest.approx(80.0)
    assert m["bmi"]["value"] == pytest.approx(80 / 1.74 ** 2, abs=0.01)
    assert m["strength_min_week"]["value"] == 0 and m["strength_days_week"]["value"] == 0
    assert summary.runs == 0 and str(summary).endswith(", 0 runs")


def test_runs_table_spike_insight_and_race_forecast(store):
    ds = history(store, "2026-09-01", 40)
    seed_run(store, ds[-10], km=4.0)
    seed_run(store, ds[-1], km=6.0)  # 50% longer than the longest in 30 days -> moderate spike
    summary = run_analysis(store, ds[-12], ds[-1])
    assert summary.runs == 2
    runs = store.runs_between(ds[0], ds[-1])
    assert [r["session_id"] for r in runs] == [f"run-{ds[-10]}", f"run-{ds[-1]}"]
    assert runs[-1]["spike_class"] == "moderate" and runs[-1]["spike_pct"] == pytest.approx(50.0)
    assert runs[-1]["z0_min"] == 30  # 60 bpm all run: below Z1
    running = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["kind"] == "running"]
    assert running and "longer than your longest run" in running[0]["title"]
    assert any(i["title"] == "Faster build-up than about +10% a week" for i in running)  # 4 km was 9 days earlier
    race = scores(store, ds[-1])["race"]
    assert race["components"]["status"] == "on_track"
    assert race["components"]["longest_km"] == 6.0
    assert race["components"]["safe_by_race_km"] is not None


def test_no_runs_race_row_and_no_running_insights(store):
    ds = history(store, "2026-09-01", 20)
    run_analysis(store, ds[-1], ds[-1])
    race = scores(store, ds[-1])["race"]
    assert race["components"]["status"] == "no_runs" and race["components"]["first_run_by"]
    assert not [i for i in store.analysis_for_day(ds[-1])["insights"] if i["kind"] == "running"]


def test_no_runs_behind_plan_insight_never_suggests_catching_up(store):
    ds = history(store, "2026-09-01", 20)
    run_analysis(store, ds[-1], ds[-1], replace(RUNNER, race_date="2026-12-01"))
    race = scores(store, ds[-1])["race"]["components"]
    assert race["status"] == "no_runs_behind"
    plan = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["kind"] == "running"]
    assert len(plan) == 1
    assert "more than ~10% a week" in plan[0]["detail"] and "Safe distance by race day" in plan[0]["detail"]
    assert set(plan[0]["evidence_ids"]) == {"riegel1981", "frandsen2025_spikes"}


def test_every_phase_b_insight_and_score_cites_evidence(store):
    ds = history(store, "2026-09-01", 40)
    seed_steps(store, ds, 3000.0)
    seed_run(store, ds[-10], km=4.0)
    seed_run(store, ds[-1], km=6.0)
    run_analysis(store, ds[-5], ds[-1], replace(RUNNER, race_date="2026-11-01"))
    for d in ds[-5:]:
        a = store.analysis_for_day(d)
        for row in a["insights"] + a["scores"]:
            assert row["evidence_ids"] and all(REG.has(e) for e in row["evidence_ids"]), row


# ---- addendum scenarios ----

def test_body_age_steps_count_worn_days_without_rows_as_zero(store):
    ds = history(store, "2026-09-01", 40)  # every day fully worn
    seed_steps(store, ds[-30:][::2], 8000.0)  # 15 of the 30 window days have step rows
    run_analysis(store, ds[-1], ds[-1])
    comp = scores(store, ds[-1])["body_age"]["components"]["components"]
    assert comp["steps"]["value"] == pytest.approx(4000.0)


def _sedentary_from_0450_local(store, day, hours=10.0):
    d = date.fromisoformat(day)
    start = datetime(d.year, d.month, d.day, 4, 50, tzinfo=UTC) - timedelta(seconds=LOCAL_OFFSET_S)
    store.replace_clean("activity-level", [day], {"activity_intervals": [
        {"start_utc": iso(start), "end_utc": iso(start + timedelta(hours=hours)), "local_date": day,
         "metric": "level_sedentary_min", "value": hours * 60}]})


def test_sedentary_uses_minute_level_rows_minus_sleep(store):
    ds = history(store, "2026-09-01", 30)  # sleep 23:00-06:50 local, so a 04:50 start overlaps it by 2 h
    for d in ds[-7:]:
        _sedentary_from_0450_local(store, d)
    run_analysis(store, ds[-1], ds[-1])
    assert metrics(store, ds[-1])["sedentary_h"]["value"] == pytest.approx(8.0, abs=0.05)


def test_weight_nudge_only_on_todays_weekday(store):
    ds = history(store, "2026-09-01", 30)
    run_analysis(store, ds[-14], ds[-1])
    nudged = {d for d in ds[-14:] for i in store.analysis_for_day(d)["insights"]
              if i["kind"] == "weight" and i["title"].startswith("Log your weight")}
    assert nudged == {ds[-8], ds[-1]}


def test_uth_vo2_is_shown_but_not_scored(store):
    ds = history(store, "2026-09-01", 40)
    run_analysis(store, ds[-1], ds[-1])
    vo2 = scores(store, ds[-1])["body_age"]["components"]["components"]["vo2"]
    assert vo2["years"] is None and vo2["excluded"]
    assert vo2["value"] == pytest.approx(15.3 * (208 - 0.7 * 26) / 60, abs=0.01)
    assert "uth2004_vo2max" in scores(store, ds[-1])["body_age"]["evidence_ids"]


def test_google_vo2_is_scored(store):
    ds = history(store, "2026-09-01", 40)
    seed_daily(store, "vo2_max", {d: 50.0 for d in ds[-5:]})
    run_analysis(store, ds[-1], ds[-1])
    vo2 = scores(store, ds[-1])["body_age"]["components"]["components"]["vo2"]
    assert vo2["value"] == 50.0 and vo2["years"] is not None and "excluded" not in vo2


def test_run_crossing_midnight_uses_next_days_heart_rate(store):
    ds = history(store, "2026-09-01", 30)
    seed_hr_minutes(store, ds[-2], bpm=150.0)
    seed_hr_minutes(store, ds[-1], bpm=150.0)
    d = date.fromisoformat(ds[-2])
    start = datetime(d.year, d.month, d.day, 23, 40, tzinfo=UTC) - timedelta(seconds=LOCAL_OFFSET_S)  # 23:40 local
    seed_run(store, ds[-2], km=5.0, minutes=40.0, start=start)
    run_analysis(store, ds[-3], ds[-1])
    run = store.runs_between(ds[-2], ds[-2])[0]
    assert sum(run[f"z{z}_min"] for z in range(6)) == 40


def test_body_age_trend_zero_for_constant_history_and_absent_early(store):
    ds = history(store, "2026-07-01", 70)
    run_analysis(store, ds[-31], ds[-1])
    c = scores(store, ds[-1])["body_age"]["components"]
    assert c["change_30d"] == pytest.approx(0.0, abs=0.01)
    assert "change_90d" not in c  # needs 120 days of history
    assert "change_30d" not in scores(store, ds[-31])["body_age"]["components"]  # < 60 days of history


# ---- review fixes ----

def test_body_age_trend_after_a_resting_hr_step(store):
    ds = history(store, "2026-07-01", 75)
    seed_daily(store, "resting_hr", {d: 70.0 for d in ds[40:]})  # 60 for the first 40 days, then 70
    run_analysis(store, ds[-31], ds[-1])
    # today's 30-day median is 70; 30 days earlier it was 60. RHR is the only scored fitness component (the Uth VO2
    # is shown, not scored), so the body-age change is the RHR component change.
    expected = bodyage.component_years("rhr", 70, REG) - bodyage.component_years("rhr", 60, REG)
    change = scores(store, ds[-1])["body_age"]["components"]["change_30d"]
    assert expected > 0 and change == pytest.approx(expected, abs=0.01)


def test_trend_absent_when_bmi_only_in_todays_window(store):
    ds = history(store, "2026-07-01", 70)
    store.replace_clean("height", [ds[0]], {"samples": [
        {"ts_utc": f"{ds[0]}T02:00:00Z", "local_date": ds[0], "metric": "height_m", "value": 1.74}]})
    for d in ds[-10:]:
        store.replace_clean("weight", [d], {"samples": [
            {"ts_utc": f"{d}T02:00:00Z", "local_date": d, "metric": "weight_kg", "value": 80.0}]})
    run_analysis(store, ds[-31], ds[-1])
    ba = scores(store, ds[-1])["body_age"]
    assert "bmi" in ba["components"]["components"] and "change_30d" not in ba["components"]


def test_trend_absent_when_earlier_window_not_covered(store):
    ds = history(store, "2026-07-01", 70)
    run_analysis(store, ds[-1], ds[-1])  # wear and SRI known only from ds[-1] - 29
    ba = scores(store, ds[-1])["body_age"]
    assert ba["status"] == "ok" and "change_30d" not in ba["components"]


def test_behind_insight_says_not_to_catch_up(store):
    ds = history(store, "2026-09-01", 40)
    seed_run(store, ds[-1], km=4.0)
    run_analysis(store, ds[-1], ds[-1], replace(RUNNER, race_date="2026-11-15"))
    assert scores(store, ds[-1])["race"]["components"]["status"] == "behind"
    plan = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["title"].endswith("behind")]
    assert plan and "more than ~10% a week" in plan[0]["detail"] and "run-walk" in plan[0]["detail"]


def test_tight_insight_is_softer(store):
    ds = history(store, "2026-09-01", 40)  # ds[-1] = 2026-10-10
    seed_run(store, ds[-1], km=4.0)  # needs 13 weeks with cutbacks; 13 weeks left, so the taper doesn't fit
    run_analysis(store, ds[-1], ds[-1], replace(RUNNER, race_date="2027-01-10"))
    assert scores(store, ds[-1])["race"]["components"]["status"] == "tight"
    plan = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["title"].endswith("tight")]
    assert plan and ("Tight but doable: keep increases to about 10% a week and keep the cutback weeks."
                     in plan[0]["detail"])
    assert "run-walk" not in plan[0]["detail"] and "later race" not in plan[0]["detail"]


def test_no_plan_insight_on_or_after_race_day(store):
    ds = history(store, "2026-09-01", 40)
    seed_run(store, ds[-3], km=4.0)
    run_analysis(store, ds[-3], ds[-1], replace(RUNNER, race_date=ds[-2]))
    plan = {i["local_date"] for d in ds[-3:] for i in store.analysis_for_day(d)["insights"]
            if i["kind"] == "running" and "plan" in i["title"]}
    assert plan == {ds[-3]}


def test_forecast_dates_never_in_the_past(store):
    ds = history(store, "2026-09-01", 40)  # 2027-02-07 race: the first-run date 2026-09-27 has passed by ds[-1]
    run_analysis(store, ds[-1], ds[-1])
    race = scores(store, ds[-1])["race"]["components"]
    assert race["status"] == "no_runs_behind"
    for key in ("first_run_by", "latest_start"):
        assert race[key] is None or race[key] >= ds[-1]
    plan = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["kind"] == "running"]
    assert plan and "2026-09-27" not in plan[0]["title"] + plan[0]["detail"]
    early = history(store, "2026-06-01", 20)  # same race, on time: the first-run date is still ahead
    run_analysis(store, early[-1], early[-1])
    assert scores(store, early[-1])["race"]["components"]["first_run_by"] >= early[-1]


def test_body_age_is_men_only(store):
    ds = history(store, "2026-09-01", 40)
    run_analysis(store, ds[-1], ds[-1], replace(RUNNER, sex="female"))
    ba = scores(store, ds[-1])["body_age"]
    assert ba["status"] == "insufficient_data" and ba["components"]["reason"] == "calibrated for men only"


def test_cardio_sessions_with_distance_counted_when_no_runs(store):
    ds = history(store, "2026-09-01", 40)
    start = datetime.fromisoformat(f"{ds[-5]}T01:00:00+00:00")
    store.replace_clean("exercise", [ds[-5]], {"exercise_sessions": [
        {"id": "c1", "exercise_type": "CARDIO_WORKOUT", "display_name": "Workout", "start_utc": iso(start),
         "end_utc": iso(start + timedelta(minutes=30)), "local_date": ds[-5], "duration_min": 30.0,
         "distance_m": 4000.0}]})
    run_analysis(store, ds[-1], ds[-1])
    assert scores(store, ds[-1])["race"]["components"]["cardio_distance_sessions"] == 1


def test_run_without_end_time_does_not_break_the_analysis(store):
    ds = history(store, "2026-09-01", 30)
    start = datetime.fromisoformat(f"{ds[-1]}T01:00:00+00:00")
    store.replace_clean("exercise", [ds[-1]], {"exercise_sessions": [
        {"id": "r-open", "exercise_type": "RUNNING", "display_name": "Run", "start_utc": iso(start), "end_utc": None,
         "local_date": ds[-1], "duration_min": 30.0, "distance_m": 5000.0}]})
    run_analysis(store, ds[-1], ds[-1])
    run = store.runs_between(ds[-1], ds[-1])[0]
    assert run["session_id"] == "r-open" and run["distance_km"] == 5.0 and run["pace_min_km"] == 6.0
    assert run["z1_min"] is None


def test_phase_b_failure_keeps_phase_a_rows(store, monkeypatch):
    import fitbit_air.analysis.daily as daily_mod

    def boom(*a, **k):
        raise RuntimeError("phase b exploded")

    monkeypatch.setattr(daily_mod.fitness, "compute", boom)
    ds = history(store, "2026-09-01", 30)
    summary = run_analysis(store, ds[-1], ds[-1])
    assert summary.error == "phase B failed: RuntimeError('phase b exploded')"
    assert "phase B failed" in str(summary)
    names = set(scores(store, ds[-1]))
    assert {"sleep", "recovery"} <= names and not names & {"body_age", "race"}


def test_body_age_mvpa_uses_the_30_day_window(store):
    ds = history(store, "2026-09-01", 40)
    store.replace_clean("steps", [ds[-20]], {"activity_intervals": [
        {"start_utc": f"{ds[-20]}T03:{m:02d}:00Z", "end_utc": f"{ds[-20]}T03:{m + 1:02d}:00Z" if m < 59
         else f"{ds[-20]}T04:00:00Z", "local_date": ds[-20], "metric": "steps", "value": 110.0} for m in range(60)]})
    run_analysis(store, ds[-1], ds[-1])
    assert metrics(store, ds[-1])["mvpa_week"]["value"] == 0  # weekly metric: 7-day window
    comp = scores(store, ds[-1])["body_age"]["components"]["components"]
    assert comp["mvpa"]["value"] == pytest.approx(14.0)  # 60 min x 7 / 30 worn days


def test_body_age_records_missing_inputs_and_what_bmi_needs(store):
    ds = history(store, "2026-09-01", 40)
    run_analysis(store, ds[-1], ds[-1])
    c = scores(store, ds[-1])["body_age"]["components"]
    assert "bmi" in c["missing"] and c["have_height"] is False and c["have_weight"] is False
    store.replace_clean("weight", [ds[-3]], {"samples": [
        {"ts_utc": f"{ds[-3]}T02:00:00Z", "local_date": ds[-3], "metric": "weight_kg", "value": 80.0}]})
    run_analysis(store, ds[-1], ds[-1])
    c = scores(store, ds[-1])["body_age"]["components"]
    assert "bmi" in c["missing"] and c["have_height"] is False and c["have_weight"] is True


def test_body_age_90_day_trend_when_history_and_coverage_allow(store):
    ds = history(store, "2026-05-01", 125)
    run_analysis(store, ds[-91], ds[-1])  # wear and SRI known from ds[-91] - 29 = ds[-120]
    c = scores(store, ds[-1])["body_age"]["components"]
    assert c["change_90d"] == pytest.approx(0.0, abs=0.01) and c["change_30d"] == pytest.approx(0.0, abs=0.01)


def test_every_cited_evidence_id_exists_statically():
    from fitbit_air.analysis import fitness
    ids = [e for v in fitness.EVIDENCE.values() for e in v]
    ids += [e for v in bodyage.EVIDENCE.values() for e in v]
    ids += [e for v in bodyage.ESTIMATED_EVIDENCE.values() for e in v]
    assert ids and [e for e in ids if not REG.has(e)] == []
    assert {"karvonen1957", "tanaka2001_hrmax"} <= set(fitness.EVIDENCE["race"])
    assert "tanaka2001_hrmax" in bodyage.ESTIMATED_EVIDENCE["vo2"]


def test_fast_weight_loss_insight_cites_evidence(store):
    ds = history(store, "2026-09-01", 30)
    for i, d in enumerate(ds[-14:]):  # 90 kg falling 0.25 kg a day: about 1.9% a week
        store.replace_clean("weight", [d], {"samples": [
            {"ts_utc": f"{d}T02:00:00Z", "local_date": d, "metric": "weight_kg", "value": 90.0 - 0.25 * i}]})
    run_analysis(store, ds[-1], ds[-1])
    fast = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["title"].startswith("Losing more than")]
    assert fast and fast[0]["evidence_ids"] and all(REG.has(e) for e in fast[0]["evidence_ids"])


def test_mvpa_insight_text_comes_from_the_registry(store):
    ds = history(store, "2026-09-01", 30)
    run_analysis(store, ds[-1], ds[-1])
    mvpa = [i for i in store.analysis_for_day(ds[-1])["insights"] if "moderate-to-vigorous" in i["title"]]
    assert mvpa and "150-300 minutes" in mvpa[0]["detail"] and "100+ steps/min" in mvpa[0]["detail"]


def test_mvpa_uses_cadence_not_heart_rate_zone_minutes(store):
    ds = history(store, "2026-09-01", 30)
    store.replace_clean("azm", [ds[-1]], {"activity_intervals": [  # HR-reserve zone minutes alone don't count
        {"start_utc": f"{ds[-1]}T03:00:00Z", "end_utc": f"{ds[-1]}T03:01:00Z", "local_date": ds[-1],
         "metric": "azm_fat_burn", "value": 300.0}]})
    run_analysis(store, ds[-1], ds[-1])
    m = metrics(store, ds[-1])
    assert m["mvpa_week"]["value"] == 0 and m["azm_week"]["value"] == pytest.approx(300.0)


def test_no_race_configured_gives_no_race_score(store):
    ds = history(store, "2026-09-01", 10)
    run_analysis(store, ds[-1], ds[-1], profile=Profile())
    race = scores(store, ds[-1])["race"]
    assert race["status"] == "insufficient_data" and race["components"]["status"] == "no_race"


def test_half_set_race_reason_asks_for_both_fields(store):
    ds = history(store, "2026-09-01", 10)
    for half in (Profile(race_date="2027-06-01"), Profile(race_distance_km=10.0)):
        run_analysis(store, ds[-1], ds[-1], profile=half)
        c = scores(store, ds[-1])["race"]["components"]
        assert c["status"] == "no_race"
        assert c["reason"] == "set both race_date and race_distance_km to get a race plan"


def _run_with(store, reg, since, today):
    store.save_snapshot("profile", {"age": 26})
    return analyze(store, RUNNER, reg, date.fromisoformat(since), date.fromisoformat(today))


def _override(**values):
    from fitbit_air.analysis.evidence import apply_overrides
    return apply_overrides(REG, {"params": {k: {"value": v, "reason": "test"} for k, v in values.items()}})


def test_weight_loss_title_uses_the_registry_limit(store):
    ds = history(store, "2026-09-01", 30)
    for i, d in enumerate(ds[-14:]):
        store.replace_clean("weight", [d], {"samples": [
            {"ts_utc": f"{d}T02:00:00Z", "local_date": d, "metric": "weight_kg", "value": 90.0 - 0.25 * i}]})
    _run_with(store, _override(weight_rate_max_pct=0.5), ds[-1], ds[-1])
    fast = [i for i in store.analysis_for_day(ds[-1])["insights"] if i["title"].startswith("Losing more than")]
    assert fast and fast[0]["title"] == "Losing more than 0.5% of body weight per week"


def test_spike_text_uses_the_registry_threshold(store):
    ds = history(store, "2026-09-01", 40)
    seed_run(store, ds[-10], km=4.0)
    seed_run(store, ds[-1], km=6.0)
    _run_with(store, _override(spike_pct=5), ds[-12], ds[-1])
    spike = [i for i in store.analysis_for_day(ds[-1])["insights"] if "longer than your longest run" in i["title"]]
    assert spike and "more than 5% longer" in spike[0]["detail"]


def test_weigh_in_hint_uses_the_registry_count(store):
    ds = history(store, "2026-09-01", 30)
    _run_with(store, _override(weight_min_weighins=2), ds[-14], ds[-1])
    hints = [i for d in ds[-14:] for i in store.analysis_for_day(d)["insights"]
             if i["title"].startswith("Log your weight")]
    assert hints and all("at least 2 weigh-ins in 14 days" in i["detail"] for i in hints)


def test_weekly_buildup_text_uses_the_registry(store):
    ds = history(store, "2026-09-01", 40)
    seed_run(store, ds[-10], km=4.0)
    seed_run(store, ds[-1], km=6.0)

    def texts(reg):
        _run_with(store, reg, ds[-12], ds[-1])
        return [(i["title"], i["detail"]) for i in store.analysis_for_day(ds[-1])["insights"]
                if i["title"].startswith("Faster build-up")]

    (title, detail), = texts(REG)  # the default wording is unchanged
    assert title == "Faster build-up than about +10% a week"
    assert "near 10% a week, with a cutback week every fourth week." in detail
    (title, detail), = texts(_override(spike_pct=5, forecast_cutback_every=3))
    assert title == "Faster build-up than about +5% a week"
    assert "near 5% a week, with a cutback week every third week." in detail
