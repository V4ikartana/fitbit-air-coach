import json
import math
import random
from datetime import date, timedelta

from analysis_seed import history, seed_daily, seed_night, seed_sedentary
import fitbit_air.analysis.daily as daily
from fitbit_air.analysis.daily import EVIDENCE, analyze
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.mathutil import phi
from profiles import RUNNER

REG = load_registry()
PROFILE = RUNNER


def run(store, since: str, today: str):
    return analyze(store, PROFILE, REG, date.fromisoformat(since), date.fromisoformat(today))


def score(store, day, name):
    return next(s for s in store.analysis_for_day(day)["scores"] if s["score"] == name)


def insights(store, day, kind):
    return [i for i in store.analysis_for_day(day)["insights"] if i["kind"] == kind]


def metric(store, day, name):
    return next((m for m in store.analysis_for_day(day)["metrics"] if m["metric"] == name), None)


def drop_rhr(store, ds):
    """Remove the resting-HR value on each date (the seed source is replaced with no rows)."""
    store.replace_clean("seed-resting_hr", ds, {})


def test_normal_month_gives_scores_and_no_alerts(store):
    ds = history(store, "2026-09-01", 30)
    summary = run(store, ds[-5], ds[-1])
    assert summary.days == 5
    rec = score(store, ds[-1], "recovery")
    assert rec["status"] == "ok" and 34 <= rec["value"] <= 90 and rec["band"] in ("green", "yellow")
    assert set(rec["components"]) >= {"hrv", "rhr", "sleep", "weights"}
    slp = score(store, ds[-1], "sleep")
    assert slp["status"] == "ok" and slp["value"] > 90 and slp["components"]["sri"] > 95
    kinds = {i["kind"] for i in store.analysis_for_day(ds[-1])["insights"]}
    assert not kinds & {"safety", "strain"}


def test_insufficient_data_is_explicit(store):
    ds = history(store, "2026-10-10", 5)
    run(store, ds[0], ds[-1])
    rec = score(store, ds[-1], "recovery")
    assert rec["status"] == "insufficient_data" and "4/7" in rec["components"]["reason"]
    data = insights(store, ds[-1], "data")
    assert data and "4/7" in data[0]["detail"] and "4/14" in data[0]["detail"]
    rhr = metric(store, ds[-1], "rhr")  # warm-up: value and baseline kept, no z yet
    assert rhr["value"] == 60.0 and rhr["baseline"] == 60.0 and rhr["n_baseline"] == 4 and rhr["z"] is None


def test_four_nights_of_raised_rhr_raise_a_strain_alert(store):
    # rhr_2_nights and rhr_drift are one condition family (spec 7.3), so the first condition night (night 2) is
    # "watch" and the alert comes from persistence: the 3rd consecutive condition night, i.e. raised night 4.
    ds = history(store, "2026-09-01", 34)
    seed_daily(store, "resting_hr", {d: 66.0 for d in ds[-4:]})
    run(store, ds[-4], ds[-1])
    sev = [[i["severity"] for i in insights(store, d, "strain")] for d in ds[-4:]]
    assert sev == [[], ["watch"], ["watch"], ["alert"]]
    strain = insights(store, ds[-1], "strain")[0]
    assert "rhr_up" in strain["detail"] and "alcohol" in strain["detail"]
    assert metric(store, ds[-1], "strain_level")["value"] == 2


def test_single_night_hrv_dip_raises_nothing(store):
    ds = history(store, "2026-09-01", 31)
    seed_daily(store, "hrv_rmssd_ms", {ds[-1]: 20.0})
    run(store, ds[-1], ds[-1])
    kinds = {i["kind"] for i in store.analysis_for_day(ds[-1])["insights"]}
    assert "strain" not in kinds


def test_safety_rail_from_high_rhr(store):
    ds = history(store, "2026-09-01", 20, rhr=105)
    run(store, ds[-1], ds[-1])
    safety = insights(store, ds[-1], "safety")
    assert safety and safety[0]["title"] == "Persistently high resting heart rate"


def test_rerun_is_idempotent_and_respects_since(store):
    ds = history(store, "2026-09-01", 20)
    run(store, ds[10], ds[-1])
    count = lambda: store.conn.execute("SELECT COUNT(*) FROM metric_daily").fetchone()[0]
    first = count()
    run(store, ds[10], ds[-1])
    assert count() == first
    assert store.conn.execute("SELECT MIN(local_date) FROM metric_daily").fetchone()[0] == ds[10]


def test_every_cited_evidence_id_exists():
    for ids in EVIDENCE.values():
        for i in ids:
            assert REG.has(i), i


# ---- addendum scenarios ----

def test_sustained_hrv_drop_is_not_damped(store):
    ds = history(store, "2026-09-01", 37)
    seed_daily(store, "hrv_rmssd_ms", {d: 24.0 for d in ds[-7:]})  # 60% of the 40 ms baseline
    run(store, ds[-1], ds[-1])
    hrv7 = metric(store, ds[-1], "hrv_7d")
    assert hrv7["band_status"] == "below" and abs(hrv7["value"] - 24.0) < 0.01 and abs(hrv7["baseline"] - 40.0) < 0.01
    # baseline / scale / z all describe the rolling-mean baseline, so they agree with each other
    assert abs(hrv7["z"] - (math.log(hrv7["value"]) - math.log(hrv7["baseline"])) / hrv7["scale"]) < 0.05
    rec = score(store, ds[-1], "recovery")
    assert rec["status"] == "ok" and rec["components"]["hrv"]["score"] < 40
    assert rec["components"]["hrv"]["band_status"] == "below"


def test_one_missing_rhr_night_pauses_the_strain_episode(store):
    # raised, raised (watch), missing (skip), raised (drift continues), raised: 3rd condition night -> alert.
    # Had the gap reset the tracker, the last night would only be the first condition night ("watch").
    ds = history(store, "2026-09-01", 35)
    seed_daily(store, "resting_hr", {d: 66.0 for d in ds[-5:]})
    drop_rhr(store, [ds[-3]])
    run(store, ds[-5], ds[-1])
    sev = [[i["severity"] for i in insights(store, d, "strain")] for d in ds[-5:]]
    assert sev == [[], ["watch"], [], ["watch"], ["alert"]]
    assert score(store, ds[-3], "recovery")["components"]["reason"] == "today's HRV or resting HR is missing"
    assert metric(store, ds[-3], "rhr") is None and metric(store, ds[-3], "strain_level") is None


def test_three_missing_rhr_nights_reset_the_strain_episode(store):
    # raised, raised (watch), 3 missing nights (skip, skip, idle), raised, raised: a fresh episode, still "watch".
    ds = history(store, "2026-09-01", 37)
    seed_daily(store, "resting_hr", {d: 66.0 for d in ds[-7:]})
    drop_rhr(store, ds[-5:-2])
    run(store, ds[-7], ds[-1])
    sev = [[i["severity"] for i in insights(store, d, "strain")] for d in ds[-7:]]
    assert sev == [[], ["watch"], [], [], [], [], ["watch"]]


def test_implausible_rhr_is_dropped_at_load(store):
    ds = history(store, "2026-09-01", 30)
    seed_daily(store, "resting_hr", {ds[-1]: 250.0})
    run(store, ds[-3], ds[-1])
    kinds = {i["kind"] for d in ds[-3:] for i in store.analysis_for_day(d)["insights"]}
    assert not kinds & {"strain", "safety"}
    assert metric(store, ds[-1], "rhr") is None
    assert metric(store, ds[-2], "rhr")["value"] == 60.0
    assert score(store, ds[-1], "recovery")["status"] == "insufficient_data"


def test_valid_but_unscorable_night(store):
    ds = history(store, "2026-09-01", 30)
    wake = ds[-1]
    # A staged 8-hour main sleep (valid) with no minutes asleep, no efficiency and no stages: nothing to score.
    store.replace_clean("sleep", [wake], {"sleep_sessions": [{
        "id": f"s-{wake}", "start_utc": "2026-09-29T17:30:00Z", "end_utc": "2026-09-30T01:30:00Z",
        "local_date": wake, "is_main": 1, "is_nap": 0, "sleep_type": "STAGES", "min_asleep": None,
        "min_awake": None, "min_in_period": 480.0, "efficiency": None, "min_light": None, "min_deep": None,
        "min_rem": None}]})
    run(store, wake, wake)
    slp = score(store, wake, "sleep")
    assert slp["status"] == "insufficient_data"
    assert slp["components"]["reason"] == "not enough sleep data to score this night"
    rec = score(store, wake, "recovery")
    assert rec["status"] == "ok"
    assert rec["components"]["sleep"]["score"] == 100.0     # debt-only sleep component (6 full nights, no debt)
    assert rec["components"]["sleep"]["debt_h"] == 0.0


def test_no_night_at_all_gives_the_no_valid_sleep_reason(store):
    ds = history(store, "2026-09-01", 30)
    store.replace_clean("sleep", [ds[-1]], {})
    run(store, ds[-1], ds[-1])
    slp = score(store, ds[-1], "sleep")
    assert slp["status"] == "insufficient_data" and slp["components"]["reason"].startswith("no valid main sleep")


def test_stress_share_uses_good_wear_days(store):
    ds = history(store, "2026-09-01", 20)
    for d in ds:
        seed_sedentary(store, d, 10, 3)
    run(store, ds[-1], ds[-1])
    stress = metric(store, ds[-1], "stress_share")
    assert stress is not None and stress["value"] == 0.0 and stress["quality"] == "good"


def test_rows_are_only_written_from_since(store):
    ds = history(store, "2026-09-01", 25)
    summary = run(store, ds[-3], ds[-1])
    assert summary.days == 3
    for table in ("metric_daily", "scores_daily", "insights"):
        low = store.conn.execute(f"SELECT MIN(local_date) FROM {table}").fetchone()[0]
        assert low is None or low >= ds[-3]


def noisy_hrv(ds, sd=0.10, seed=1):
    rng = random.Random(seed)
    return {d: 40.0 * math.exp(rng.gauss(0, sd)) for d in ds}


def below_insights(store, day):
    return [i for i in insights(store, day, "recovery") if i["title"].startswith("HRV 7-day average")]


def test_single_hrv_dip_in_noisy_history_gives_no_below_insight(store):
    ds = history(store, "2026-09-01", 41)
    hrv = noisy_hrv(ds)
    hrv[ds[-1]] = 24.0
    seed_daily(store, "hrv_rmssd_ms", hrv)
    run(store, ds[-10], ds[-1])
    assert all(not below_insights(store, d) for d in ds[-10:])


def test_sustained_hrv_drop_in_noisy_history_gives_below_insight(store):
    ds = history(store, "2026-09-01", 44)
    hrv = noisy_hrv(ds)
    hrv.update({d: 24.0 for d in ds[-4:]})
    seed_daily(store, "hrv_rmssd_ms", hrv)
    run(store, ds[-10], ds[-1])
    assert all(not below_insights(store, d) for d in ds[-10:-4])
    assert below_insights(store, ds[-1])
    assert metric(store, ds[-1], "hrv_7d")["z"] <= REG.param("hrv_below_z")


def test_loading_windows(store, monkeypatch):
    seen = {}
    real = daily.load_inputs

    def spy(store_, start, end, minutes_start=None):
        seen.update(start=start, end=end, minutes_start=minutes_start)
        return real(store_, start, end, minutes_start=minutes_start)

    history(store, "2026-08-01", 3)  # data starts well before since, so since is not clipped
    monkeypatch.setattr(daily, "load_inputs", spy)
    run(store, "2026-10-10", "2026-10-15")
    since = date(2026, 10, 10)
    window, stress_days = int(REG.param("baseline_window_days")), int(REG.param("stress_baseline_days"))
    assert seen == {"start": (since - timedelta(days=2 * window)).isoformat(), "end": "2026-10-15",
                    "minutes_start": (since - timedelta(days=stress_days + 1)).isoformat()}


def test_sleep_debt_insight_text(store):
    ds = history(store, "2026-09-01", 20, asleep_h=6.0)
    run(store, ds[-1], ds[-1])
    debt = insights(store, ds[-1], "sleep")
    assert debt and debt[0]["title"] == "Sleep debt of 10.5 h this week"
    assert debt[0]["detail"] == "Estimated from your recent nights against your 7.5 h nightly need."


# ---- whole-branch review ----

def bad_day(store, day):
    seed_daily(store, "resting_hr", {day: 75.0})
    seed_daily(store, "hrv_rmssd_ms", {day: 15.0})


def low_insights(store, day):
    return [i for i in insights(store, day, "recovery") if i["title"] == "Recovery is low today"]


def test_red_recovery_at_low_confidence_gives_no_low_insight(store):
    ds = history(store, "2026-09-01", 13)
    bad_day(store, ds[-1])
    run(store, ds[-1], ds[-1])
    rec = score(store, ds[-1], "recovery")
    assert rec["status"] == "ok" and rec["band"] == "red"
    assert rec["confidence"] < REG.param("finding_min_confidence")
    assert not low_insights(store, ds[-1])


def test_red_recovery_at_full_confidence_gives_low_insight(store):
    ds = history(store, "2026-09-01", 31)
    bad_day(store, ds[-1])
    run(store, ds[-1], ds[-1])
    rec = score(store, ds[-1], "recovery")
    assert rec["band"] == "red" and rec["confidence"] >= REG.param("finding_min_confidence")
    assert low_insights(store, ds[-1])


def test_short_staged_night_counts_toward_debt_and_is_explained(store):
    ds = history(store, "2026-09-01", 30)
    seed_night(store, ds[-1], asleep_h=2.9, awake_min=0)  # 174 min in bed: staged but under the 180-min minimum
    run(store, ds[-1], ds[-1])
    slp = score(store, ds[-1], "sleep")
    assert slp["status"] == "insufficient_data"
    assert slp["components"]["reason"] == "short night: 2.9 h asleep, under the 3 h minimum for a full score"
    debt = metric(store, ds[-1], "sleep_debt_h")
    assert debt["value"] == 4.6 and debt["quality"] == "good"   # (7.5 - 2.9) / 7 nights x 7; multi-night estimate
    assert score(store, ds[-1], "recovery")["components"]["sleep"]["debt_h"] == 4.6
    assert metric(store, ds[-1], "sleep_min") is None                        # baselines keep the validity rule


def test_hrv_trend_not_ready_uses_last_night(store):
    ds = history(store, "2026-09-01", 9)   # 8 baseline days, but only 5 past 7-day means
    run(store, ds[-1], ds[-1])
    rec = score(store, ds[-1], "recovery")
    assert rec["status"] == "ok"
    assert rec["components"]["hrv"]["roll_z"] is None and rec["components"]["hrv"]["night_z"] == 0.0
    assert rec["components"]["hrv"]["score"] == 50.0 and rec["components"]["hrv"]["shrunk"] is True


def test_overnight_rows_use_night_validity_for_quality(store):
    ds = history(store, "2026-09-01", 30)
    for d in ds:
        seed_sedentary(store, d, 10, 3)
    store.replace_clean("heart-rate", [ds[-1]], {})            # no daytime wear on the last day
    seed_night(store, ds[-2], asleep_h=2.0, awake_min=0)       # invalid (short) night on the day before
    run(store, ds[-3], ds[-1])
    assert metric(store, ds[-1], "rhr")["quality"] == "good"
    assert metric(store, ds[-1], "hrv_7d")["quality"] == "good"
    assert metric(store, ds[-2], "rhr")["quality"] == "invalid"
    assert metric(store, ds[-2], "stress_share")["quality"] == "good"   # daytime rows keep day-wear quality


def test_every_insight_cites_known_evidence(store):
    ds = history(store, "2026-07-01", 60, asleep_h=6.0)
    store.replace_clean("seed-resting_hr", ds[:50], {"daily_metrics": [
        {"local_date": d, "metric": "resting_hr", "value": 60.0 if d < ds[40] else 105.0} for d in ds[:50]]})
    seed_daily(store, "resting_hr", {d: 66.0 for d in ds[50:54]})
    bad_day(store, ds[-1])
    run(store, ds[0], ds[-1])
    rows = store.conn.execute("SELECT kind, evidence_ids FROM insights").fetchall()
    kinds = {r[0] for r in rows}
    assert {"data", "safety", "strain", "sleep", "recovery"} <= kinds
    for kind, ids in rows:
        ids = json.loads(ids)
        assert ids and all(REG.has(i) for i in ids), kind


def test_since_is_clipped_to_the_first_data(store):
    ds = history(store, "2026-09-01", 20)
    summary = run(store, "2026-08-01", ds[-1])
    assert summary.days == 20
    for table in ("metric_daily", "scores_daily", "insights"):
        assert store.conn.execute(f"SELECT MIN(local_date) FROM {table}").fetchone()[0] == ds[0]


def test_empty_store_writes_nothing(store):
    summary = run(store, "2026-08-01", "2026-10-15")
    assert (summary.days, summary.metric_rows, summary.score_rows, summary.insights) == (0, 0, 0, 0)
    assert store.conn.execute("SELECT COUNT(*) FROM scores_daily").fetchone()[0] == 0


def test_night_only_hrv_term_is_shrunk_early_on(store):
    ds = history(store, "2026-09-01", 9)
    seed_daily(store, "hrv_rmssd_ms", {ds[-1]: 30.0})
    run(store, ds[-1], ds[-1])
    hrv = score(store, ds[-1], "recovery")["components"]["hrv"]
    night_z = math.log(30 / 40) / REG.param("floor_hrv")
    assert hrv["night_z"] == round(night_z, 2) and hrv["shrunk"] is True and hrv["roll_z"] is None
    assert hrv["score"] == round(100 * phi(night_z * REG.param("hrv_night_only_shrink")), 1)


def test_hrv_term_not_shrunk_once_trend_is_ready(store):
    ds = history(store, "2026-09-01", 30)
    run(store, ds[-1], ds[-1])
    assert score(store, ds[-1], "recovery")["components"]["hrv"]["shrunk"] is False
