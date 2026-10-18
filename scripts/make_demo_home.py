"""Build a demo home: realistic made-up data for a fictional runner, "Alex", so you can try fitbit-air without a band.

The data points have the same shapes as the Google Health API v4 and are stored exactly as `fitbit-air sync` stores
real ones (raw rows, then the clean tables), then the real analysis runs. The story ends on the build day, and the
analysis scores that day. On a later day, build a new home or ask for the build day:
`fitbit-air report --date <build day>`. Alex lives at this computer's UTC offset on the build day, so local times
read correctly here; it is one fixed offset for the whole story, so daylight-saving changes are not modelled.
Usage:

    uv run python scripts/make_demo_home.py ~/.fitbit-air-demo
    FITBIT_AIR_HOME=~/.fitbit-air-demo uv run fitbit-air report
"""
from __future__ import annotations

import argparse
import os
import random
import shlex
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

from fitbit_air import normalize
from fitbit_air.analysis.profile import set_value
from fitbit_air.analysis.runner import run_analysis
from fitbit_air.config import DEFAULT_HOME, Paths
from fitbit_air.home import init_home
from fitbit_air.registry import BY_NAME
from fitbit_air.store import Store

UTC = timezone.utc
REAL_HOME = DEFAULT_HOME.expanduser()
RACE_AFTER_DAYS = 70
DEMO_AGE = 38  # Google's profile gives the age; body age needs it
MIN_DAYS = 14  # recovery needs 7 valid days and the strain signals 14

# Alex's heart rate (bpm) by what he is doing; each reading adds a little noise
SLEEP_BPM, REST_BPM, WALK_BPM, STRENGTH_BPM, EASY_BPM, TEMPO_BPM = 52, 72, 108, 112, 136, 158
WAKE_TIME = time(6, 15)  # give or take 15 minutes; bedtime follows from the night's length
RUN_DAYS = (1, 3, 6)  # Tuesday easy, Thursday tempo, Sunday long run
TEMPO_DAY, LONG_RUN_DAY, STRENGTH_DAY = 3, 6, 5
WEIGH_IN_DAYS = (0, 3, 5)
TEMPO_WARM_UP_MIN = 10
WALK_STARTS = (time(12, 31), time(18, 31))  # just after that half-hour's background steps
STRENGTH_START, STRENGTH_MIN = time(10, 0), 40
DESK_HOURS = ((9, 12), (14, 17))  # weekdays only


class DemoError(Exception):
    """The demo home can't be built where asked (not empty, a file, or inside the real home)."""


# ---- API-shaped builders: every time is an aware local datetime ---------------------------------------------------

def _iso(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _offset(t: datetime) -> str:
    return f"{int(t.utcoffset().total_seconds())}s"


def _date(d: date) -> dict:
    return {"year": d.year, "month": d.month, "day": d.day}


def _civil(t: datetime) -> dict:
    return {"date": _date(t.date()), "time": {"hours": t.hour, "minutes": t.minute, "seconds": t.second}}


def _sample_time(t: datetime) -> dict:
    return {"physicalTime": _iso(t), "utcOffset": _offset(t), "civilTime": _civil(t)}


def _interval(a: datetime, b: datetime) -> dict:
    return {"startTime": _iso(a), "endTime": _iso(b), "startUtcOffset": _offset(a), "endUtcOffset": _offset(b),
            "civilStartTime": _civil(a), "civilEndTime": _civil(b)}


def _point(dtype: str, body: dict, name: str | None = None) -> tuple[str, dict]:
    point = {BY_NAME[dtype].field: body}
    if name:
        point["name"] = f"users/me/dataTypes/{dtype}/dataPoints/{name}"
    return dtype, point


def _minutes(start: datetime, n: int):
    return (start + timedelta(minutes=m) for m in range(n))


# ---- the plan of each night and day ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class Night:
    bed: datetime
    wake: datetime
    stages: tuple[tuple[str, int], ...]  # (stage, minutes) from bed to wake

    def asleep(self, t: datetime) -> bool:
        return self.bed <= t < self.wake


@dataclass(frozen=True)
class Run:
    start: datetime
    km: float
    minutes: float
    tempo: bool  # a tempo run: easy warm-up, then hard

    def bpm(self, t: datetime) -> int:
        warm = (t - self.start).total_seconds() < TEMPO_WARM_UP_MIN * 60
        return TEMPO_BPM if self.tempo and not warm else EASY_BPM

    def avg_bpm(self) -> int:
        if not self.tempo:
            return EASY_BPM
        easy = min(self.minutes, TEMPO_WARM_UP_MIN)
        return round((EASY_BPM * easy + TEMPO_BPM * (self.minutes - easy)) / self.minutes)

    def covers(self, t: datetime) -> bool:
        return 0 <= (t - self.start).total_seconds() < self.minutes * 60


def _plan_night(rng: random.Random, wake_day: date, tz: timezone, short: bool) -> Night:
    """Five sleep cycles ending about 06:15; a short night is three cycles after a late bedtime, same wake time."""
    stages = [("AWAKE", 8)]  # falling asleep
    for c in range(3 if short else 5):
        stages += [("LIGHT", rng.randint(38, 52)), ("DEEP", max(8, 34 - 6 * c + rng.randint(-4, 4))),
                   ("REM", 14 + 5 * c + rng.randint(-3, 3))]
        if c in (1, 3):
            stages.append(("AWAKE", rng.randint(3, 9)))
    stages.append(("AWAKE", 4))
    wake = datetime.combine(wake_day, WAKE_TIME, tz) + timedelta(minutes=rng.randint(-15, 15))
    return Night(bed=wake - timedelta(minutes=sum(m for _, m in stages)), wake=wake, stages=tuple(stages))


def _plan_run(rng: random.Random, d: date, i: int, wake: datetime) -> Run:
    """Distance grows a quarter km a week; the Sunday long run is 40% longer; pace improves slowly."""
    week = i // 7
    km = (3.0 + 0.25 * week) * (1.4 if d.weekday() == LONG_RUN_DAY else 1.0)
    pace = 6.6 - 0.04 * week + rng.uniform(-0.1, 0.1)  # min/km
    start = wake + timedelta(minutes=rng.randint(30, 45))
    return Run(start=start, km=km, minutes=km * pace, tempo=d.weekday() == TEMPO_DAY)


# ---- data points ----------------------------------------------------------------------------------------------------

def _sleep(night: Night) -> tuple[str, dict]:
    spans, t = [], night.bed
    for kind, m in night.stages:
        end = t + timedelta(minutes=m)
        spans.append({"type": kind, "startTime": _iso(t), "startUtcOffset": _offset(t),
                      "endTime": _iso(end), "endUtcOffset": _offset(end)})
        t = end
    minutes, counts = Counter(), Counter()
    for kind, m in night.stages:
        minutes[kind] += m
        counts[kind] += 1
    period = sum(minutes.values())
    body = {"interval": _interval(night.bed, night.wake), "type": "STAGES",
            "updateTime": _iso(night.wake + timedelta(minutes=5)),
            "metadata": {"mainSleep": True, "nap": False, "processed": True}, "stages": spans,
            "summary": {"minutesAsleep": str(period - minutes["AWAKE"]), "minutesAwake": str(minutes["AWAKE"]),
                        "minutesInSleepPeriod": str(period), "minutesToFallAsleep": str(night.stages[0][1]),
                        "minutesAfterWakeUp": str(night.stages[-1][1]),
                        "stagesSummary": [{"type": k, "minutes": str(minutes[k]), "count": str(counts[k])}
                                          for k in minutes]}}
    return _point("sleep", body, f"demo-sleep-{night.wake.date().isoformat()}")


def _vitals(rng: random.Random, d: date, p: float, rough: bool) -> list[tuple[str, dict]]:
    """The nightly summaries. Fitness improves across the story (p from 0 to 1). A rough night (late and short) shows
    as resting HR +5, HRV -15% and a warmer night (+0.6 C), which the strain signals pick up as a watch."""
    hrv = (48 + 7 * p + rng.uniform(-3, 3)) * (0.85 if rough else 1.0)
    return [
        _point("daily-resting-heart-rate", {"date": _date(d), "beatsPerMinute": str(round(
            58 - 3 * p + rng.uniform(-1, 1) + (5 if rough else 0)))}),
        _point("daily-heart-rate-variability", {
            "date": _date(d), "averageHeartRateVariabilityMilliseconds": round(hrv, 1),
            "deepSleepRootMeanSquareOfSuccessiveDifferencesMilliseconds": round(54 + 6 * p + rng.uniform(-3, 3), 1),
            "entropy": round(2.6 + rng.uniform(-0.1, 0.1), 2), "nonRemHeartRateBeatsPerMinute": str(54)}),
        _point("daily-respiratory-rate", {"date": _date(d),
                                          "breathsPerMinute": round(14.4 + rng.uniform(-0.3, 0.3), 1)}),
        _point("daily-oxygen-saturation", {"date": _date(d),
                                           "averagePercentage": round(96.6 + rng.uniform(-0.5, 0.5), 1),
                                           "lowerBoundPercentage": 94.8, "upperBoundPercentage": 98.4}),
        _point("daily-sleep-temperature-derivations", {
            "date": _date(d), "nightlyTemperatureCelsius": round(33.6 + rng.uniform(-0.15, 0.15)
                                                                 + (0.6 if rough else 0.0), 2),
            "baselineTemperatureCelsius": 33.55, "relativeNightlyStddev30dCelsius": 0.2}),
    ]


def _heart_rate(rng: random.Random, midnight: datetime, nights: tuple[Night, Night], run: Run | None,
                walks: list[tuple[datetime, int]], lift: datetime | None) -> list[tuple[str, dict]]:
    """A reading every minute (the band is worn all day), plus Google's active zone minutes: 2 per cardio minute,
    1 per fat-burn minute. `nights` are the night ending this morning and the one starting this evening."""
    pts = []
    for t in _minutes(midnight, 1440):
        walking = any(0 <= (t - w).total_seconds() < n * 60 for w, n in walks)
        lifting = lift is not None and 0 <= (t - lift).total_seconds() < STRENGTH_MIN * 60
        zone = None
        if any(n.asleep(t) for n in nights):
            bpm = SLEEP_BPM
        elif run is not None and run.covers(t):
            bpm, zone = run.bpm(t), "CARDIO"
        elif walking:
            bpm, zone = WALK_BPM, "FAT_BURN"
        elif lifting:
            bpm, zone = STRENGTH_BPM, "FAT_BURN"
        else:
            bpm = REST_BPM
        pts.append(_point("heart-rate", {"sampleTime": _sample_time(t),
                                         "beatsPerMinute": str(bpm + rng.randint(-3, 3))}))
        if zone:
            pts.append(_point("active-zone-minutes", {"interval": _interval(t, t + timedelta(minutes=1)),
                                                      "heartRateZone": zone,
                                                      "activeZoneMinutes": "2" if zone == "CARDIO" else "1"}))
    return pts


def _steps(rng: random.Random, midnight: datetime, walks: list[tuple[datetime, int]],
           desk: bool) -> list[tuple[str, dict]]:
    pts = []
    for h in range(8, 21):  # background steps, too slow to count as brisk walking
        a = midnight + timedelta(hours=h)
        pts.append(_point("steps", {"interval": _interval(a, a + timedelta(minutes=30)),
                                    "count": str(rng.randint(260, 420))}))
    for start, n in walks:  # brisk walks, minute by minute at walking cadence
        for a in _minutes(start, n):
            pts.append(_point("steps", {"interval": _interval(a, a + timedelta(minutes=1)),
                                        "count": str(rng.randint(108, 124))}))
    for h0, h1 in DESK_HOURS if desk else ():
        a, b = midnight + timedelta(hours=h0), midnight + timedelta(hours=h1)
        pts.append(_point("activity-level", {"interval": _interval(a, b), "activityLevelType": "SEDENTARY"}))
    return pts


def _run(run: Run) -> tuple[str, dict]:
    end = run.start + timedelta(minutes=run.minutes)
    return _point("exercise", {
        "interval": _interval(run.start, end), "exerciseType": "RUNNING", "displayName": "Run",
        "activeDuration": f"{int(run.minutes * 60)}s",
        "metricsSummary": {"averageHeartRateBeatsPerMinute": str(run.avg_bpm()),
                           "caloriesKcal": round(run.km * 68, 1), "distanceMillimeters": int(run.km * 1_000_000),
                           "steps": str(int(run.minutes * 165)), "activeZoneMinutes": str(int(run.minutes) * 2)}},
        f"demo-run-{run.start.date().isoformat()}")


def _weights(rng: random.Random, start: datetime) -> tuple[str, dict]:
    end = start + timedelta(minutes=STRENGTH_MIN)
    return _point("exercise", {
        "interval": _interval(start, end), "exerciseType": "STRENGTH_TRAINING", "displayName": "Weights",
        "activeDuration": f"{STRENGTH_MIN * 60}s",
        "metricsSummary": {"averageHeartRateBeatsPerMinute": str(STRENGTH_BPM + rng.randint(-3, 3)),
                           "caloriesKcal": 190.0, "activeZoneMinutes": str(STRENGTH_MIN)}},
        f"demo-weights-{start.date().isoformat()}")


def _weigh_in(rng: random.Random, at: datetime, days_left: int) -> tuple[str, dict]:
    """Down about 0.23 kg a week, to 76.8 kg on the last day."""
    kg = 76.8 + 0.033 * days_left + rng.uniform(-0.15, 0.15)
    return _point("weight", {"sampleTime": _sample_time(at), "weightGrams": int(kg * 1000)},
                  f"demo-weight-{at.date().isoformat()}")


def _day(rng: random.Random, d: date, i: int, days: int, nights: tuple[Night, Night]) -> list[tuple[str, dict]]:
    """Day `i` of the story. The last day follows a rough short night, so it is a rest day."""
    rough = i == days - 1
    wake = nights[0].wake
    tz = wake.tzinfo
    midnight = datetime.combine(d, time(0), tz)
    pts = _vitals(rng, d, i / max(days - 1, 1), rough)
    run = _plan_run(rng, d, i, wake) if d.weekday() in RUN_DAYS and not rough else None
    walks = [(datetime.combine(d, s, tz), rng.randint(18, 26)) for s in WALK_STARTS]
    lift = datetime.combine(d, STRENGTH_START, tz) if d.weekday() == STRENGTH_DAY else None
    pts += _heart_rate(rng, midnight, nights, run, walks, lift)
    pts += _steps(rng, midnight, walks, desk=d.weekday() < 5)
    if run is not None:
        pts.append(_run(run))
    if lift is not None:
        pts.append(_weights(rng, lift))
    if d.weekday() in WEIGH_IN_DAYS:
        pts.append(_weigh_in(rng, wake + timedelta(minutes=10), days - 1 - i))
    return pts


def local_offset_s(day: date) -> int:
    """This computer's UTC offset (seconds) at noon on `day`."""
    return int(datetime.combine(day, time(12)).astimezone().utcoffset().total_seconds())


def story(end: date, days: int, seed: int, offset_s: int | None = None) -> list[tuple[str, dict]]:
    """Every data point of a `days`-long story ending on `end`, deterministic for a seed. Alex lives at UTC offset
    `offset_s` (default: this computer's) for the whole story: one fixed offset, so daylight-saving changes are not
    modelled. The night after `end` is planned too, so the last evening's heart rate knows his bedtime, but it isn't
    stored. The last day's minute data runs to midnight, even though the recorded sync is stamped at build time."""
    rng = random.Random(seed)
    tz = timezone(timedelta(seconds=local_offset_s(end) if offset_s is None else offset_s))
    first = end - timedelta(days=days - 1)
    nights = [_plan_night(rng, first + timedelta(days=i), tz, short=i == days - 1) for i in range(days + 1)]
    pts = [_point("height", {"sampleTime": _sample_time(datetime.combine(first, time(8), tz)),
                             "heightMillimeters": "1780"}, "demo-height")]
    for i in range(days):
        pts.append(_sleep(nights[i]))
        pts += _day(rng, first + timedelta(days=i), i, days, (nights[i], nights[i + 1]))
    return pts


# ---- building -------------------------------------------------------------------------------------------------------

def _outside_real_home(p: Path) -> Path:
    """`p` resolved (symlinks followed); DemoError if that is the real home or anything inside it. The path test
    misses another spelling of the same folder (macOS disks ignore case), so the folders on disk are compared too."""
    resolved = Path(p).expanduser().resolve()
    real = REAL_HOME.expanduser().resolve()
    inside = resolved.is_relative_to(real) or (
        real.exists() and any(q.exists() and os.path.samefile(q, real) for q in (resolved, *resolved.parents)))
    if inside:
        raise DemoError(f"refusing to use {p}: it is the real home {REAL_HOME} or inside it")
    return resolved


def _store(store: Store, points: list[tuple[str, dict]]) -> None:
    """Store the points as sync does, one batch per data type, and record a finished sync run."""
    run_id = store.start_run("demo")
    by_type: dict[str, list] = {}
    for dtype, point in points:
        by_type.setdefault(dtype, []).append(normalize.to_raw_row(BY_NAME[dtype], point))
    for dtype, rows in by_type.items():
        normalize.rebuild(store, BY_NAME[dtype], store.upsert_raw(rows))
    store.finish_run(run_id, len(points), len(by_type), 0, None)


def build(home: Path, end: date | None = None, days: int = 90, seed: int = 7, analyse: bool = True,
          offset_s: int | None = None) -> Paths:
    """Create a demo home in `home` (new or empty, never the real home) with a `days`-long story ending on `end`
    (default today) at UTC offset `offset_s` (default: this computer's), then analyse the whole story as of `end`."""
    home = Path(home).expanduser()
    _outside_real_home(home)
    if home.exists() and not home.is_dir():
        raise DemoError(f"{home} is not a folder; pick a new folder")
    if home.exists() and any(home.iterdir()):
        raise DemoError(f"{home} is not empty; pick a new folder")
    end = end or date.today()
    paths = Paths(home)
    init_home(paths, template="runner-10k")
    for key, value in (("sex", "male"), ("race_date", (end + timedelta(days=RACE_AFTER_DAYS)).isoformat()),
                       ("height_m", "1.78")):
        set_value(paths.profile_file, key, value)
    store = Store(paths.db)
    try:
        store.save_snapshot("profile", {"age": DEMO_AGE})  # sync saves Google's profile the same way
        _store(store, story(end, days, seed, offset_s))
        if analyse:
            run_analysis(store, paths, since=end - timedelta(days=days - 1), today=end)
    finally:
        store.close()
    return paths


# ---- facts for the demo video ---------------------------------------------------------------------------------------

INIT_ARGS = ["init", "--template", "runner-10k", "--home", "~/.fitbit-air-demo"]


def _check_facts_path(out: Path) -> None:
    """DemoError if `out` is in the real home, or would overwrite a file that isn't a .json file."""
    _outside_real_home(out)
    if Path(out).expanduser().exists() and Path(out).suffix.lower() != ".json":
        raise DemoError(f"{out} exists and is not a .json file; pick a new facts file")


def write_facts(home: Path, out: Path, today: date | None = None) -> Path:
    """The real output of the commands and coach tools the video shows, run on the demo home."""
    import json

    from fitbit_air.analysis import report
    from fitbit_air.coach import snapshot as snapshot_mod
    from fitbit_air.coach import tools
    from fitbit_air.coach.tools import Ctx
    from fitbit_air.userconfig import registry_for

    _outside_real_home(home)
    _check_facts_path(out)
    paths = Paths(Path(home).expanduser())
    today = today or date.today()
    reg, _ = registry_for(paths)
    store = Store(paths.db)
    try:
        ctx = Ctx(store=store, reg=reg, now=datetime.now(UTC), today=today, paths=paths)
        facts = {
            "init_command": "fitbit-air " + " ".join(INIT_ARGS),
            "init_output": _init_text(),
            "report": report.render(store, today.isoformat(), reg=reg),
            "overview": tools.get_overview(ctx),
            "sleep": tools.get_sleep(ctx, days=7),
            "running": tools.get_running(ctx, days=90),
            "snapshot": snapshot_mod.build_snapshot(ctx),
        }
    finally:
        store.close()
    out = Path(out).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(facts, indent=2, default=str), encoding="utf-8")
    return out


def _init_text() -> str:
    """What `fitbit-air init --template runner-10k --home ~/.fitbit-air-demo` prints, run on a throwaway home; the
    throwaway folder is shown as `~` so no real path ends up in the facts."""
    import tempfile

    from typer.testing import CliRunner

    from fitbit_air.cli import app

    with tempfile.TemporaryDirectory() as tmp:
        target = str(Path(tmp) / ".fitbit-air-demo")
        args = [target if a == "~/.fitbit-air-demo" else a for a in INIT_ARGS]
        # FITBIT_AIR_HOME points at the throwaway home too, so nothing looks at the real default home
        result = CliRunner().invoke(app, args, env={"FITBIT_AIR_HOME": target})
    if result.exit_code != 0:
        raise DemoError(f"`fitbit-air init` failed: {result.output.strip()}")
    return result.output.replace(tmp, "~")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("home", help="a new, empty folder, e.g. ~/.fitbit-air-demo")
    ap.add_argument("--days", type=int, default=90, help=f"length of the story in days (at least {MIN_DAYS})")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--facts", metavar="PATH", help="also write the real report and coach tool output as JSON")
    args = ap.parse_args(argv)
    if args.days < MIN_DAYS:
        ap.error(f"--days must be at least {MIN_DAYS} (recovery needs 7 valid days, the strain signals 14)")
    today = date.today()
    facts = Path(args.facts) if args.facts else None
    try:
        if facts is not None:
            _check_facts_path(facts)  # before building, so a bad path costs nothing
        paths = build(Path(args.home), end=today, days=args.days, seed=args.seed)
        if facts is not None:
            print(f"facts written: {shlex.quote(str(write_facts(paths.root, facts, today=today)))}")
    except DemoError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    home = shlex.quote(str(paths.root))
    print(f"demo home ready: {home}\nTry: FITBIT_AIR_HOME={home} uv run fitbit-air report")
    return 0


if __name__ == "__main__":
    sys.exit(main())
