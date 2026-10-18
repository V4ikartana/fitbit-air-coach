import logging
from datetime import datetime, timedelta

import pytest
import samples
from fitbit_air import normalize
from fitbit_air.config import HISTORY_FLOOR
from fitbit_air.errors import ApiError, NormalizeError, ReauthRequired
from fitbit_air.registry import DataType, Kind
from fitbit_air.sync import Syncer, forward_windows
from fitbit_air.timeutil import UTC, iso_utc, parse_ts

NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)
STEPS = DataType("steps", Kind.INTERVAL, window_days=7)
WEIGHT = DataType("weight", Kind.SAMPLE, window_days=90)


class FakeClient:
    """Serves points whose start falls in the requested window; records every window requested."""

    def __init__(self, points=None, errors=None, fail_on_call=None, fail_with=None):
        self.points = points or {}
        self.errors = errors or {}
        self.fail_on_call = fail_on_call
        self.fail_with = fail_with
        self.windows = []

    def iter_points(self, dt, start, end):
        self.windows.append((dt.name, start, end))
        if dt.name in self.errors:
            raise self.errors[dt.name]
        if self.fail_on_call == len(self.windows):
            raise self.fail_with
        yield [p for p in self.points.get(dt.name, []) if start <= _start(dt, p) < end]

    def get_profile(self):
        return {"age": 35}

    def list_paired_devices(self):
        return {"pairedDevices": []}


def _start(dt, point):
    body = point[dt.field]
    return parse_ts(body["sampleTime"]["physicalTime"] if "sampleTime" in body else body["interval"]["startTime"])


def steps_history(days):
    """One 1-minute steps point per day, at NOW - i days - 1 hour, for i in 0..days-1."""
    out = []
    for i in range(days):
        start = NOW - timedelta(days=i, hours=1)
        out.append(samples.steps(iso_utc(start), iso_utc(start + timedelta(minutes=1)), 100))
    return out


def syncer(store, client, registry=(STEPS,), now=NOW):
    return Syncer(store, client, registry=registry, now=lambda: now)


def test_forward_windows():
    a = NOW - timedelta(days=10)
    assert forward_windows(a, NOW, 7) == [(a, a + timedelta(days=7)), (a + timedelta(days=7), NOW)]
    assert forward_windows(NOW, NOW, 7) == []


def test_first_run_backfills_full_history(store):
    client = FakeClient({"steps": steps_history(200)})
    result = syncer(store, client).run()
    assert [(r.name, r.status, r.points) for r in result.results] == [("steps", "ok", 200)]
    assert store.raw_count("steps") == 200
    assert store.table_counts()["activity_intervals"] == 200
    st = store.get_state("steps")
    assert st.backfill_done and st.watermark_utc == NOW and st.status == "ok"


def test_backfill_walks_to_the_floor_with_doubling_empty_windows(store):
    client = FakeClient({"steps": steps_history(5)})
    syncer(store, client).run()
    sizes = [(end - start).days for _, start, end in client.windows]
    # One window holds the data (and resets the gallop), then empty windows double up to the cap.
    assert sizes[:9] == [7, 7, 14, 28, 56, 112, 224, 365, 365]
    assert max(sizes) == 365
    assert min(w[1] for w in client.windows) == HISTORY_FLOOR
    assert store.get_state("steps").backfill_done and store.get_state("steps").backfill_cursor_utc == HISTORY_FLOOR


def test_backfill_logs_one_info_line_per_window(store, caplog):
    client = FakeClient({"steps": steps_history(5)})
    with caplog.at_level(logging.INFO, logger="fitbit_air.sync"):
        syncer(store, client).run()
    lines = [r.getMessage() for r in caplog.records if " backfill " in r.getMessage()]
    assert len(lines) == len(client.windows)
    assert lines[0] == f"steps backfill {(NOW - timedelta(days=7)).date()}..{NOW.date()}: 5 points"


def test_sparse_sample_logged_once_400_days_ago_is_found(store):
    old = samples.weight(iso_utc(NOW - timedelta(days=400)))
    client = FakeClient({"weight": [old]})
    result = syncer(store, client, registry=(WEIGHT,)).run()
    assert [(r.name, r.status, r.points) for r in result.results] == [("weight", "ok", 1)]
    assert store.table_counts()["samples"] == 1


def test_data_after_a_long_gap_is_found(store):
    recent = steps_history(3)
    old = []
    for i in range(3):
        start = NOW - timedelta(days=110 + i, hours=1)
        old.append(samples.steps(iso_utc(start), iso_utc(start + timedelta(minutes=1)), 50))
    client = FakeClient({"steps": recent + old})
    syncer(store, client).run()
    assert store.raw_count("steps") == 6


def test_empty_type_reaches_the_floor_in_few_requests(store):
    daily_window = DataType("steps", Kind.INTERVAL, window_days=1)
    client = FakeClient()
    result = syncer(store, client, registry=(daily_window,)).run()
    assert result.results[0].status == "empty"
    assert len(client.windows) < 25
    assert store.get_state("steps").backfill_done


def test_first_incremental_does_a_deep_rescan_then_three_day_overlap(store):
    syncer(store, FakeClient({"steps": steps_history(20)})).run()
    assert store.get_state("steps").deep_scan_utc is None  # the backfill itself is not a deep scan

    day1 = NOW + timedelta(days=1)
    client = FakeClient({"steps": steps_history(20)})
    syncer(store, client, now=day1).run()
    assert client.windows == [("steps", NOW - timedelta(days=14), NOW - timedelta(days=7)),
                              ("steps", NOW - timedelta(days=7), NOW),
                              ("steps", NOW, day1)]
    st = store.get_state("steps")
    assert st.watermark_utc == day1 and st.deep_scan_utc == day1

    soon = day1 + timedelta(hours=2)
    client = FakeClient({"steps": steps_history(20)})
    syncer(store, client, now=soon).run()
    assert client.windows == [("steps", day1 - timedelta(days=3), soon)]
    assert store.get_state("steps").deep_scan_utc == day1  # unchanged

    tomorrow = day1 + timedelta(days=1, hours=3)
    client = FakeClient({"steps": steps_history(20)})
    syncer(store, client, now=tomorrow).run()
    assert client.windows[0] == ("steps", soon - timedelta(days=14), soon - timedelta(days=7))
    assert store.get_state("steps").deep_scan_utc == tomorrow


def test_failed_forward_pass_does_not_count_as_a_deep_scan(store):
    syncer(store, FakeClient({"steps": steps_history(20)})).run()
    client = FakeClient({"steps": steps_history(20)}, fail_on_call=2, fail_with=ApiError(503, "busy"))
    syncer(store, client, now=NOW + timedelta(days=1)).run()
    assert store.get_state("steps").deep_scan_utc is None


def test_rerun_is_idempotent(store):
    points = {"steps": steps_history(30)}
    syncer(store, FakeClient(points)).run()
    syncer(store, FakeClient(points)).run(full_backfill=True)
    assert store.raw_count("steps") == 30
    assert store.table_counts()["activity_intervals"] == 30


def test_interrupted_backfill_resumes_from_cursor(store):
    points = {"steps": steps_history(200)}
    first = FakeClient(points, fail_on_call=5, fail_with=ApiError(503, "busy"))
    result = syncer(store, first).run()
    assert result.results[0].status == "failed"
    st = store.get_state("steps")
    assert not st.backfill_done and st.backfill_cursor_utc == NOW - timedelta(days=28)

    second = FakeClient(points)
    syncer(store, second).run()
    assert second.windows[:2] == [("steps", NOW - timedelta(days=14), NOW - timedelta(days=7)),
                                  ("steps", NOW - timedelta(days=7), NOW)]  # incremental part (first deep scan)
    assert second.windows[2] == ("steps", NOW - timedelta(days=35), NOW - timedelta(days=28))  # resumed backfill
    assert store.raw_count("steps") == 200


def test_unavailable_type_does_not_stop_others(store):
    client = FakeClient({"steps": steps_history(5)}, errors={"weight": ApiError(403, "scope not granted")})
    result = syncer(store, client, registry=(WEIGHT, STEPS)).run()
    assert [(r.name, r.status) for r in result.results] == [("weight", "unavailable"), ("steps", "ok")]
    assert "403" in store.get_state("weight").last_error
    assert store.last_run()["types_failed"] == 0


def test_reauth_stops_the_run(store):
    client = FakeClient(errors={"steps": ReauthRequired("invalid_grant")})
    result = syncer(store, client, registry=(STEPS, WEIGHT)).run()
    assert result.reauth_required
    assert [w[0] for w in client.windows] == ["steps"]
    assert store.last_run()["error_summary"] == "reauth_required"
    assert store.get_snapshot("profile") is None
    assert store.get_state("steps").status == "failed"
    assert "reauth required" in store.get_state("steps").last_error


def test_empty_type_is_reported_empty(store):
    client = FakeClient()
    result = syncer(store, client, registry=(WEIGHT,)).run()
    assert result.results[0].status == "empty"
    assert client.windows[-1][1] == HISTORY_FLOOR  # an empty type is still walked to the floor


def test_snapshots_saved(store):
    syncer(store, FakeClient()).run()
    assert store.get_snapshot("profile") == {"age": 35}
    assert store.get_snapshot("paired_devices") == {"pairedDevices": []}


def test_types_filter(store):
    client = FakeClient()
    syncer(store, client, registry=(STEPS, WEIGHT)).run(types=["weight"])
    assert {w[0] for w in client.windows} == {"weight"}


def test_network_error_marks_type_failed_and_run_continues(store):
    client = FakeClient({"steps": steps_history(5)}, errors={"weight": ApiError(0, "network unavailable")})
    result = syncer(store, client, registry=(WEIGHT, STEPS)).run()
    assert [(r.name, r.status) for r in result.results] == [("weight", "failed"), ("steps", "ok")]
    assert store.get_state("weight").status == "failed"
    assert "network unavailable" in store.get_state("weight").last_error
    assert store.last_run()["types_failed"] == 1


def test_rebuild_failure_marks_type_failed_but_does_not_pin_progress(store, monkeypatch):
    real = normalize.rebuild

    def rebuild(st, dt, dates):
        if dt.name == "weight":
            raise NormalizeError("weight: all 1 points failed to normalize")
        return real(st, dt, dates)

    monkeypatch.setattr(normalize, "rebuild", rebuild)
    client = FakeClient({"weight": [samples.weight("2026-10-10T08:00:00Z")], "steps": steps_history(5)})
    result = syncer(store, client, registry=(WEIGHT, STEPS)).run()
    assert [(r.name, r.status) for r in result.results] == [("weight", "failed"), ("steps", "ok")]
    assert "failed to normalize" in result.results[0].error
    st = store.get_state("weight")
    assert st.status == "failed" and "failed to normalize" in st.last_error
    assert st.backfill_done and st.watermark_utc == NOW  # raw rows are safe, so progress is kept
    assert store.raw_count("weight") == 1
    assert store.last_run()["types_failed"] == 1
    assert store.raw_count("steps") == 5


def test_real_unnormalizable_points_fail_the_type_but_keep_raw(store):
    # No endTime: parses into a raw row, but the steps normalizer needs one, so every point fails.
    bad = samples.steps("2026-10-13T10:00:00Z", "2026-10-13T10:01:00Z")
    del bad["steps"]["interval"]["endTime"]
    result = syncer(store, FakeClient({"steps": [bad]})).run()
    assert result.results[0].status == "failed"
    assert store.raw_count("steps") == 1
    assert store.get_state("steps").backfill_done
    assert store.last_run()["types_failed"] == 1


class Renamed(FakeClient):
    """Every window returns points whose payload key the parser doesn't know."""

    def iter_points(self, dt, start, end):
        self.windows.append((dt.name, start, end))
        yield [{"stepz": {"x": i}} for i in range(3)]


def test_unparseable_window_is_not_mistaken_for_empty_history(store):
    client = Renamed()
    result = syncer(store, client).run()
    assert [(r.name, r.status) for r in result.results] == [("steps", "failed")]
    assert "none of 3 points could be parsed" in result.results[0].error
    assert len(client.windows) == 1  # stopped at the first window instead of walking on
    st = store.get_state("steps")
    assert st.status == "failed" and not st.backfill_done
    assert st.watermark_utc is None and st.backfill_cursor_utc is None and st.earliest_data_utc is None
    assert store.last_run()["types_failed"] == 1


def test_unparseable_window_does_not_advance_existing_watermark(store):
    syncer(store, FakeClient({"steps": steps_history(5)})).run()
    later = NOW + timedelta(days=1)
    result = syncer(store, Renamed(), now=later).run()
    assert result.results[0].status == "failed"
    assert store.get_state("steps").watermark_utc == NOW


def test_earliest_data_is_earliest_parsed_point_not_window_start(store):
    syncer(store, FakeClient({"steps": steps_history(5)})).run()
    assert store.get_state("steps").earliest_data_utc == NOW - timedelta(days=4, hours=1)


def test_earliest_data_for_daily_points_is_midnight_utc(store):
    daily = DataType("daily-resting-heart-rate", Kind.DAILY, window_days=30)
    pts = [samples.daily_rhr("2026-10-05"), samples.daily_rhr("2026-10-09")]

    class Daily(FakeClient):
        def iter_points(self, dt, start, end):
            self.windows.append((dt.name, start, end))
            yield pts if len(self.windows) == 1 else []

    syncer(store, Daily(), registry=(daily,)).run()
    assert store.get_state("daily-resting-heart-rate").earliest_data_utc == datetime(2026, 10, 5, tzinfo=UTC)


def test_watermark_advances_after_each_forward_window(store):
    syncer(store, FakeClient({"steps": steps_history(20)})).run()
    later = NOW + timedelta(days=10)
    # Deep rescan from NOW-14d: windows end at NOW-7d, NOW, NOW+7d, NOW+10d; the 4th fails.
    client = FakeClient({"steps": steps_history(20)}, fail_on_call=4, fail_with=ApiError(503, "busy"))
    result = syncer(store, client, now=later).run()
    assert result.results[0].status == "failed"
    assert store.get_state("steps").watermark_utc == NOW + timedelta(days=7)  # end of the last good window


def test_bad_request_is_a_failure_not_unavailable(store):
    client = FakeClient(errors={"steps": ApiError(400, "bad filter")})
    result = syncer(store, client).run()
    assert result.results[0].status == "failed"
    assert store.last_run()["types_failed"] == 1


def test_api_errors_name_the_window(store):
    client = FakeClient(errors={"weight": ApiError(403, "scope not granted")})
    syncer(store, client, registry=(WEIGHT,)).run()
    err = store.get_state("weight").last_error
    assert "403" in err and "scope not granted" in err and "[window 2026-07-16T12:00:00Z..2026-10-14T12:00:00Z]" in err


def test_unparseable_points_are_dropped_with_one_warning_per_page(store, caplog):
    good = samples.steps("2026-10-13T10:00:00Z", "2026-10-13T10:01:00Z", 7)
    junk = [{"steps": {"interval": {}}}, {"steps": "nope"}, {"other": 1}]

    class Junky(FakeClient):
        def iter_points(self, dt, start, end):
            self.windows.append((dt.name, start, end))
            yield junk + [good] if len(self.windows) == 1 else []

    with caplog.at_level(logging.WARNING, logger="fitbit_air.sync"):
        result = syncer(store, Junky()).run()
    drops = [r for r in caplog.records if "dropping" in r.getMessage()]
    assert len(drops) == 1 and "3" in drops[0].getMessage()
    assert [(r.status, r.points) for r in result.results] == [("ok", 1)]
    assert store.raw_count("steps") == 1


def test_unexpected_error_is_isolated_per_type_and_run_is_recorded(store, monkeypatch):
    real = normalize.rebuild

    def rebuild(st, dt, dates):
        if dt.name == "weight":
            raise RuntimeError("boom")
        return real(st, dt, dates)

    monkeypatch.setattr(normalize, "rebuild", rebuild)
    client = FakeClient({"weight": [samples.weight("2026-10-10T08:00:00Z")], "steps": steps_history(5)})
    result = syncer(store, client, registry=(WEIGHT, STEPS)).run()
    assert [(r.name, r.status) for r in result.results] == [("weight", "failed"), ("steps", "ok")]
    assert "RuntimeError('boom')" in store.get_state("weight").last_error
    assert store.get_state("weight").status == "failed"
    run = store.last_run()
    assert run["finished_at"] is not None and run["types_failed"] == 1 and run["types_ok"] == 1


def test_run_is_finished_even_if_something_escapes(store):
    class BadSnapshots(FakeClient):
        def get_profile(self):
            raise RuntimeError("snapshot boom")

    with pytest.raises(RuntimeError, match="snapshot boom"):
        syncer(store, BadSnapshots({"steps": steps_history(3)})).run()
    run = store.last_run()
    assert run["finished_at"] is not None and run["types_ok"] == 1


def _failing_rebuild(monkeypatch, name):
    real = normalize.rebuild

    def rebuild(st, dt, dates):
        if dt.name == name:
            raise NormalizeError(f"{name}: all points failed to normalize")
        return real(st, dt, dates)

    monkeypatch.setattr(normalize, "rebuild", rebuild)
    return real


def test_failed_rebuild_stays_failed_until_repaired(store, monkeypatch):
    points = {"steps": steps_history(5)}
    real = _failing_rebuild(monkeypatch, "steps")
    assert syncer(store, FakeClient(points)).run().results[0].status == "failed"
    assert len(store.pending_dates("steps")) == 5 and "failed to normalize" in store.pending_error("steps")
    assert store.table_counts()["activity_intervals"] == 0

    # Same data again (raw rows unchanged, so nothing new to rebuild) and rebuild still broken: still failed.
    later = NOW + timedelta(hours=2)
    result = syncer(store, FakeClient(points), now=later).run()
    assert result.results[0].status == "failed" and "failed to normalize" in result.results[0].error
    assert store.get_state("steps").status == "failed"
    assert len(store.pending_dates("steps")) == 5

    monkeypatch.setattr(normalize, "rebuild", real)
    result = syncer(store, FakeClient(points), now=later + timedelta(hours=2)).run()
    assert result.results[0].status == "ok"
    assert store.pending_dates("steps") == [] and store.pending_error("steps") is None
    assert store.table_counts()["activity_intervals"] == 5
    assert store.get_state("steps").last_error is None


def test_pending_store_api(store):
    store.add_pending("steps", {"2026-10-02", "2026-10-01"}, "e1")
    store.add_pending("steps", ["2026-10-01"], "e2")  # same key: error is refreshed, no duplicate
    store.add_pending("weight", ["2026-10-03"], "w")
    assert store.pending_dates("steps") == ["2026-10-01", "2026-10-02"]
    assert store.pending_error("steps") in ("e1", "e2") and store.pending_error("nothing") is None
    store.clear_pending("steps", ["2026-10-01"])
    assert store.pending_dates("steps") == ["2026-10-02"] and store.pending_dates("weight") == ["2026-10-03"]
    store.clear_pending("steps", ["2026-10-02"])
    assert store.pending_dates("steps") == []


class PagedClient(FakeClient):
    """Each point is its own page. Logs (window days, points in window, pages actually consumed) per window."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.log = []

    def iter_points(self, dt, start, end):
        self.windows.append((dt.name, start, end))
        pts = [p for p in self.points.get(dt.name, []) if start <= _start(dt, p) < end]
        entry = {"days": (end - start).days, "points": len(pts), "pages": 0}
        self.log.append(entry)
        for p in pts:
            entry["pages"] += 1
            yield [p]


def test_galloped_windows_are_probed_with_one_page_then_walked_in_base_windows(store):
    recent = steps_history(3)
    old = []
    for i in range(30):  # dense data starting 110 days back
        start = NOW - timedelta(days=110 + i, hours=1)
        old.append(samples.steps(iso_utc(start), iso_utc(start + timedelta(minutes=1)), 50))
    client = PagedClient({"steps": recent + old})
    syncer(store, client).run()
    assert store.raw_count("steps") == 33  # nothing is lost
    base = STEPS.window_days
    oversized_hits = [e for e in client.log if e["days"] > base and e["points"]]
    assert oversized_hits and all(e["pages"] == 1 for e in oversized_hits)
    assert all(e["pages"] == e["points"] for e in client.log if e["days"] <= base)
    assert any(e["days"] == base and e["points"] > 1 for e in client.log)


def test_watermark_follows_the_clock_after_a_run_with_a_wrong_future_clock(store):
    syncer(store, FakeClient({"steps": steps_history(20)})).run()
    syncer(store, FakeClient({"steps": steps_history(20)}), now=NOW + timedelta(days=60)).run()
    client = FakeClient({"steps": steps_history(20)})
    later = NOW + timedelta(days=1)
    syncer(store, client, now=later).run()
    assert client.windows  # not skipped because the watermark was left in the future
    assert client.windows[-1][2] == later
    assert store.get_state("steps").watermark_utc == later


def test_probe_does_not_stop_on_an_empty_first_page(store):
    recent = steps_history(3)
    old = []
    for i in range(30):
        start = NOW - timedelta(days=110 + i, hours=1)
        old.append(samples.steps(iso_utc(start), iso_utc(start + timedelta(minutes=1)), 50))

    class EmptyFirstPage(FakeClient):
        """Like Google can: an empty page (with a next-page token) before the page that has the points."""

        def iter_points(self, dt, start, end):
            pts = next(super().iter_points(dt, start, end))
            yield []
            if pts:
                yield pts

    result = syncer(store, EmptyFirstPage({"steps": recent + old})).run()
    assert store.raw_count("steps") == 33
    assert result.results[0].status == "ok" and store.get_state("steps").backfill_done


def test_pending_retry_clears_per_batch_and_stops_at_first_failure(store, monkeypatch):
    dates = [f"2026-10-{d:02d}" for d in range(1, 11)]
    store.add_pending("steps", dates, "old error")
    monkeypatch.setattr(normalize, "REBUILD_BATCH_DAYS", 3)  # batches: 01-03, 04-06, 07-09, 10
    real = normalize.rebuild
    calls = []

    def rebuild(st, dt, ds):
        calls.append(list(ds))
        if "2026-10-08" in ds:
            raise NormalizeError("steps: still broken")
        return real(st, dt, ds)

    monkeypatch.setattr(normalize, "rebuild", rebuild)
    result = syncer(store, FakeClient(), registry=(STEPS,)).run(types=["steps"])
    assert calls[:3] == [dates[0:3], dates[3:6], dates[6:9]]  # stopped at the failing batch
    assert store.pending_dates("steps") == dates[6:]
    assert store.pending_error("steps") == "steps: still broken"
    assert result.results[0].status == "failed" and "still broken" in result.results[0].error


def test_run_mode_is_backfill_only_while_history_is_unfinished(store):
    client = FakeClient({"steps": steps_history(2)})
    syncer(store, client).run()
    assert store.last_run()["mode"] == "backfill"  # first run: no sync state yet
    syncer(store, client).run()
    assert store.last_run()["mode"] == "incremental"  # backfill finished
    syncer(store, client).run(full_backfill=True)
    assert store.last_run()["mode"] == "backfill"


ZONES = DataType("daily-heart-rate-zones", Kind.DAILY, window_days=30, backfill=False)


def test_settings_type_fetches_only_recent_days_and_never_backfills(store):
    client = FakeClient()
    syncer(store, client, registry=(ZONES,)).run()
    assert client.windows == [("daily-heart-rate-zones", NOW - timedelta(days=14), NOW)]
    assert store.get_state("daily-heart-rate-zones").backfill_done
    client.windows.clear()
    syncer(store, client, registry=(ZONES,)).run(full_backfill=True)
    assert all(w[1] >= NOW - timedelta(days=14) for w in client.windows)
