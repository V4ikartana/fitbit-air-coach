"""Seed analysis rows, sync runs and raw-ish data for coach tests (no analysis engine needed)."""
from datetime import date, timedelta


def days(end: str, n: int) -> list[str]:
    e = date.fromisoformat(end)
    return [(e - timedelta(days=n - 1 - i)).isoformat() for i in range(n)]


def seed_analysis(store, end: str = "2026-10-17", n: int = 10) -> list[str]:
    """`n` days of metric, score and insight rows ending `end`, plus a run and one safety insight on `end`."""
    ds = days(end, n)
    metrics, scores, insights = [], [], []
    for i, d in enumerate(ds):
        metrics += [
            {"local_date": d, "metric": "rhr", "value": 60.0 + i, "baseline": 60.0, "scale": 2.0, "z": i / 2,
             "band_status": None, "confidence": 0.8, "quality": "good", "n_baseline": 20},
            {"local_date": d, "metric": "steps_7d", "value": 8000.0 + 100 * i, "quality": "good"},
            {"local_date": d, "metric": "mvpa_week", "value": 200.0, "quality": "good"},
            {"local_date": d, "metric": "sleep_debt_h", "value": 3.0, "quality": "good"},
        ]
        scores += [
            {"local_date": d, "score": "recovery", "value": 70.0 - i, "band": "green", "confidence": 0.9,
             "status": "ok", "components": {"hrv": {"score": 60}}, "evidence_ids": ["plews2013_hrv"]},
            {"local_date": d, "score": "sleep", "value": 80.0, "band": None, "confidence": 0.6, "status": "ok",
             "components": {"subscores": {"duration": 90}, "sri": 82.0 - i, "reason": None},
             "evidence_ids": ["hirshkowitz2015_sleep"]},
        ]
    scores.append({"local_date": ds[-1], "score": "body_age", "value": 34.0, "band": None, "confidence": 0.3,
                   "status": "provisional", "evidence_ids": ["gbmc2016_bmi"],
                   "components": {"display": "34 (range 26–41)", "components": {"bmi": {"years": 6.1}},
                                  "levers": {"bmi_minus_2": -1.3}, "caveat": "estimate", "reason": None}})
    scores.append({"local_date": ds[-1], "score": "race", "value": None, "band": "no_runs_behind",
                   "confidence": None, "status": "ok", "evidence_ids": ["riegel1981"],
                   "components": {"status": "no_runs_behind", "safe_by_race_km": 7.8, "weeks_left": 15}})
    insights.append({"local_date": ds[-1], "kind": "safety", "severity": "alert", "title": "Resting HR high",
                     "detail": "3 nights above band", "evidence_ids": ["design_safety_rails"]})
    insights.append({"local_date": ds[-1], "kind": "sleep", "severity": "watch", "title": "Sleep debt",
                     "detail": "3 h", "evidence_ids": ["hirshkowitz2015_sleep"]})
    runs = [{"session_id": "run-1", "local_date": ds[-2], "distance_km": 3.0, "duration_min": 24.0,
             "pace_min_km": 8.0, "avg_hr": 150.0, "spike_pct": None, "spike_class": None}]
    store.replace_analysis(ds[0], metrics, scores, insights, runs)
    return ds


def seed_sync(store, finished_at: str | None) -> None:
    """One sync run; finished_at None leaves it unfinished."""
    run_id = store.start_run("incremental")
    if finished_at is not None:
        store.finish_run(run_id, 10, 5, 0, None)
        store.conn.execute("UPDATE sync_runs SET finished_at = ? WHERE id = ?", (finished_at, run_id))
        store.conn.commit()


def seed_sleep(store, wake: str, start_utc: str, end_utc: str, asleep: float, main: int = 1, nap: int = 0) -> None:
    store.replace_clean(f"sleep-{wake}-{start_utc}", [wake], {"sleep_sessions": [
        {"id": f"s-{wake}-{start_utc}", "start_utc": start_utc, "end_utc": end_utc, "local_date": wake,
         "is_main": main, "is_nap": nap, "sleep_type": "STAGES", "min_asleep": asleep, "min_awake": 20.0,
         "min_in_period": asleep + 20, "efficiency": 0.93, "min_light": asleep * 0.6, "min_deep": asleep * 0.2,
         "min_rem": asleep * 0.2}]})
