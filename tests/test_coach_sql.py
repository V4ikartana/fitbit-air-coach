import hashlib
import sqlite3

import pytest

from coach_seed import seed_analysis
from fitbit_air.coach.sql import MAX_CELL_CHARS, MAX_ROWS, MAX_VALUE_BYTES, SqlError, run_query
from fitbit_air.store import Store


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "health.db"
    s = Store(path)
    seed_analysis(s)
    s.close()
    return path


def _digest(path) -> str:
    """Hash the database file and, when present, its write-ahead log (a write could sit there uncheckpointed)."""
    h = hashlib.sha256(path.read_bytes())
    wal = path.with_name(path.name + "-wal")
    if wal.exists():
        h.update(wal.read_bytes())
    return h.hexdigest()


def test_select_join_and_cte(db):
    out = run_query(db, "WITH r AS (SELECT local_date, value FROM scores_daily WHERE score = 'recovery') "
                        "SELECT r.local_date, r.value, m.value AS rhr FROM r JOIN metric_daily m "
                        "ON m.local_date = r.local_date AND m.metric = 'rhr' ORDER BY r.local_date DESC LIMIT 2;")
    assert out["columns"] == ["local_date", "value", "rhr"] and len(out["rows"]) == 2 and out["truncated"] is False


@pytest.mark.parametrize("sql", [
    "INSERT INTO journal (local_date, created_at) VALUES ('x', 'y')",
    "UPDATE scores_daily SET value = 0",
    "DELETE FROM insights",
    "DROP TABLE runs",
    "ATTACH DATABASE ':memory:' AS other",
    "PRAGMA journal_mode = DELETE",
    "SELECT 1; SELECT 2",
    "CREATE TABLE x (a)",
    # beyond the plan: other ways to write or escape
    "ATTACH DATABASE 'EVIL_PATH' AS other",
    "VACUUM INTO 'EVIL_PATH'",
    "VACUUM",
    "REINDEX",
    "ANALYZE",
    "BEGIN",
    "SAVEPOINT s",
    "ALTER TABLE runs RENAME TO runs2",
    "CREATE TRIGGER t AFTER INSERT ON runs BEGIN SELECT 1; END",
    "CREATE VIEW v AS SELECT 1",
    "REPLACE INTO insights DEFAULT VALUES",
    "WITH x AS (SELECT 1) DELETE FROM insights",
    "WITH x AS (SELECT 1) INSERT INTO journal (local_date, created_at) SELECT 'x', 'y'",
    "PRAGMA writable_schema = ON",
    "PRAGMA user_version = 99",
    "PRAGMA query_only = OFF",
    "PRAGMA foreign_keys = OFF",
    "PRAGMA wal_checkpoint(TRUNCATE)",
    "PRAGMA optimize",
    "PRAGMA table_info(journal); DROP TABLE runs",
    "SELECT 1; DROP TABLE runs",
    "SELECT 1; -- harmless\nDROP TABLE runs",
    "SELECT 1 /* c */; INSERT INTO journal (local_date, created_at) VALUES ('x', 'y')",
    "SELECT load_extension('/usr/lib/evil')",
    "/* hidden */ DELETE FROM insights",
    "-- note\nDELETE FROM insights",
    "   ;DELETE FROM insights",
    "SELECT 'a;b'; DROP TABLE runs",
    "SELECT 1; -- done\nDROP TABLE runs",
])
def test_writes_and_multiple_statements_refused(db, tmp_path, sql):
    sql = sql.replace("EVIL_PATH", str(tmp_path / "evil.db"))
    before = _digest(db)
    with pytest.raises(SqlError):
        run_query(db, sql)
    assert not (tmp_path / "evil.db").exists()
    assert _digest(db) == before  # byte-identical
    s = Store(db)
    assert s.conn.execute("SELECT COUNT(*) FROM insights").fetchone()[0] == 2  # nothing changed
    assert s.conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] >= 0  # runs table still exists
    s.close()


def test_trailing_comment_after_semicolon_is_fine(db):
    assert run_query(db, "SELECT 1; -- done")["rows"] == [[1]]
    assert run_query(db, "SELECT 1 /* x */ ;  ")["rows"] == [[1]]


def test_empty_query_is_refused(db):
    for sql in ("", "   ", ";", " ; "):
        with pytest.raises(SqlError, match="empty"):
            run_query(db, sql)


def test_second_statement_message(db):
    with pytest.raises(SqlError, match="only one statement"):
        run_query(db, "SELECT 1; SELECT 2")


def test_oversized_blob_is_refused(db):
    with pytest.raises(SqlError):
        run_query(db, f"SELECT zeroblob({MAX_VALUE_BYTES + 1})")
    assert run_query(db, f"SELECT length(zeroblob({MAX_VALUE_BYTES // 2})) AS n")["rows"] == [[MAX_VALUE_BYTES // 2]]


def test_value_limit_is_small_enough_for_the_tool():
    assert MAX_VALUE_BYTES == 100_000


def test_long_strings_are_truncated(db):
    out = run_query(db, "SELECT printf('%.*c', 5000, 'x') AS s, 'short' AS t")
    s, t = out["rows"][0]
    assert s == "x" * MAX_CELL_CHARS + "…<truncated, 5000 chars>" and t == "short"


def test_non_finite_floats_become_null(db):
    out = run_query(db, "SELECT 1e999 AS a, -1e999 AS b, 1.5 AS c")
    assert out["rows"][0] == [None, None, 1.5]


def test_non_time_errors_are_not_labelled_time_limit(db):
    with pytest.raises(SqlError, match="query refused or failed") as exc:
        run_query(db, "SELECT * FROM no_such_table")
    assert "time" not in str(exc.value)


def test_semicolon_inside_string_literal_is_one_statement(db):
    out = run_query(db, "SELECT 'a;b' AS s;")
    assert out["rows"] == [["a;b"]]


def test_pragma_table_info_allowed(db):
    out = run_query(db, "PRAGMA table_info(journal)")
    assert "name" in out["columns"]


def test_row_cap_and_truncation(db):
    out = run_query(db, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 600) SELECT i FROM n")
    assert len(out["rows"]) == MAX_ROWS and out["truncated"] is True


def test_runaway_query_is_stopped(db):
    with pytest.raises(SqlError, match="time"):
        run_query(db, "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n) SELECT COUNT(*) FROM n",
                  time_limit_s=0.5)


def test_values_are_json_friendly(db):
    out = run_query(db, "SELECT x'0102' AS b, 1.0 / 3 AS f, NULL AS n")
    assert out["rows"][0] == ["<blob 2 bytes>", 0.3333, None]


def test_missing_database_is_an_error(tmp_path):
    with pytest.raises(SqlError, match="not found"):
        run_query(tmp_path / "nope.db", "SELECT 1")
    assert not (tmp_path / "nope.db").exists()  # read-only open must not create it


def test_database_path_with_uri_characters(tmp_path):
    odd = tmp_path / "we?ird #dir"
    odd.mkdir()
    path = odd / "health.db"
    s = Store(path)
    seed_analysis(s)
    s.close()
    assert run_query(path, "SELECT COUNT(*) FROM insights")["rows"] == [[2]]


def test_connection_really_is_read_only_even_without_the_authorizer(db):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("DELETE FROM insights")
    conn.close()
