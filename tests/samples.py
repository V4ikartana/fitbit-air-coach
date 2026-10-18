"""Builders for synthetic Google Health API v4 data points."""
from datetime import datetime, timedelta

LOCAL_OFFSET_S = 34200  # a fixed non-UTC, half-hour local offset (+09:30)
LOCAL_UTC_OFFSET = f"{LOCAL_OFFSET_S}s"


def _dt(ts: str) -> datetime:
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def date_obj(d: str) -> dict:
    y, m, dd = (int(x) for x in d.split("-"))
    return {"year": y, "month": m, "day": dd}


def civil(ts: str) -> dict:
    loc = _dt(ts) + timedelta(seconds=LOCAL_OFFSET_S)
    return {"date": date_obj(loc.date().isoformat()), "time": {"hours": loc.hour, "minutes": loc.minute, "seconds": loc.second}}


def sample_time(ts: str) -> dict:
    return {"physicalTime": ts, "utcOffset": LOCAL_UTC_OFFSET, "civilTime": civil(ts)}


def interval(start: str, end: str) -> dict:
    return {"startTime": start, "endTime": end, "startUtcOffset": LOCAL_UTC_OFFSET, "endUtcOffset": LOCAL_UTC_OFFSET,
            "civilStartTime": civil(start), "civilEndTime": civil(end)}


# ---- samples ----
def heart_rate(ts, bpm=60):
    return {"heartRate": {"sampleTime": sample_time(ts), "beatsPerMinute": str(bpm), "metadata": {"motionContext": "SEDENTARY"}}}


def hrv_sample(ts, rmssd=40.0, sdnn=55.0):
    return {"heartRateVariability": {"sampleTime": sample_time(ts), "rootMeanSquareOfSuccessiveDifferencesMilliseconds": rmssd,
                                     "standardDeviationMilliseconds": sdnn}}


def spo2(ts, pct=96.5):
    return {"oxygenSaturation": {"sampleTime": sample_time(ts), "percentage": pct}}


def weight(ts, grams=72500):
    return {"weight": {"sampleTime": sample_time(ts), "weightGrams": grams}}


# ---- daily ----
def daily_rhr(d, bpm=58):
    return {"dailyRestingHeartRate": {"date": date_obj(d), "beatsPerMinute": str(bpm)}}


def daily_hrv(d, rmssd=42.0, deep=48.0, entropy=2.9, non_rem=55):
    return {"dailyHeartRateVariability": {"date": date_obj(d), "averageHeartRateVariabilityMilliseconds": rmssd,
                                          "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": deep,
                                          "entropy": entropy, "nonRemHeartRateBeatsPerMinute": str(non_rem)}}


def daily_temp(d, nightly=34.9, baseline=34.6, sd=0.2):
    return {"dailySleepTemperatureDerivations": {"date": date_obj(d), "nightlyTemperatureCelsius": nightly,
                                                 "baselineTemperatureCelsius": baseline, "relativeNightlyStddev30dCelsius": sd}}


def daily_hr_zones(d):
    return {"dailyHeartRateZones": {"date": date_obj(d), "heartRateZones": [
        {"heartRateZoneType": "LIGHT", "minBeatsPerMinute": "95", "maxBeatsPerMinute": "114"},
        {"heartRateZoneType": "PEAK", "minBeatsPerMinute": "162", "maxBeatsPerMinute": "220"},
    ]}}


# ---- intervals ----
def steps(start, end, count=100):
    return {"steps": {"interval": interval(start, end), "count": str(count)}}


def azm(start, end, zone="CARDIO", minutes=2):
    return {"activeZoneMinutes": {"interval": interval(start, end), "heartRateZone": zone, "activeZoneMinutes": str(minutes)}}


def active_minutes(start, end):
    return {"activeMinutes": {"interval": interval(start, end), "activeMinutesByActivityLevel": [
        {"activityLevel": "LIGHT", "activeMinutes": "10"}, {"activityLevel": "VIGOROUS", "activeMinutes": "5"}]}}


def time_in_zone(start, end, zone="MODERATE"):
    return {"timeInHeartRateZone": {"interval": interval(start, end), "heartRateZoneType": zone}}


# ---- sessions ----
def exercise(name, start, end):
    return {"name": f"users/me/dataTypes/exercise/dataPoints/{name}", "exercise": {
        "interval": interval(start, end), "exerciseType": "RUNNING", "displayName": "Run", "activeDuration": "1800s",
        "metricsSummary": {"averageHeartRateBeatsPerMinute": "148", "caloriesKcal": 320.5, "steps": "4100",
                           "distanceMillimeters": 5000000, "activeZoneMinutes": "28"}}}


def irn(start, end):
    return {"irregularRhythmNotification": {"interval": interval(start, end), "alertWindows": []}}


def sleep(name, start, end, stages, update_time="2026-10-13T02:00:00Z", main=True):
    """stages: list of (STAGE_TYPE, start, end)."""
    mins: dict[str, float] = {}
    for t, a, b in stages:
        mins[t] = mins.get(t, 0) + (_dt(b) - _dt(a)).total_seconds() / 60
    period = (_dt(end) - _dt(start)).total_seconds() / 60
    asleep = sum(v for k, v in mins.items() if k != "AWAKE")
    return {"name": f"users/me/dataTypes/sleep/dataPoints/{name}", "sleep": {
        "interval": interval(start, end), "type": "stages", "updateTime": update_time,
        "metadata": {"mainSleep": main, "nap": not main, "processed": True},
        "stages": [{"type": t, "startTime": a, "endTime": b} for t, a, b in stages],
        "summary": {"minutesAsleep": str(int(asleep)), "minutesAwake": str(int(mins.get("AWAKE", 0))),
                    "minutesInSleepPeriod": str(int(period)), "minutesToFallAsleep": "10", "minutesAfterWakeUp": "3",
                    "stagesSummary": [{"type": t, "minutes": str(int(v)), "count": "1"} for t, v in mins.items()]}}}


# A standard night: 22:30 -> 06:30 local, waking on 2026-10-13. Asleep 450 of 480 min.
NIGHT_STAGES = [
    ("LIGHT", "2026-10-12T13:00:00Z", "2026-10-12T15:00:00Z"),
    ("DEEP", "2026-10-12T15:00:00Z", "2026-10-12T16:30:00Z"),
    ("REM", "2026-10-12T16:30:00Z", "2026-10-12T18:00:00Z"),
    ("AWAKE", "2026-10-12T18:00:00Z", "2026-10-12T18:30:00Z"),
    ("LIGHT", "2026-10-12T18:30:00Z", "2026-10-12T21:00:00Z"),
]


# ---- generic builders for types without a dedicated helper ----
def sample_point(field, ts, **body):
    return {field: {"sampleTime": sample_time(ts), **body}}


def daily_point(field, d, **body):
    return {field: {"date": date_obj(d), **body}}


def interval_point(field, start, end, **body):
    return {field: {"interval": interval(start, end), **body}}
