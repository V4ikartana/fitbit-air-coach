from datetime import date

from analysis_seed import history
from fitbit_air.analysis.daily import analyze
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.profile import Profile
from fitbit_air.analysis.report import DISCLAIMER, render
from profiles import RUNNER


def _analyze(store, ds):
    analyze(store, RUNNER, load_registry(), date.fromisoformat(ds[-1]), date.fromisoformat(ds[-1]))


def test_report_shows_scores_components_and_disclaimer(store):
    ds = history(store, "2026-09-01", 30)
    _analyze(store, ds)
    text = render(store, ds[-1])
    assert "Recovery" in text and "Sleep" in text
    assert "duration" in text and "hrv" in text
    assert text.strip().endswith(DISCLAIMER)


def test_safety_comes_first(store):
    ds = history(store, "2026-09-01", 20, rhr=105)
    _analyze(store, ds)
    text = render(store, ds[-1])
    assert text.index("SAFETY") < text.index("Scores")


def test_no_analysis_message(store):
    assert "fitbit-air analyze" in render(store, "2026-10-14")


def test_none_subscore_prints_dash(store):
    ds = history(store, "2026-09-01", 30)
    _analyze(store, ds)
    store.conn.execute("UPDATE scores_daily SET components_json = ? WHERE score = 'sleep' AND local_date = ?",
                       ('{"subscores": {"regularity": null, "duration": 80.0}}', ds[-1]))
    text = render(store, ds[-1])
    line = next(ln for ln in text.splitlines() if ln.strip().startswith("regularity"))
    assert line.split() == ["regularity", "-"]


def test_low_confidence_note_only_when_confidence_below_half(store):
    ds = history(store, "2026-09-01", 30)
    _analyze(store, ds)
    note = "(low confidence: baselines still building)"
    store.conn.execute("UPDATE scores_daily SET confidence = 0.4 WHERE score = 'recovery' AND local_date = ?",
                       (ds[-1],))
    low = render(store, ds[-1])
    assert low.count(note) == 1
    lines = low.splitlines()
    i = next(i for i, ln in enumerate(lines) if ln.strip().startswith("Recovery"))
    assert note in lines[i + 1]
    store.conn.execute("UPDATE scores_daily SET confidence = 0.9 WHERE local_date = ?", (ds[-1],))
    assert note not in render(store, ds[-1])


def _stress(store, values: dict[str, float]):
    store.conn.executemany("INSERT INTO metric_daily (local_date, metric, value) VALUES (?, 'stress_share', ?)",
                           list(values.items()))


def test_experimental_stress_line_is_a_7_day_mean(store):
    ds = history(store, "2026-09-01", 30)
    _analyze(store, ds)
    assert "Experimental" not in render(store, ds[-1])
    _stress(store, {ds[-1]: 0.25, ds[-2]: 0.10})                 # two values: not enough for a trend
    assert "Experimental" not in render(store, ds[-1])
    _stress(store, {ds[-4]: 0.40, ds[-8]: 0.90})                 # ds[-8] lies outside the 7 days
    text = render(store, ds[-1])
    assert "Experimental stress index (7-day): 25% of waking sedentary minutes elevated (trend only)" in text
    assert text.index("Experimental") < text.index(DISCLAIMER)


def test_recovery_hrv_line_shows_band_status(store):
    ds = history(store, "2026-09-01", 30)
    _analyze(store, ds)
    line = next(ln for ln in render(store, ds[-1]).splitlines() if ln.strip().startswith("hrv"))
    assert line.endswith("(7-day band: within)")


def test_no_analysis_message_mentions_since_for_old_dates(store):
    assert "fitbit-air analyze --since YYYY-MM-DD" in render(store, "2026-10-14")


# ---- Phase B sections ----

def _analyze_aged(store, ds):
    store.save_snapshot("profile", {"age": 26})
    _analyze(store, ds)


def test_report_has_phase_b_sections(store):
    ds = history(store, "2026-09-01", 40)
    _analyze_aged(store, ds)
    text = render(store, ds[-1])
    assert "Body age" in text and "Running & 10k" in text
    assert "population-statistics estimate" in text
    assert text.index("Body age") < text.index(DISCLAIMER)


def test_generic_scores_section_skips_phase_b_scores(store):
    ds = history(store, "2026-09-01", 40)
    _analyze_aged(store, ds)
    scores_part = render(store, ds[-1]).split("Scores", 1)[1].split("\n\n", 1)[0]
    assert "Body_age" not in scores_part and "Race" not in scores_part


def test_body_age_section_from_a_real_analysis(store):
    ds = history(store, "2026-09-01", 40)
    _analyze_aged(store, ds)
    ba = next(s for s in store.analysis_for_day(ds[-1])["scores"] if s["score"] == "body_age")
    text = render(store, ds[-1])
    assert f"Body age {ba['components']['display']} vs your age 26" in text
    assert "VO2 max ~" in text and "(estimated from heart rates; shown, not scored)" in text
    assert "At your age this describes a risk profile" in text  # age 26 is age_extrapolated


def _put(store, day, *scores, metrics=()):
    rows = [{"local_date": day, "value": None, "band": None, "confidence": None, "status": "ok",
             "evidence_ids": ["x"], **s} for s in scores]
    store.replace_analysis(day, [{"local_date": day, "quality": "good", **m} for m in metrics], rows, [])


BODY = {"score": "body_age", "value": 31.0, "confidence": 0.5, "status": "provisional", "components": {
    "components": {"rhr": {"value": 72.0, "years": 1.2}, "steps": {"value": 4000.0, "years": 2.5},
                   "vo2": {"value": 44.0, "years": None, "excluded": "derived from resting HR"}},
    "levers": {"rhr_to_floor": -0.9, "steps_7000": -2.4, "mvpa_150": -1.0, "vo2_plus_3_5": -0.5},
    "uncertainty": 2.9, "clamped": False, "levers_unclamped": True, "flags": [], "ignored": [],
    "display": "31 (range 28–34)", "caveat": "c", "age": 30, "reason": None, "change_30d": 1.4}}


def test_body_age_levers_labels_trend_and_unclamped_note(store):
    _put(store, "2026-10-14", BODY)
    text = render(store, "2026-10-14")
    assert "Body age 31 (range 28–34) vs your age 30 (provisional)" in text
    assert text.index("steps") < text.index("resting HR")  # ranked by size of effect
    assert ("Biggest levers: 7,000 steps a day -2.4 y, 150 brisk minutes a week -1.0 y, resting HR down to 60 -0.9 y"
            in text)
    assert "before the cap" in text
    assert "Trend: +1.4 y over the last 30 days" in text
    assert "risk profile" not in text  # not age_extrapolated


def test_body_age_insufficient_shows_reason(store):
    _put(store, "2026-10-14", {"score": "body_age", "status": "insufficient_data",
                               "components": {"reason": "needs 4 components, have 2", "components": {}}})
    text = render(store, "2026-10-14")
    assert "needs 4 components, have 2" in text and "population-statistics estimate" in text


RACE = {"race_km": 10.0, "race_date": "2027-02-07", "safe_by_race_km": 10.0, "riegel_min": None, "easy_share": None,
        "first_run_km": None, "first_run_by": None, "weeks_short": None, "weeks_needed": 7, "weeks_left": 15,
        "longest_km": 4.0, "increments": 10, "status": "on_track"}


def test_race_section_riegel_hidden_until_half_distance_and_shown_after(store):
    _put(store, "2026-10-14", {"score": "race", "components": RACE})
    text = render(store, "2026-10-14")
    assert "Plan: on track" in text and "Safe distance by race day: 10.0 km" in text
    assert "finish-time estimate appears once you've run at least half the race distance" in text
    _put(store, "2026-10-14", {"score": "race", "components": {**RACE, "riegel_min": 65.4, "easy_share": 0.72}})
    text = render(store, "2026-10-14")
    assert "Estimated 10 km time: 1:05 (Riegel)" in text and "Easy running share (28 days): 72%" in text


def test_race_section_no_runs_behind_is_honest(store):
    race = {**RACE, "status": "no_runs_behind", "longest_km": None, "increments": None, "first_run_km": 3.0,
            "first_run_by": None, "weeks_short": 6, "safe_by_race_km": 7.1}
    _put(store, "2026-10-14", {"score": "race", "components": race})
    section = render(store, "2026-10-14").split("Running & 10k", 1)[1].split("\n\n", 1)[0]
    assert "safely" not in section and "7.1 km" in section and "6 weeks" in section
    assert "run-walk" in section and "None" not in section


def test_activity_line_strength_and_weight_rate_only_when_known(store):
    _put(store, "2026-10-14", metrics=[{"metric": "steps_7d", "value": 6500.0},
                                       {"metric": "strength_days_week", "value": 1.4},
                                       {"metric": "weight_trend_kg", "value": 80.0}])
    text = render(store, "2026-10-14")
    assert "steps 6,500/day (7-day)" in text and "strength 1.4 days/week (target 2)" in text
    assert "weight trend 80.0 kg" in text and "%/week" not in text


def test_race_heading_follows_race_distance(store):
    _put(store, "2026-10-14", {"score": "race", "components": {**RACE, "race_km": 21.1}})
    text = render(store, "2026-10-14")
    assert "Running & 21.1k" in text and "Running & 10k" not in text


def test_race_date_passed_message(store):
    # one day after the 2027-02-07 race (the report compares the day with race_date)
    _put(store, "2027-02-08", {"score": "race", "components": {**RACE, "status": "behind", "weeks_left": 0}})
    section = render(store, "2027-02-08").split("Running & 10k", 1)[1].split("\n\n", 1)[0]
    assert "Race date passed; set a new one with `fitbit-air profile set race_date YYYY-MM-DD`" in section
    assert "Plan:" not in section


def test_cardio_with_distance_hint(store):
    race = {**RACE, "status": "no_runs", "longest_km": None, "increments": None, "first_run_km": 3.0,
            "first_run_by": "2026-11-01", "cardio_distance_sessions": 2}
    _put(store, "2026-10-14", {"score": "race", "components": race})
    assert ("Cardio sessions with distance found but no runs — log runs as 'Run' so they count toward the 10k plan"
            in render(store, "2026-10-14"))
    _put(store, "2026-10-14", {"score": "race", "components": {**race, "cardio_distance_sessions": 0}})
    assert "Cardio sessions" not in render(store, "2026-10-14")


def test_rhr_floor_label_comes_from_the_registry(store):
    class Floor55:
        def __init__(self):
            self.reg = load_registry()

        def param(self, name):
            return 55.0 if name == "lever_rhr_floor" else self.reg.param(name)

        def overrides(self):
            return {}

    _put(store, "2026-10-14", BODY)
    assert "resting HR down to 55" in render(store, "2026-10-14", reg=Floor55())


def test_body_age_lists_missing_components_with_hints(store):
    comps = {**BODY["components"], "missing": ["bmi", "sri"], "have_height": False, "have_weight": True}
    _put(store, "2026-10-14", {**BODY, "components": comps})
    text = render(store, "2026-10-14")
    assert "Not included: BMI — set height (`fitbit-air profile set height_m 1.75`)" in text
    assert "log weight" not in text.split("Not included: BMI", 1)[1].split("\n", 1)[0]
    assert "Not included: sleep regularity — needs nightly wear" in text
    comps = {**comps, "missing": ["bmi"], "have_height": True, "have_weight": False}
    _put(store, "2026-10-14", {**BODY, "components": comps})
    assert "Not included: BMI — log your weight a few times a week" in render(store, "2026-10-14")


def test_body_age_trend_prefers_90_days_and_says_stable_inside_the_deadband(store):
    _put(store, "2026-10-14", {**BODY, "components": {**BODY["components"], "change_30d": 0.4}})
    assert "Trend: stable over the last 30 days (within ±1 y)" in render(store, "2026-10-14")
    _put(store, "2026-10-14", {**BODY, "components": {**BODY["components"], "change_30d": 0.4, "change_90d": -1.4}})
    text = render(store, "2026-10-14")
    assert "Trend: -1.4 y over the last 90 days" in text and "30 days" not in text.split("Trend:", 1)[1]


def test_race_section_says_when_the_plan_starts_below_the_longest_run(store):
    _put(store, "2026-10-14", {"score": "race", "components": {**RACE, "longest_km": 9.0, "planning_km": 5.5}})
    assert "planned from 5.5 km: a jump counts as +10% at most" in render(store, "2026-10-14")
    _put(store, "2026-10-14", {"score": "race", "components": {**RACE, "planning_km": 4.0}})
    assert "planned from" not in render(store, "2026-10-14")


def test_race_day_message(store):
    _put(store, "2027-02-07", {"score": "race", "components": {**RACE, "weeks_left": 0}})
    section = render(store, "2027-02-07").split("Running & 10k", 1)[1].split("\n\n", 1)[0]
    assert "Race day — good luck! Run your own pace; run-walk is fine." in section and "passed" not in section


def test_targets_in_the_report_come_from_the_registry(store):
    _put(store, "2026-10-14", {"score": "race", "components": {**RACE, "easy_share": 0.72}},
         metrics=[{"metric": "strength_days_week", "value": 1.4}])
    text = render(store, "2026-10-14")
    assert "strength 1.4 days/week (target 2)" in text and "72% (target about 80%)" in text


def test_component_values_formatted_sensibly(store):
    comps = {**BODY["components"], "components": {"sri": {"value": 78.456, "years": 0.3},
                                                  "bmi": {"value": 27.08, "years": 0.3}}}
    _put(store, "2026-10-14", {**BODY, "components": comps})
    text = render(store, "2026-10-14")
    assert "(your value 78)" in text and "(your value 27.1)" in text


def test_report_without_a_race_says_how_to_set_one(store):
    ds = history(store, "2026-09-01", 40)
    store.save_snapshot("profile", {"age": 26})
    analyze(store, Profile(), load_registry(), date.fromisoformat(ds[-1]), date.fromisoformat(ds[-1]))
    text = render(store, ds[-1])
    assert "No race configured" in text and "profile set race_date" in text
    assert "Running & 10k" not in text


def test_report_shows_half_set_race_reason(store):
    ds = history(store, "2026-09-01", 40)
    store.save_snapshot("profile", {"age": 26})
    analyze(store, Profile(race_date="2027-06-01"), load_registry(), date.fromisoformat(ds[-1]),
            date.fromisoformat(ds[-1]))
    assert "Set both race_date and race_distance_km to get a race plan" in render(store, ds[-1])
