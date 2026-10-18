from datetime import datetime, timedelta, timezone

import pytest

from fitbit_air.analysis.baseline import Baseline
from fitbit_air.analysis.evidence import load_registry
from fitbit_air.analysis.signals import StrainTracker, cusum_step, strain_flags, stress_share, waking_sedentary_bpm

REG = load_registry()
B = lambda c, s: Baseline(c, s, 20, 0.7)


def flags(**over):
    args = dict(rhr=60, rhr_b=B(60, 1.5), resp=15, resp_b=B(15, 0.3), temp=34.5, temp_b=B(34.5, 0.1),
                google_temp_dev=0.0, hrv_status="within", hrv_night_z=0.0, spo2=96, spo2_b=B(96, 0.5))
    args.update(over)
    return strain_flags(reg=REG, **args)


def test_each_flag_fires_at_its_threshold():
    assert flags() == []
    assert flags(rhr=64.0) == ["rhr_up"]                   # z=2.67 and +4 bpm
    assert flags(rhr=63.0) == []                           # z=2.0 but only +3 bpm
    assert flags(resp=16.0) == ["resp_up"]
    assert flags(google_temp_dev=0.5) == ["temp_up"]
    assert flags(temp=34.71) == ["temp_up"]                # z=2.1
    assert flags(hrv_status="below", hrv_night_z=-1.5) == ["hrv_down"]
    assert flags(hrv_status="within", hrv_night_z=-3) == []
    assert flags(spo2=94.0) == ["spo2_down"]


def test_resp_needs_both_absolute_rise_and_zscore():
    assert flags(resp=16.0, resp_b=B(15, 1.0)) == []       # +1.0 br but z=1.0 < 1.5
    assert flags(resp=16.5, resp_b=B(15, 1.0)) == ["resp_up"]
    assert flags(resp=15.9, resp_b=B(15, 0.3)) == []       # z=3 but only +0.9 br


def test_exact_boundaries_flag():
    assert flags(rhr=64.0, rhr_b=B(60, 4 / 1.5)) == ["rhr_up"]          # z exactly 1.5 and exactly +4 bpm
    assert flags(rhr=63.99, rhr_b=B(60, 1.0)) == []                      # z huge but just under +4 bpm
    assert flags(spo2=94.0) == ["spo2_down"]                             # exactly center - 2
    assert flags(spo2=94.01) == []


def test_implausible_values_are_ignored():
    assert flags(spo2=0) == []
    assert flags(spo2=101) == []
    assert flags(rhr=300.0) == []
    assert flags(rhr=10.0, rhr_b=B(60, 1.5)) == []


def test_missing_values_never_flag():
    assert flags(rhr=None, rhr_b=None, resp=None, temp=None, google_temp_dev=None, spo2=None) == []


def test_cusum_step_is_a_plain_accumulator():
    assert cusum_step(3.5, 1.5, 0.5) == 4.5      # no reset inside the accumulator
    assert cusum_step(0.0, -2.0, 0.5) == 0.0     # floored at zero


def test_tracker_two_nights_then_drift_same_family():
    t = StrainTracker(REG)
    assert t.step(["rhr_up"], 2.0)[0] is None
    sev, conds, _ = t.step(["rhr_up"], 2.0)
    assert sev == "watch" and conds == ["rhr_2_nights"]
    sev, conds, run = t.step(["rhr_up"], 2.0)   # s = 4.5 > h and z > k
    assert set(conds) == {"rhr_2_nights", "rhr_drift"}
    assert sev == "watch" and run == 2           # both conditions are one family; persistence not reached yet


def test_multiple_flags_need_two_families():
    assert StrainTracker(REG).step(["resp_up", "temp_up"], 0.0)[0] == "watch"
    assert StrainTracker(REG).step(["rhr_up", "resp_up"], 0.0)[1] == ["multiple_flags"]
    assert StrainTracker(REG).step(["resp_up", "spo2_down"], 0.0) == (None, [], 0)  # one family (breathing)


def test_slow_drift_alone_stays_watch_indefinitely():
    t = StrainTracker(REG)
    out = [t.step([], 1.0) for _ in range(20)]
    assert [o[0] for o in out[:8]] == [None] * 8
    assert all(o[0] == "watch" and o[1] == ["rhr_drift"] for o in out[8:])
    assert [o[2] for o in out[8:]] == list(range(1, 13))    # run keeps growing
    assert t.alert_run == 0


def test_persistent_rhr_up_still_escalates():
    t = StrainTracker(REG)
    sevs = [t.step(["rhr_up"], 0.0)[0] for _ in range(5)]
    assert sevs == [None, "watch", "watch", "alert", "alert"]


def test_drift_clears_when_z_drops():
    t = StrainTracker(REG)
    for _ in range(11):
        t.step([], 1.0)                          # s = 5.5, drift active
    assert t.step([], 0.0) == (None, [], 0)      # z <= k and s > h: back to baseline, episode clears
    assert t.step([], 1.0)[0] is None            # accumulator restarted from zero


def test_drift_reset_leaves_no_residue():
    t = StrainTracker(REG)
    sevs = [t.step([], 2.0)[1] for _ in range(3)]
    assert sevs == [[], [], ["rhr_drift"]]       # s = 1.5, 3.0, 4.5
    assert t.step([], -1.0) == (None, [], 0)     # reset to 0, not 4.5 - 1.5 = 3.0
    assert t.step([], 2.0)[1] == []              # s = 1.5, no immediate drift


def test_alert_is_sticky_through_the_episode():
    t = StrainTracker(REG)
    seen = [t.step(["rhr_up"], 1.5) for _ in range(8)]
    sevs = [s[0] for s in seen]
    assert sevs[0] is None
    first_alert = sevs.index("alert")
    assert all(s == "alert" for s in sevs[first_alert:])      # never alert -> watch inside the episode
    assert t.alert_run == len(sevs) - first_alert


def test_alert_run_resets_when_episode_ends():
    t = StrainTracker(REG)
    for _ in range(4):
        t.step(["resp_up", "temp_up"], 0.0)
    assert t.alert_run == 2
    assert t.step([], 0.0)[0] is None and t.alert_run == 0


def test_persistence_escalates_and_idle_resets():
    t = StrainTracker(REG)
    sevs = [t.step(["resp_up", "temp_up"], 0.0)[0] for _ in range(3)]
    assert sevs == ["watch", "watch", "alert"]
    t.idle()
    assert t.run == 0 and t.alert_run == 0
    assert t.step(["resp_up", "temp_up"], 0.0)[2] == 1


def test_skip_breaks_two_night_pair_but_keeps_run_and_alert_run():
    t = StrainTracker(REG)
    t.step(["rhr_up"], 0.0)
    t.skip()
    assert t.step(["rhr_up"], 0.0)[0] is None               # no rhr_2_nights across the gap
    t = StrainTracker(REG)
    for _ in range(3):
        t.step(["resp_up", "temp_up"], 0.0)
    assert (t.run, t.alert_run) == (3, 1)
    t.skip()
    assert (t.run, t.alert_run) == (3, 1)
    sev, _, run = t.step(["resp_up", "temp_up"], 0.0)
    assert sev == "alert" and run == 4 and t.alert_run == 2


def test_skip_keeps_cusum_state():
    t = StrainTracker(REG)
    for _ in range(8):
        t.step([], 1.0)
    t.skip()
    assert t.step([], 1.0)[0] == "watch"                     # s carried across the gap


def test_stress_share_counts_waking_sedentary_minutes():
    t0 = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
    minutes = [(t0 + timedelta(minutes=i), 70.0 if i < 30 else 90.0) for i in range(120)]
    sedentary = [(t0, t0 + timedelta(minutes=120))]
    sleep = [(t0, t0 + timedelta(minutes=20))]
    bpm = waking_sedentary_bpm(minutes, sedentary, sleep)
    assert len(bpm) == 100
    assert stress_share(bpm, baseline_median=70.0, reg=REG) == pytest.approx(90 / 100)
    assert stress_share(bpm[:30], baseline_median=70.0, reg=REG) is None  # fewer than 60 minutes


def test_waking_sedentary_bpm_keeps_half_open_bounds_off_the_minute():
    t0 = datetime(2026, 10, 14, 4, tzinfo=timezone.utc)
    minutes = [(t0 + timedelta(minutes=i), float(i)) for i in range(30)]
    sedentary = [(t0 + timedelta(seconds=30), t0 + timedelta(minutes=10)),       # 04:00 out, 04:10 out
                 (t0 + timedelta(minutes=15), t0 + timedelta(minutes=20, seconds=1))]  # 04:20 in
    sleep = [(t0 + timedelta(minutes=3, seconds=59), t0 + timedelta(minutes=5))]  # only 04:04
    expected = [bpm for t, bpm in minutes
                if any(s <= t < e for s, e in sedentary) and not any(s <= t < e for s, e in sleep)]
    assert waking_sedentary_bpm(minutes, sedentary, sleep) == expected
    assert expected == [1, 2, 3, 5, 6, 7, 8, 9, 15, 16, 17, 18, 19, 20]
    odd = [(t0 + timedelta(minutes=2, seconds=10), 99.0)]                        # a timestamp off the minute
    assert waking_sedentary_bpm(odd, sedentary, []) == [99.0]
