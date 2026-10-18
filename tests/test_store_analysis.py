from fitbit_air.store import SCHEMA_VERSION, RawRow


def test_schema_v3_has_analysis_tables(store):
    assert SCHEMA_VERSION >= 3
    names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"metric_daily", "scores_daily", "insights", "analysis_runs"} <= names


def test_replace_analysis_rewrites_only_dates_from_since(store):
    m = lambda d, v: {"local_date": d, "metric": "rhr", "value": v}
    s = lambda d: {"local_date": d, "score": "sleep", "value": 80.0, "band": None, "confidence": 1.0,
                   "status": "ok", "components": {"subscores": {"duration": 100}}, "evidence_ids": ["a"]}
    i = lambda d: {"local_date": d, "kind": "sleep", "severity": "info", "title": "t", "detail": "d",
                   "evidence_ids": []}
    store.replace_analysis("2026-10-01", [m("2026-10-01", 60), m("2026-10-02", 61)], [s("2026-10-02")],
                           [i("2026-10-02")])
    store.replace_analysis("2026-10-02", [m("2026-10-02", 70)], [], [])
    rows = store.conn.execute("SELECT local_date, value FROM metric_daily ORDER BY local_date").fetchall()
    assert [tuple(r) for r in rows] == [("2026-10-01", 60.0), ("2026-10-02", 70.0)]
    assert store.conn.execute("SELECT COUNT(*) FROM scores_daily").fetchone()[0] == 0


def test_analysis_for_day_parses_json_and_orders_insights(store):
    store.replace_analysis("2026-10-02", [], [
        {"local_date": "2026-10-02", "score": "recovery", "value": 55.0, "band": "yellow", "confidence": 0.5,
         "status": "ok", "components": {"hrv": {"score": 40}}, "evidence_ids": ["plews2013_hrv"]}], [
        {"local_date": "2026-10-02", "kind": "data", "severity": "info", "title": "b", "detail": "",
         "evidence_ids": []},
        {"local_date": "2026-10-02", "kind": "safety", "severity": "alert", "title": "a", "detail": "",
         "evidence_ids": ["safety_thresholds"]}])
    out = store.analysis_for_day("2026-10-02")
    assert out["scores"][0]["components"] == {"hrv": {"score": 40}}
    assert out["scores"][0]["evidence_ids"] == ["plews2013_hrv"]
    assert [i["title"] for i in out["insights"]] == ["a", "b"]


def test_analysis_runs(store):
    assert store.last_analysis() is None
    run = store.start_analysis_run("2026-08-16")
    store.finish_analysis_run(run, None, "60 days")
    last = store.last_analysis()
    assert last["since"] == "2026-08-16" and last["summary"] == "60 days" and last["error"] is None


def test_read_methods(store):
    store.replace_clean("seed", ["2026-10-14"], {"daily_metrics": [
        {"local_date": "2026-10-14", "metric": "resting_hr", "value": 60.0}]})
    assert store.daily_metric_values(["resting_hr"], "2026-10-01", "2026-10-31") == [("2026-10-14", "resting_hr", 60.0)]
    store.replace_clean("sleep", ["2026-10-14"], {
        "sleep_sessions": [{"id": "s1", "start_utc": "2026-10-13T17:33:00Z", "end_utc": "2026-10-14T01:33:00Z",
                            "local_date": "2026-10-14", "is_main": 1, "is_nap": 0, "sleep_type": "STAGES",
                            "min_asleep": 450.0, "min_in_period": 480.0, "efficiency": 0.9375}],
        "sleep_stages": [{"session_id": "s1", "stage": "LIGHT", "start_utc": "2026-10-13T17:33:00Z",
                          "end_utc": "2026-10-14T01:33:00Z", "local_date": "2026-10-14"}]})
    store.upsert_raw([RawRow("sleep", "s1", "2026-10-13T17:33:00Z", "2026-10-14T01:33:00Z", "2026-10-14",
                             "34200s", "reconciled", {"x": 1})])
    nights = store.main_sleep_sessions("2026-10-01", "2026-10-31")
    assert nights[0]["id"] == "s1" and nights[0]["utc_offset"] == "34200s"
    assert store.sleep_stages_for(["s1"])[0]["stage"] == "LIGHT"
    store.replace_clean("heart-rate", ["2026-10-14"], {"heart_rate": [
        {"ts_utc": "2026-10-14T03:00:05Z", "local_ts": None, "local_date": "2026-10-14", "bpm": 60, "motion": None},
        {"ts_utc": "2026-10-14T03:00:35Z", "local_ts": None, "local_date": "2026-10-14", "bpm": 70, "motion": None}]})
    assert store.hr_minute_means("2026-10-14", "2026-10-14") == [("2026-10-14", "2026-10-14T03:00", 65.0)]
    store.replace_clean("activity-level", ["2026-10-14"], {"activity_intervals": [
        {"start_utc": "2026-10-14T03:00:00Z", "end_utc": "2026-10-14T04:00:00Z", "local_date": "2026-10-14",
         "metric": "level_sedentary_min", "value": 60.0}]})
    assert store.sedentary_intervals("2026-10-14", "2026-10-14") == [
        ("2026-10-14", "2026-10-14T03:00:00Z", "2026-10-14T04:00:00Z")]
    store.replace_clean("irregular-rhythm-notification", ["2026-10-14"], {"alerts": [
        {"start_utc": "2026-10-14T03:00:00Z", "end_utc": None, "local_date": "2026-10-14",
         "kind": "irregular_rhythm", "payload": "{}"}]})
    assert store.alerts_between("2026-10-01", "2026-10-31") == [("2026-10-14", "irregular_rhythm")]


def _s(d, comps=None):
    return {"local_date": d, "score": "sleep", "value": 80.0, "band": None, "confidence": 1.0, "status": "ok",
            "components": comps if comps is not None else {}, "evidence_ids": []}


def _i(d, kind, severity, title):
    return {"local_date": d, "kind": kind, "severity": severity, "title": title, "detail": "", "evidence_ids": []}


def _counts(store):
    return [store.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
            for t in ("metric_daily", "scores_daily", "insights")]


def test_replace_analysis_rejects_rows_before_since(store):
    import pytest
    store.replace_analysis("2026-10-01", [{"local_date": "2026-10-02", "metric": "rhr", "value": 60}],
                           [_s("2026-10-02")], [_i("2026-10-02", "sleep", "info", "t")])
    before = _counts(store)
    bad_metric = {"local_date": "2026-09-30", "metric": "rhr", "value": 1}
    for args in (([bad_metric], [], []), ([], [_s("2026-09-30")], []),
                 ([], [], [_i("2026-09-30", "sleep", "info", "x")]),
                 ([{"local_date": None, "metric": "rhr", "value": 1}], [], [])):
        with pytest.raises(ValueError, match="before since=2026-10-02"):
            store.replace_analysis("2026-10-02", *args)
    assert _counts(store) == before


def test_heart_rate_date_index_exists(store):
    names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "heart_rate_date" in names


def test_replace_analysis_rejects_nan_and_commits_nothing(store):
    import pytest
    store.replace_analysis("2026-10-01", [{"local_date": "2026-10-02", "metric": "rhr", "value": 60}], [], [])
    with pytest.raises(ValueError):
        store.replace_analysis("2026-10-01", [], [_s("2026-10-02", {"x": float("nan")})], [])
    assert _counts(store) == [1, 0, 0]


def test_finish_analysis_run_unknown_id(store):
    import pytest
    with pytest.raises(ValueError):
        store.finish_analysis_run(999, None, "x")


def test_replace_analysis_rolls_back_on_failure(store):
    import pytest
    store.replace_analysis("2026-10-01", [{"local_date": "2026-10-02", "metric": "rhr", "value": 60}],
                           [_s("2026-10-02")], [_i("2026-10-02", "sleep", "info", "t")])
    before = _counts(store)
    dup = {"local_date": "2026-10-03", "metric": "rhr", "value": 1}
    with pytest.raises(Exception):
        store.replace_analysis("2026-10-01", [dup, dup], [], [])
    assert _counts(store) == before == [1, 1, 1]


def test_v2_to_v3_upgrade(tmp_path):
    from fitbit_air.store import Store
    path = tmp_path / "v2.db"
    s = Store(path)
    s.replace_clean("seed", ["2026-10-14"], {"daily_metrics": [
        {"local_date": "2026-10-14", "metric": "resting_hr", "value": 60.0}]})
    for t in ("metric_daily", "scores_daily", "insights", "analysis_runs"):
        s.conn.execute(f"DROP TABLE {t}")
    s.conn.execute("UPDATE schema_version SET version = 2")
    s.conn.commit()
    s.close()
    s = Store(path)
    try:
        assert s.conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        names = {r[0] for r in s.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"metric_daily", "scores_daily", "insights", "analysis_runs"} <= names
        assert s.daily_metric_values(["resting_hr"], "2026-10-01", "2026-10-31") == [
            ("2026-10-14", "resting_hr", 60.0)]
    finally:
        s.close()


def test_main_sleep_sessions_utc_offset_none_without_raw(store):
    store.replace_clean("sleep", ["2026-10-14"], {"sleep_sessions": [{
        "id": "s1", "start_utc": "2026-10-13T17:33:00Z", "end_utc": "2026-10-14T01:33:00Z",
        "local_date": "2026-10-14", "is_main": 1, "is_nap": 0}]})
    assert store.main_sleep_sessions("2026-10-01", "2026-10-31")[0]["utc_offset"] is None


def test_insight_ordering_safety_first_then_severity(store):
    store.replace_analysis("2026-10-02", [], [], [
        _i("2026-10-02", "data", "info", "info"),
        _i("2026-10-02", "sleep", "alert", "alert"),
        _i("2026-10-02", "sleep", "watch", "watch"),
        _i("2026-10-02", "safety", "info", "safety-info")])
    titles = [i["title"] for i in store.analysis_for_day("2026-10-02")["insights"]]
    assert titles == ["safety-info", "alert", "watch", "info"]


def test_metric_values_over_a_date_range(store):
    rows = [{"local_date": d, "metric": m, "value": v} for d, m, v in (
        ("2026-10-01", "stress_share", 0.1), ("2026-10-02", "stress_share", None), ("2026-10-03", "stress_share", 0.3),
        ("2026-10-03", "rhr", 60.0), ("2026-10-05", "stress_share", 0.5))]
    store.replace_analysis("2026-10-01", rows, [], [])
    assert store.metric_values("stress_share", "2026-10-01", "2026-10-04") == [("2026-10-01", 0.1), ("2026-10-03", 0.3)]
    assert store.metric_values("stress_share", "2026-10-06", "2026-10-09") == []


def test_first_data_date(store):
    overnight = ("resting_hr", "hrv_rmssd_ms")
    assert store.first_data_date(overnight) is None
    store.replace_clean("zones", ["2026-09-01"], {"daily_metrics": [
        {"local_date": "2026-09-01", "metric": "hr_zone_fat_burn_lo_bpm", "value": 98.0}]})
    assert store.first_data_date(overnight) is None              # hr-zone settings don't start the analysis
    store.replace_clean("seed", ["2026-10-03"], {"daily_metrics": [
        {"local_date": "2026-10-03", "metric": "resting_hr", "value": 60.0}]})
    assert store.first_data_date(overnight) == "2026-10-03"
    store.replace_clean("sleep", ["2026-10-02"], {"sleep_sessions": [{
        "id": "s1", "start_utc": "2026-10-01T17:30:00Z", "end_utc": "2026-10-02T01:30:00Z", "local_date": "2026-10-02",
        "is_main": 1, "is_nap": 0, "sleep_type": "STAGES", "min_asleep": 450.0, "min_awake": 30.0,
        "min_in_period": 480.0, "efficiency": 0.94, "min_light": 450.0, "min_deep": 0.0, "min_rem": 0.0}]})
    assert store.first_data_date(overnight) == "2026-10-02"


def test_schema_v4_runs_table_and_replace(store):
    from fitbit_air.store import SCHEMA_VERSION
    assert SCHEMA_VERSION >= 4
    run = {"session_id": "r1", "local_date": "2026-10-14", "distance_km": 3.0, "duration_min": 20.0,
           "pace_min_km": 6.67, "avg_hr": 150.0, "z0_min": 2.0, "z1_min": 1.0, "z2_min": 10.0, "z3_min": 9.0, "z4_min": 0.0,
           "z5_min": 0.0, "decoupling_pct": 3.0, "decoupling_approx": 1, "economy_m_per_beat": None,
           "spike_pct": None, "spike_class": None}
    store.replace_analysis("2026-10-14", [], [], [], [run])
    assert [r["session_id"] for r in store.runs_between("2026-10-01", "2026-10-31")] == ["r1"]
    assert store.runs_between("2026-10-01", "2026-10-31")[0]["z0_min"] == 2.0
    store.replace_analysis("2026-10-14", [], [], [], [])
    assert store.runs_between("2026-10-01", "2026-10-31") == []


def test_phase_b_read_methods(store):
    store.replace_clean("steps", ["2026-10-14"], {"activity_intervals": [
        {"start_utc": "2026-10-14T03:00:00Z", "end_utc": "2026-10-14T03:01:00Z", "local_date": "2026-10-14",
         "metric": "steps", "value": 100.0}]})
    assert store.activity_intervals(["steps"], "2026-10-14", "2026-10-14") == [
        ("2026-10-14", "steps", "2026-10-14T03:00:00Z", "2026-10-14T03:01:00Z", 100.0)]
    store.replace_clean("exercise", ["2026-10-14"], {"exercise_sessions": [
        {"id": "e1", "exercise_type": "RUNNING", "display_name": "Run", "start_utc": "2026-10-14T01:03:00Z",
         "end_utc": "2026-10-14T01:33:00Z", "local_date": "2026-10-14", "duration_min": 30.0, "active_min": 30.0,
         "avg_hr": 150.0, "calories_kcal": 300.0, "steps": 4000.0, "distance_m": 5000.0, "azm": 25.0}]})
    sessions = store.exercise_sessions("2026-10-01", "2026-10-31")
    assert sessions[0]["id"] == "e1" and sessions[0]["distance_m"] == 5000.0
    store.replace_clean("weight", ["2026-10-14"], {"samples": [
        {"ts_utc": "2026-10-14T02:00:00Z", "local_date": "2026-10-14", "metric": "weight_kg", "value": 80.0}]})
    assert store.sample_values(["weight_kg"], "2026-10-01", "2026-10-31") == [
        ("2026-10-14", "2026-10-14T02:00:00Z", "weight_kg", 80.0)]


def test_replace_analysis_rejects_runs_before_since(store):
    import pytest
    with pytest.raises(ValueError):
        store.replace_analysis("2026-10-14", [], [], [], [{"session_id": "r0", "local_date": "2026-10-13"}])


def test_v3_to_v4_upgrade_creates_runs_table(tmp_path):
    from fitbit_air.store import Store
    path = tmp_path / "v3.db"
    s = Store(path)
    s.conn.execute("DROP TABLE runs")
    s.conn.execute("UPDATE schema_version SET version = 3")
    s.conn.commit()
    s.close()
    s = Store(path)
    try:
        assert s.conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
        assert s.runs_between("2026-01-01", "2026-12-31") == []
    finally:
        s.close()


def _run(sid, date="2026-10-14", **extra):
    return {"session_id": sid, "local_date": date, **extra}


def test_replace_analysis_rejects_bad_run_rows_without_deleting(store):
    import pytest
    store.replace_analysis("2026-10-14", [], [], [], [_run("keep")])
    cases = [
        ([_run(None)], "session_id"),
        ([_run("")], "session_id"),
        ([_run(7)], "session_id"),
        ([{"local_date": "2026-10-14"}], "session_id"),
        ([_run("x", bogus=1)], "bogus"),
        ([_run("d"), _run("d")], "duplicate"),
    ]
    for rows, match in cases:
        with pytest.raises(ValueError, match=match):
            store.replace_analysis("2026-10-14", [], [], [], rows)
        assert [r["session_id"] for r in store.runs_between("2026-10-01", "2026-10-31")] == ["keep"]


def test_runs_session_id_is_not_null(store):
    import sqlite3
    import pytest
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO runs (session_id, local_date) VALUES (NULL, '2026-10-14')")


def test_replace_analysis_rewrites_run_whose_date_moved_across_since(store):
    store.replace_analysis("2026-10-10", [], [], [], [_run("r1", "2026-10-10", distance_km=3.0)])
    store.replace_analysis("2026-10-14", [], [], [], [_run("r1", "2026-10-14", distance_km=3.5)])
    rows = store.runs_between("2026-10-01", "2026-10-31")
    assert [(r["session_id"], r["local_date"], r["distance_km"]) for r in rows] == [("r1", "2026-10-14", 3.5)]
