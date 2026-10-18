import sqlite3

import pytest

from coach_seed import seed_analysis, seed_sleep, seed_sync
from fitbit_air.store import SCHEMA_VERSION, Store


def test_schema_v5_tables_exist(store):
    names = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"journal", "coach_notes", "meta"} <= names
    assert SCHEMA_VERSION == 5


def test_v4_database_upgrades_and_keeps_data(tmp_path):
    path = tmp_path / "health.db"
    s = Store(path)
    s.save_snapshot("profile", {"age": 26})
    s.conn.execute("DROP TABLE journal")
    s.conn.execute("DROP TABLE coach_notes")
    s.conn.execute("DROP TABLE meta")
    s.conn.execute("UPDATE schema_version SET version = 4")
    s.conn.commit()
    s.close()
    s = Store(path)
    assert s.get_snapshot("profile") == {"age": 26}
    assert s.conn.execute("SELECT version FROM schema_version").fetchone()[0] == 5
    assert s.add_journal("2026-10-17", {"feeling": 4})["feeling"] == 4
    s.close()


def test_meta_round_trip_and_overwrite(store):
    assert store.get_meta("auth_completed_at") is None
    store.set_meta("auth_completed_at", "2026-10-17T04:00:00Z")
    store.set_meta("auth_completed_at", "2026-10-18T04:00:00Z")
    assert store.get_meta("auth_completed_at") == "2026-10-18T04:00:00Z"


def test_journal_add_and_range(store):
    a = store.add_journal("2026-10-16", {"feeling": 3, "illness": 1, "notes": "sore throat"})
    store.add_journal("2026-10-17", {"alcohol_units": 2.5})
    store.add_journal("2026-10-17", {"energy": 4})
    assert a["id"] and a["local_date"] == "2026-10-16" and a["illness"] == 1 and a["created_at"]
    rows = store.journal_between("2026-10-17", "2026-10-17")
    assert [r["alcohol_units"] for r in rows] == [2.5, None] and rows[1]["energy"] == 4
    with pytest.raises(ValueError):
        store.add_journal("2026-10-17", {"mood": 5})


def test_notes_add_list_order_and_update(store):
    later = store.add_note("target", "3 runs this week", "2026-10-24")
    soon = store.add_note("plan", "easy 3 km Saturday", "2026-10-18")
    loose = store.add_note("observation", "sleeps better without late screens", None)
    assert [n["id"] for n in store.notes()] == [soon["id"], later["id"], loose["id"]]  # due first, undated last
    assert [n["id"] for n in store.notes(due_by="2026-10-20")] == [soon["id"]]
    done = store.update_note(soon["id"], {"status": "done"})
    assert done["status"] == "done" and done["updated_at"]
    assert soon["id"] not in [n["id"] for n in store.notes()]
    assert [n["id"] for n in store.notes(status=None)] == [soon["id"], later["id"], loose["id"]]
    assert store.update_note(9999, {"status": "done"}) is None
    with pytest.raises(ValueError):
        store.update_note(later["id"], {"kind": "plan"})
    assert store.get_note(later["id"])["text"] == "3 runs this week"


def test_journal_table_rejects_missing_date(store):
    with pytest.raises(sqlite3.IntegrityError):
        store.conn.execute("INSERT INTO journal (created_at) VALUES ('x')")


def test_last_finished_run_skips_unfinished(store):
    assert store.last_finished_run() is None
    seed_sync(store, "2026-10-17T02:00:00Z")
    seed_sync(store, None)
    assert store.last_finished_run()["finished_at"] == "2026-10-17T02:00:00Z"


def test_analysis_names_ranges_and_latest(store):
    ds = seed_analysis(store)
    assert store.first_analysis_date() == ds[0]
    assert {"rhr", "steps_7d"} <= set(store.analysis_metric_names())
    assert {"recovery", "sleep", "body_age", "race"} <= set(store.score_names())
    rows = store.metric_rows_between(["rhr"], ds[-3], ds[-1])
    assert [r["local_date"] for r in rows] == ds[-3:] and rows[0]["baseline"] == 60.0
    sc = store.scores_between(["recovery"], ds[-2], ds[-1])
    assert [s["local_date"] for s in sc] == ds[-2:] and sc[0]["components"] == {"hrv": {"score": 60}}
    assert sc[0]["evidence_ids"] == ["plews2013_hrv"]
    assert store.latest_score("body_age", "2026-12-01")["local_date"] == ds[-1]
    assert store.latest_score("body_age", ds[0]) is None


def test_sleep_sessions_between_includes_naps(store):
    seed_sleep(store, "2026-10-16", "2026-10-15T17:30:00Z", "2026-10-16T01:00:00Z", 400)
    seed_sleep(store, "2026-10-16", "2026-10-16T09:00:00Z", "2026-10-16T09:40:00Z", 35, main=0, nap=1)
    rows = store.sleep_sessions_between("2026-10-16", "2026-10-16")
    assert [r["is_nap"] for r in rows] == [0, 1]


def test_schema_description_lists_tables_and_columns(store):
    desc = {t["name"]: t for t in store.schema_description()}
    assert desc["journal"]["type"] == "table"
    assert {"name": "local_date", "type": "TEXT"} in desc["journal"]["columns"]
    assert "daily_wide" in desc and desc["daily_wide"]["type"] == "view"
    assert not any(n.startswith("sqlite_") for n in desc)


def test_heart_rate_days_counts_distinct_dates(store):
    assert store.heart_rate_days("2026-10-01", "2026-10-17") == 0
    hr = [{"ts_utc": "2026-10-16T10:00:00Z", "local_date": "2026-10-16", "bpm": 60},
          {"ts_utc": "2026-10-16T10:01:00Z", "local_date": "2026-10-16", "bpm": 61}]
    store.replace_clean("hr-16", ["2026-10-16"], {"heart_rate": hr})
    store.replace_clean("hr-17", ["2026-10-17"], {"heart_rate": [
        {"ts_utc": "2026-10-17T10:00:00Z", "local_date": "2026-10-17", "bpm": 62}]})
    assert store.heart_rate_days("2026-10-01", "2026-10-17") == 2
    assert store.heart_rate_days("2026-10-17", "2026-10-17") == 1


def test_count_rows_only_known_tables(store):
    seed_analysis(store)
    assert store.count_rows("insights") == 2
    with pytest.raises(ValueError):
        store.count_rows("sqlite_master; DROP TABLE runs")


def test_first_and_last_analysis_date_span_scores_and_metrics(store):
    assert store.first_analysis_date() is None and store.last_analysis_date() is None
    seed_analysis(store)
    assert (store.first_analysis_date(), store.last_analysis_date()) == ("2026-10-08", "2026-10-17")
    store.conn.execute("DELETE FROM scores_daily")
    assert (store.first_analysis_date(), store.last_analysis_date()) == ("2026-10-08", "2026-10-17")
