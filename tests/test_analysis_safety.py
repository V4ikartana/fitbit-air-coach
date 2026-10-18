from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.safety import evaluate

REG = load_registry()
DAY = "2026-10-14"
WEEK = [f"2026-10-{d:02d}" for d in range(8, 15)]


def full(rhr=None, spo2=None, alerts=None, run=0):
    return evaluate(DAY, rhr or {}, spo2 or {}, alerts or [], run, REG)


def rails(rhr=None, spo2=None, alerts=None, run=0):
    return [r[0] for r in full(rhr, spo2, alerts, run)]


def test_no_rails_on_normal_data():
    assert rails({d: 70 for d in WEEK}, {d: 96 for d in WEEK}) == []


def test_rhr_rail_needs_three_nights_above_100_in_the_week():
    assert rails({WEEK[0]: 101, WEEK[3]: 102, WEEK[6]: 105}) == ["rhr_high"]
    assert rails({WEEK[0]: 101, WEEK[6]: 105, "2026-10-01": 110}) == []  # third night is outside the window
    assert rails({WEEK[0]: 100, WEEK[1]: 100, WEEK[2]: 100}) == []     # must exceed 100


def test_spo2_rail():
    assert rails(spo2={WEEK[1]: 91.5, WEEK[5]: 90}) == ["spo2_low"]
    assert rails(spo2={WEEK[1]: 92.0, WEEK[5]: 90}) == []


def test_afib_and_persistent_strain():
    assert rails(alerts=[(WEEK[2], "irregular_rhythm")]) == ["afib_notification"]
    assert rails(run=6) == ["strain_persistent"]
    assert rails(run=5) == []


def test_alert_outside_window_or_of_other_kind_is_ignored():
    assert rails(alerts=[("2026-10-01", "irregular_rhythm")]) == []
    assert rails(alerts=[(WEEK[2], "other_notification")]) == []


def test_implausible_values_are_ignored():
    assert rails(spo2={WEEK[1]: 0, WEEK[5]: 0}) == []
    assert rails(rhr={WEEK[0]: 300, WEEK[3]: 250, WEEK[6]: 400}) == []
    assert rails(rhr={WEEK[0]: 101, WEEK[3]: 102, WEEK[6]: 400}) == []  # only two plausible high nights


def test_strain_rail_is_driven_by_alert_run():
    assert rails(run=5) == []
    assert rails(run=6) == ["strain_persistent"]


def test_wording():
    afib = full(alerts=[(WEEK[1], "irregular_rhythm"), (WEEK[4], "irregular_rhythm")])[0][2]
    assert "isn't a diagnosis" in afib and WEEK[4] in afib and WEEK[1] not in afib
    assert "Seek urgent care" in afib
    rhr = full({WEEK[0]: 101, WEEK[3]: 102, WEEK[6]: 105})[0][2]
    assert "in the last 7 days" in rhr and rhr.endswith("chest pain, fainting or severe breathlessness.")
    spo2 = full(spo2={WEEK[1]: 91.5, WEEK[5]: 90})[0][2]
    assert "in the last 7 days" in spo2 and spo2.endswith("chest pain, fainting or severe breathlessness.")
    strain = full(run=6)[0][2]
    assert strain.endswith("chest pain, fainting or severe breathlessness.")


def test_afib_text_reuses_the_urgent_line():
    from fitbit_air.analysis.safety import URGENT
    afib = full(alerts=[(WEEK[1], "irregular_rhythm")])[0][2]
    assert afib.endswith(URGENT) and afib.count("Seek urgent care") == 1
