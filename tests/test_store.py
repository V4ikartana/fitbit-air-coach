import sqlite3
from datetime import datetime

import pytest

from fitbit_air.store import SCHEMA_VERSION, CLEAN_TABLES, RawRow, Store, SyncState
from fitbit_air.timeutil import UTC


def raw(key="k1", date="2026-10-13", payload=None, data_type="steps"):
    return RawRow(data_type, key, "2026-10-12T20:00:00Z", None, date, "34200s", "reconciled", payload or {"v": 1})


def test_fresh_store_has_schema(store):
    assert store.conn.execute("SELECT version FROM schema_version").fetchone()[0] == SCHEMA_VERSION
    counts = store.table_counts()
    assert set(counts) == {"raw_points", *CLEAN_TABLES}
    assert all(v == 0 for v in counts.values())


def test_reopen_keeps_single_schema_version(tmp_path):
    Store(tmp_path / "h.db").close()
    s = Store(tmp_path / "h.db")
    assert s.conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0] == 1
    s.close()


def test_upsert_raw_returns_dates_and_overwrites(store):
    assert store.upsert_raw([raw(), raw("k2", "2026-10-14")]) == {"2026-10-13", "2026-10-14"}
    store.upsert_raw([raw(payload={"v": 2})])
    assert store.raw_count("steps") == 2
    assert list(store.raw_points_for("steps", ["2026-10-13"])) == [("k1", "2026-10-13", {"v": 2})]


def test_raw_dates_since(store):
    store.upsert_raw([raw(), raw("k2", "2026-10-14")])
    assert store.raw_dates("steps") == ["2026-10-13", "2026-10-14"]
    assert store.raw_dates("steps", since="2026-10-14") == ["2026-10-14"]


def test_replace_clean_is_scoped_to_type_and_dates(store):
    def row(d, v):
        return {"start_utc": "a", "end_utc": "b", "local_date": d, "metric": "steps", "value": v}

    store.replace_clean("steps", ["2026-10-13", "2026-10-14"], {"activity_intervals": [row("2026-10-13", 1), row("2026-10-14", 2)]})
    store.replace_clean("distance", ["2026-10-13"], {"activity_intervals": [row("2026-10-13", 9)]})
    store.replace_clean("steps", ["2026-10-13"], {"activity_intervals": [row("2026-10-13", 5)]})
    got = sorted((r["src_type"], r["local_date"], r["value"]) for r in store.conn.execute("SELECT * FROM activity_intervals"))
    assert got == [("distance", "2026-10-13", 9.0), ("steps", "2026-10-13", 5.0), ("steps", "2026-10-14", 2.0)]


def test_replace_clean_rejects_unknown_table(store):
    with pytest.raises(ValueError):
        store.replace_clean("x", ["2026-10-13"], {"nope": [{"local_date": "2026-10-13"}]})


def test_state_roundtrip(store):
    assert store.get_state("steps") is None
    st = SyncState("steps", watermark_utc=datetime(2026, 10, 14, 12, tzinfo=UTC), backfill_done=True, status="ok")
    store.save_state(st)
    got = store.get_state("steps")
    assert got.watermark_utc == st.watermark_utc
    assert got.backfill_done is True and got.status == "ok" and got.backfill_cursor_utc is None
    st.status = "failed"
    store.save_state(st)
    assert store.get_state("steps").status == "failed"
    assert len(store.all_states()) == 1


def test_runs(store):
    assert store.last_run() is None
    run_id = store.start_run("incremental")
    store.finish_run(run_id, 10, 3, 1, "x: boom")
    run = store.last_run()
    assert run["id"] == run_id and run["mode"] == "incremental"
    assert run["points_fetched"] == 10 and run["types_ok"] == 3 and run["types_failed"] == 1
    assert run["finished_at"] and run["error_summary"] == "x: boom"


def test_snapshots(store):
    store.save_snapshot("profile", {"age": 35})
    store.save_snapshot("profile", {"age": 36})
    assert store.get_snapshot("profile") == {"age": 36}
    assert store.get_snapshot("nope") is None


def test_views_exist(store):
    assert store.conn.execute("SELECT * FROM daily_wide").fetchall() == []
    assert store.conn.execute("SELECT * FROM activity_daily").fetchall() == []


def test_upsert_raw_returns_previous_date_when_point_moves(store):
    store.upsert_raw([raw("K", "2026-10-13")])
    assert store.upsert_raw([raw("K", "2026-10-14")]) == {"2026-10-13", "2026-10-14"}


def test_replace_clean_rejects_rows_outside_dates(store):
    def row(d):
        return {"start_utc": "a", "end_utc": "b", "local_date": d, "metric": "steps", "value": 1}

    store.replace_clean("steps", ["2026-10-13"], {"activity_intervals": [row("2026-10-13")]})
    with pytest.raises(ValueError, match=r"rows outside rebuild dates: \[.2026-10-14.\]"):
        store.replace_clean("steps", ["2026-10-13"], {"activity_intervals": [row("2026-10-14")]})
    # nothing was deleted by the rejected call
    assert store.table_counts()["activity_intervals"] == 1


def test_replace_clean_rejects_unknown_column(store):
    with pytest.raises(ValueError, match="bogus"):
        store.replace_clean("steps", ["2026-10-13"], {"samples": [{"local_date": "2026-10-13", "bogus": 1}]})


def test_replace_clean_rejects_src_type_key(store):
    row = {"ts_utc": "a", "local_date": "2026-10-13", "metric": "m", "value": 1, "src_type": "x"}
    with pytest.raises(ValueError, match="src_type"):
        store.replace_clean("steps", ["2026-10-13"], {"samples": [row]})


def test_replace_clean_with_empty_rows_still_clears(store):
    row = {"ts_utc": "a", "local_date": "2026-10-13", "metric": "m", "value": 1}
    store.replace_clean("steps", ["2026-10-13"], {"samples": [row]})
    assert store.table_counts()["samples"] == 1
    store.replace_clean("steps", ["2026-10-13"], {})
    assert store.table_counts()["samples"] == 0


def test_views_are_recreated_on_version_bump(tmp_path):
    from fitbit_air.store import SCHEMA_VERSION

    Store(tmp_path / "v.db").close()
    c = sqlite3.connect(tmp_path / "v.db")
    c.execute("DROP VIEW daily_wide")
    c.execute("CREATE VIEW daily_wide AS SELECT 1 AS stale")
    c.execute("UPDATE schema_version SET version = 0 WHERE id = 1")
    c.commit()
    c.close()
    s = Store(tmp_path / "v.db")
    cols = [r[1] for r in s.conn.execute("PRAGMA table_info(daily_wide)")]
    assert "resting_hr" in cols and "stale" not in cols
    assert s.conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()[0] == SCHEMA_VERSION
    s.close()


def test_open_does_not_need_write_lock_when_schema_current(tmp_path):
    Store(tmp_path / "w.db").close()
    blocker = sqlite3.connect(tmp_path / "w.db", timeout=0.2)
    try:
        blocker.execute("BEGIN IMMEDIATE")
        s = Store(tmp_path / "w.db")
        assert s.table_counts()["raw_points"] == 0
        s.close()
    finally:
        blocker.rollback()
        blocker.close()


def test_raw_points_for_orders_by_date_then_key(store):
    store.upsert_raw([raw("b", "2026-10-14"), raw("z", "2026-10-13"), raw("a", "2026-10-14")])
    keys = [k for k, _, _ in store.raw_points_for("steps", ["2026-10-13", "2026-10-14"])]
    assert keys == ["z", "a", "b"]


def test_upsert_raw_skips_unchanged_points(store):
    rows = [raw(), raw("k2", "2026-10-14")]
    assert store.upsert_raw(rows) == {"2026-10-13", "2026-10-14"}
    fetched = store.conn.execute("SELECT fetched_at FROM raw_points WHERE point_key = 'k1'").fetchone()[0]
    assert store.upsert_raw(rows) == set()  # identical payloads: nothing to rebuild
    assert store.conn.execute("SELECT fetched_at FROM raw_points WHERE point_key = 'k1'").fetchone()[0] == fetched
    assert store.upsert_raw([raw(payload={"v": 2}), raw("k2", "2026-10-14")]) == {"2026-10-13"}
    assert list(store.raw_points_for("steps", ["2026-10-13"])) == [("k1", "2026-10-13", {"v": 2})]


def test_upsert_raw_same_payload_on_new_date_is_not_skipped(store):
    store.upsert_raw([raw("K", "2026-10-13")])
    assert store.upsert_raw([raw("K", "2026-10-14")]) == {"2026-10-13", "2026-10-14"}


def test_sync_state_roundtrips_deep_scan(store):
    when = datetime(2026, 10, 14, 12, tzinfo=UTC)
    store.save_state(SyncState("steps", deep_scan_utc=when))
    assert store.get_state("steps").deep_scan_utc == when
    store.save_state(SyncState("weight"))
    assert store.get_state("weight").deep_scan_utc is None


def test_store_files_are_private(tmp_path):
    import os
    import stat

    d = tmp_path / "data"
    d.mkdir(mode=0o755)
    from fitbit_air.store import Store as _Store

    _Store(d / "health.db").close()
    assert stat.S_IMODE(os.stat(d).st_mode) == 0o700
    assert stat.S_IMODE(os.stat(d / "health.db").st_mode) == 0o600


def test_daily_wide_lists_only_dates_with_headline_metrics(store):
    zone = {"local_date": "2015-06-01", "metric": "hr_zone_light_lo_bpm", "value": 30.0}
    rhr = {"local_date": "2026-10-14", "metric": "resting_hr", "value": 58.0}
    store.replace_clean("daily-heart-rate-zones", ["2015-06-01"], {"daily_metrics": [zone]})
    store.replace_clean("daily-resting-heart-rate", ["2026-10-14"], {"daily_metrics": [rhr]})
    assert [r["local_date"] for r in store.conn.execute("SELECT local_date FROM daily_wide")] == ["2026-10-14"]
