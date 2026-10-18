"""SQLite storage. This is the only module that issues SQL, so a cloud backend can replace it later."""
from __future__ import annotations

import json
import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, Iterator

from .timeutil import UTC, iso_utc, parse_ts

SCHEMA_VERSION = 5
CLEAN_TABLES = (
    "heart_rate",
    "samples",
    "daily_metrics",
    "sleep_sessions",
    "sleep_stages",
    "activity_intervals",
    "exercise_sessions",
    "alerts",
)
# Bump SCHEMA_VERSION when changing this so existing DBs rebuild their views.
DAILY_WIDE_METRICS = (
    "resting_hr",
    "hrv_rmssd_ms",
    "hrv_deep_rmssd_ms",
    "non_rem_hr",
    "spo2_avg_pct",
    "resp_rate",
    "sleep_temp_dev_c",
    "vo2_max",
)

_RUN_COLS = ("session_id", "local_date", "distance_km", "duration_min", "pace_min_km", "avg_hr",
             "z0_min", "z1_min", "z2_min", "z3_min", "z4_min", "z5_min", "decoupling_pct", "decoupling_approx",
             "economy_m_per_beat", "spike_pct", "spike_class")
_JOURNAL_COLS = ("feeling", "energy", "soreness", "session_rpe", "illness", "alcohol_units", "notes")
_NOTE_UPDATABLE = ("status", "text", "review_on")
_COUNTABLE = ("metric_daily", "scores_daily", "insights", "runs", "journal", "coach_notes", "analysis_runs")

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_version (id INTEGER PRIMARY KEY CHECK (id = 1), version INTEGER NOT NULL);

CREATE TABLE IF NOT EXISTS raw_points (
    data_type  TEXT NOT NULL,
    point_key  TEXT NOT NULL,
    start_utc  TEXT,
    end_utc    TEXT,
    local_date TEXT NOT NULL,
    utc_offset TEXT,
    source     TEXT NOT NULL,
    payload    TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (data_type, point_key)
);
CREATE INDEX IF NOT EXISTS raw_points_type_date ON raw_points (data_type, local_date);

CREATE TABLE IF NOT EXISTS heart_rate (
    ts_utc TEXT NOT NULL, local_ts TEXT, local_date TEXT NOT NULL,
    bpm INTEGER NOT NULL, motion TEXT, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS samples (
    ts_utc TEXT NOT NULL, local_date TEXT NOT NULL, metric TEXT NOT NULL,
    value REAL, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_metrics (
    local_date TEXT NOT NULL, metric TEXT NOT NULL, value REAL, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sleep_sessions (
    id TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, local_date TEXT NOT NULL,
    is_main INTEGER, is_nap INTEGER, sleep_type TEXT,
    min_asleep REAL, min_awake REAL, min_to_fall_asleep REAL, min_after_wake REAL,
    min_in_period REAL, efficiency REAL, min_light REAL, min_deep REAL, min_rem REAL,
    update_time TEXT, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sleep_stages (
    session_id TEXT NOT NULL, stage TEXT NOT NULL, start_utc TEXT NOT NULL, end_utc TEXT NOT NULL,
    local_date TEXT NOT NULL, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS activity_intervals (
    start_utc TEXT NOT NULL, end_utc TEXT NOT NULL, local_date TEXT NOT NULL,
    metric TEXT NOT NULL, value REAL, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS exercise_sessions (
    id TEXT NOT NULL, exercise_type TEXT, display_name TEXT, start_utc TEXT NOT NULL, end_utc TEXT,
    local_date TEXT NOT NULL, duration_min REAL, active_min REAL, avg_hr REAL, calories_kcal REAL,
    steps REAL, distance_m REAL, azm REAL, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS alerts (
    start_utc TEXT NOT NULL, end_utc TEXT, local_date TEXT NOT NULL, kind TEXT NOT NULL,
    payload TEXT NOT NULL, src_type TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS snapshots (
    kind TEXT PRIMARY KEY, fetched_at TEXT NOT NULL, payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sync_state (
    data_type TEXT PRIMARY KEY, watermark_utc TEXT, backfill_cursor_utc TEXT,
    backfill_done INTEGER NOT NULL DEFAULT 0, earliest_data_utc TEXT,
    status TEXT, last_error TEXT, deep_scan_utc TEXT, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pending_rebuild (
    data_type TEXT NOT NULL, local_date TEXT NOT NULL, error TEXT, PRIMARY KEY (data_type, local_date)
);
CREATE TABLE IF NOT EXISTS sync_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, mode TEXT NOT NULL,
    points_fetched INTEGER, types_ok INTEGER, types_failed INTEGER, error_summary TEXT
);
CREATE TABLE IF NOT EXISTS metric_daily (
    local_date TEXT NOT NULL, metric TEXT NOT NULL, value REAL, baseline REAL, scale REAL, z REAL,
    band_status TEXT, confidence REAL, quality TEXT, n_baseline INTEGER, PRIMARY KEY (local_date, metric)
);
CREATE TABLE IF NOT EXISTS scores_daily (
    local_date TEXT NOT NULL, score TEXT NOT NULL, value REAL, band TEXT, confidence REAL, status TEXT NOT NULL,
    components_json TEXT NOT NULL, evidence_ids TEXT NOT NULL, PRIMARY KEY (local_date, score)
);
CREATE TABLE IF NOT EXISTS insights (
    id INTEGER PRIMARY KEY AUTOINCREMENT, local_date TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
    title TEXT NOT NULL, detail TEXT NOT NULL, evidence_ids TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS insights_date ON insights (local_date);
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL, finished_at TEXT, since TEXT NOT NULL,
    error TEXT, summary TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    session_id TEXT PRIMARY KEY NOT NULL, local_date TEXT NOT NULL, distance_km REAL, duration_min REAL, pace_min_km REAL,
    avg_hr REAL, z0_min REAL, z1_min REAL, z2_min REAL, z3_min REAL, z4_min REAL, z5_min REAL, decoupling_pct REAL,
    decoupling_approx INTEGER, economy_m_per_beat REAL, spike_pct REAL, spike_class TEXT
);
CREATE INDEX IF NOT EXISTS runs_date ON runs (local_date);
CREATE TABLE IF NOT EXISTS journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT, local_date TEXT NOT NULL, created_at TEXT NOT NULL, feeling INTEGER,
    energy INTEGER, soreness INTEGER, session_rpe INTEGER, illness INTEGER, alcohol_units REAL, notes TEXT
);
CREATE INDEX IF NOT EXISTS journal_date ON journal (local_date);
CREATE TABLE IF NOT EXISTS coach_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
    review_on TEXT, status TEXT NOT NULL DEFAULT 'active', updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL);
"""

UPSERT_RAW = """
INSERT INTO raw_points (data_type, point_key, start_utc, end_utc, local_date, utc_offset, source, payload, fetched_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (data_type, point_key) DO UPDATE SET
    start_utc = excluded.start_utc, end_utc = excluded.end_utc, local_date = excluded.local_date,
    utc_offset = excluded.utc_offset, source = excluded.source, payload = excluded.payload,
    fetched_at = excluded.fetched_at
"""

SAVE_STATE = """
INSERT INTO sync_state (data_type, watermark_utc, backfill_cursor_utc, backfill_done, earliest_data_utc,
                        status, last_error, deep_scan_utc, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT (data_type) DO UPDATE SET
    watermark_utc = excluded.watermark_utc, backfill_cursor_utc = excluded.backfill_cursor_utc,
    backfill_done = excluded.backfill_done, earliest_data_utc = excluded.earliest_data_utc,
    status = excluded.status, last_error = excluded.last_error, deep_scan_utc = excluded.deep_scan_utc,
    updated_at = excluded.updated_at
"""


def _index_sql() -> str:
    lines = [f"CREATE INDEX IF NOT EXISTS {t}_src_date ON {t} (src_type, local_date);" for t in CLEAN_TABLES]
    lines.append("CREATE INDEX IF NOT EXISTS heart_rate_ts ON heart_rate (ts_utc);")
    lines.append("CREATE INDEX IF NOT EXISTS heart_rate_date ON heart_rate (local_date);")
    return "\n".join(lines)


def _view_statements() -> tuple[str, ...]:
    names = ", ".join(f"'{m}'" for m in DAILY_WIDE_METRICS)
    cols = ",\n    ".join(f"MAX(CASE WHEN metric = '{m}' THEN value END) AS {m}" for m in DAILY_WIDE_METRICS)
    return (
        "DROP VIEW IF EXISTS daily_wide",
        f"CREATE VIEW daily_wide AS SELECT local_date,\n    {cols}\nFROM daily_metrics WHERE metric IN ({names}) GROUP BY local_date",
        "DROP VIEW IF EXISTS activity_daily",
        "CREATE VIEW activity_daily AS "
        "SELECT local_date, metric, SUM(value) AS value FROM activity_intervals GROUP BY local_date, metric",
    )


@dataclass
class RawRow:
    data_type: str
    point_key: str
    start_utc: str | None
    end_utc: str | None
    local_date: str
    utc_offset: str | None
    source: str
    payload: dict


@dataclass
class SyncState:
    data_type: str
    watermark_utc: datetime | None = None
    backfill_cursor_utc: datetime | None = None
    backfill_done: bool = False
    earliest_data_utc: datetime | None = None
    status: str | None = None
    last_error: str | None = None
    deep_scan_utc: datetime | None = None  # when the last wide re-scan of recent history finished


def _chunks(items: list[str], size: int = 500) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _to_dt(value: str | None) -> datetime | None:
    return parse_ts(value) if value else None


def _to_s(value: datetime | None) -> str | None:
    return iso_utc(value) if value else None


def _score_row(r: sqlite3.Row) -> dict:
    row = dict(r)
    row["components"] = json.loads(row.pop("components_json"))
    row["evidence_ids"] = json.loads(row["evidence_ids"])
    return row


def _now() -> str:
    return iso_utc(datetime.now(UTC))


def _state_from_row(r: sqlite3.Row) -> SyncState:
    return SyncState(
        data_type=r["data_type"],
        watermark_utc=_to_dt(r["watermark_utc"]),
        backfill_cursor_utc=_to_dt(r["backfill_cursor_utc"]),
        backfill_done=bool(r["backfill_done"]),
        earliest_data_utc=_to_dt(r["earliest_data_utc"]),
        status=r["status"],
        last_error=r["last_error"],
        deep_scan_utc=_to_dt(r["deep_scan_utc"]),
    )


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass  # best effort


class Store:
    def __init__(self, path: Path | str):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        _chmod(path.parent, 0o700)  # health data is private: tighten pre-existing locations too
        self.conn = sqlite3.connect(path)
        _chmod(path, 0o600)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        # Tables and indexes only: no-ops (and no write lock) once they exist.
        self.conn.executescript(SCHEMA + _index_sql())
        row = self.conn.execute("SELECT version FROM schema_version WHERE id = 1").fetchone()
        if row is None or row[0] < SCHEMA_VERSION:
            # Views and the version row change together, in one write transaction.
            with self.conn:
                self.conn.execute("BEGIN IMMEDIATE")
                for stmt in _view_statements():
                    self.conn.execute(stmt)
                self.conn.execute(
                    "INSERT INTO schema_version (id, version) VALUES (1, ?) "
                    "ON CONFLICT (id) DO UPDATE SET version = excluded.version",
                    (SCHEMA_VERSION,),
                )
        self._columns = {
            t: frozenset(r["name"] for r in self.conn.execute(f"PRAGMA table_info({t})")) for t in CLEAN_TABLES
        }

    def close(self) -> None:
        self.conn.close()

    # ---- raw layer ---------------------------------------------------------

    def upsert_raw(self, rows: Iterable[RawRow]) -> set[str]:
        """Insert or replace raw points; returns the local dates touched, including the previous
        local date of any point that moved (its old date's clean rows must be rebuilt too).
        A point whose payload and local date are unchanged is skipped and contributes no date."""
        fetched = _now()
        dates: set[str] = set()
        with self.conn:
            for r in rows:
                payload = json.dumps(r.payload, separators=(",", ":"))
                prev = self.conn.execute(
                    "SELECT local_date, payload FROM raw_points WHERE data_type = ? AND point_key = ?",
                    (r.data_type, r.point_key),
                ).fetchone()
                if prev is not None:
                    if prev[1] == payload and prev[0] == r.local_date:
                        continue
                    dates.add(prev[0])
                self.conn.execute(
                    UPSERT_RAW,
                    (r.data_type, r.point_key, r.start_utc, r.end_utc, r.local_date, r.utc_offset, r.source,
                     payload, fetched),
                )
                dates.add(r.local_date)
        return dates

    def raw_points_for(self, data_type: str, dates: Iterable[str]) -> Iterator[tuple[str, str, dict]]:
        """Yield (point_key, local_date, payload) for one data type on the given local dates."""
        for chunk in _chunks(sorted(set(dates))):
            marks = ",".join("?" * len(chunk))
            sql = (f"SELECT point_key, local_date, payload FROM raw_points "
                   f"WHERE data_type = ? AND local_date IN ({marks}) ORDER BY local_date, point_key")
            for row in self.conn.execute(sql, (data_type, *chunk)):
                yield row["point_key"], row["local_date"], json.loads(row["payload"])

    def raw_dates(self, data_type: str, since: str | None = None) -> list[str]:
        sql = "SELECT DISTINCT local_date FROM raw_points WHERE data_type = ?"
        args: list[str] = [data_type]
        if since:
            sql += " AND local_date >= ?"
            args.append(since)
        return [r[0] for r in self.conn.execute(sql + " ORDER BY local_date", args)]

    def raw_count(self, data_type: str | None = None) -> int:
        if data_type is None:
            return self.conn.execute("SELECT COUNT(*) FROM raw_points").fetchone()[0]
        return self.conn.execute("SELECT COUNT(*) FROM raw_points WHERE data_type = ?", (data_type,)).fetchone()[0]

    # ---- clean layer -------------------------------------------------------

    def replace_clean(self, src_type: str, dates: Iterable[str], rows: dict[str, list[dict]]) -> None:
        """Delete every clean row produced by `src_type` on `dates`, then insert `rows` (table -> row dicts)."""
        unknown = set(rows) - set(CLEAN_TABLES)
        if unknown:
            raise ValueError(f"unknown clean tables: {sorted(unknown)}")
        dates = sorted(set(dates))
        allowed = set(dates)
        stray = {r.get("local_date") for table_rows in rows.values() for r in table_rows} - allowed
        if stray:
            raise ValueError(f"rows outside rebuild dates: {sorted(stray, key=str)}")
        for table, table_rows in rows.items():
            for r in table_rows:
                if "src_type" in r:
                    raise ValueError(f"rows for {table} must not set src_type; the store adds it")
                bad = set(r) - self._columns[table]
                if bad:
                    raise ValueError(f"unknown columns for {table}: {sorted(bad)}")
        with self.conn:
            for chunk in _chunks(dates):
                marks = ",".join("?" * len(chunk))
                for table in CLEAN_TABLES:
                    self.conn.execute(f"DELETE FROM {table} WHERE src_type = ? AND local_date IN ({marks})",
                                      (src_type, *chunk))
            for table, table_rows in rows.items():
                groups: dict[tuple[str, ...], list[tuple]] = defaultdict(list)
                for r in table_rows:
                    groups[tuple(r)].append((*r.values(), src_type))
                for cols, values in groups.items():
                    names = ", ".join((*cols, "src_type"))
                    marks = ", ".join("?" * (len(cols) + 1))
                    self.conn.executemany(f"INSERT INTO {table} ({names}) VALUES ({marks})", values)

    def table_counts(self) -> dict[str, int]:
        return {t: self.conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("raw_points", *CLEAN_TABLES)}

    # ---- sync state --------------------------------------------------------

    def get_state(self, data_type: str) -> SyncState | None:
        r = self.conn.execute("SELECT * FROM sync_state WHERE data_type = ?", (data_type,)).fetchone()
        return None if r is None else _state_from_row(r)

    def all_states(self) -> list[SyncState]:
        return [_state_from_row(r) for r in self.conn.execute("SELECT * FROM sync_state ORDER BY data_type")]

    def save_state(self, st: SyncState) -> None:
        with self.conn:
            self.conn.execute(
                SAVE_STATE,
                (st.data_type, _to_s(st.watermark_utc), _to_s(st.backfill_cursor_utc), int(st.backfill_done),
                 _to_s(st.earliest_data_utc), st.status, st.last_error, _to_s(st.deep_scan_utc), _now()),
            )

    # ---- pending rebuilds: raw rows stored, clean rows not yet (re)built ---------

    def add_pending(self, data_type: str, dates: Iterable[str], error: str) -> None:
        with self.conn:
            self.conn.executemany(
                "INSERT INTO pending_rebuild (data_type, local_date, error) VALUES (?, ?, ?) "
                "ON CONFLICT (data_type, local_date) DO UPDATE SET error = excluded.error",
                [(data_type, d, error) for d in sorted(set(dates))],
            )

    def pending_dates(self, data_type: str) -> list[str]:
        return [r[0] for r in self.conn.execute(
            "SELECT local_date FROM pending_rebuild WHERE data_type = ? ORDER BY local_date", (data_type,))]

    def pending_error(self, data_type: str) -> str | None:
        r = self.conn.execute(
            "SELECT error FROM pending_rebuild WHERE data_type = ? ORDER BY local_date LIMIT 1", (data_type,)
        ).fetchone()
        return None if r is None else r[0]

    def clear_pending(self, data_type: str, dates: Iterable[str]) -> None:
        with self.conn:
            self.conn.executemany("DELETE FROM pending_rebuild WHERE data_type = ? AND local_date = ?",
                                  [(data_type, d) for d in set(dates)])

    # ---- run log -----------------------------------------------------------

    def start_run(self, mode: str) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO sync_runs (started_at, mode) VALUES (?, ?)", (_now(), mode))
        return cur.lastrowid

    def finish_run(self, run_id: int, points: int, ok: int, failed: int, error_summary: str | None) -> None:
        with self.conn:
            self.conn.execute(
                "UPDATE sync_runs SET finished_at = ?, points_fetched = ?, types_ok = ?, types_failed = ?, "
                "error_summary = ? WHERE id = ?",
                (_now(), points, ok, failed, error_summary, run_id),
            )

    def last_run(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()
        return None if r is None else dict(r)

    # ---- snapshots ---------------------------------------------------------

    def save_snapshot(self, kind: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO snapshots (kind, fetched_at, payload) VALUES (?, ?, ?) "
                "ON CONFLICT (kind) DO UPDATE SET fetched_at = excluded.fetched_at, payload = excluded.payload",
                (kind, _now(), json.dumps(payload)),
            )

    def get_snapshot(self, kind: str) -> dict | None:
        r = self.conn.execute("SELECT payload FROM snapshots WHERE kind = ?", (kind,)).fetchone()
        return None if r is None else json.loads(r["payload"])

    # ---- analysis inputs (read) --------------------------------------------

    def daily_metric_values(self, metrics: Iterable[str], start: str, end: str) -> list[tuple[str, str, float]]:
        names = list(metrics)
        marks = ",".join("?" * len(names))
        sql = (f"SELECT local_date, metric, value FROM daily_metrics WHERE metric IN ({marks}) "
               f"AND local_date BETWEEN ? AND ? ORDER BY local_date, metric")
        return [(r[0], r[1], r[2]) for r in self.conn.execute(sql, (*names, start, end))]

    def daily_values_between(self, start: str, end: str) -> list[tuple[str, str, float]]:
        """Every Google daily value (any metric) dated start..end, for plugins."""
        sql = ("SELECT local_date, metric, value FROM daily_metrics WHERE local_date BETWEEN ? AND ? "
               "ORDER BY local_date, metric")
        return [(r[0], r[1], r[2]) for r in self.conn.execute(sql, (start, end))]

    def main_sleep_sessions(self, start: str, end: str) -> list[dict]:
        """Main sleep sessions by wake date, with the UTC offset recorded on their raw point."""
        sql = ("SELECT s.*, r.utc_offset AS utc_offset FROM sleep_sessions s "
               "LEFT JOIN raw_points r ON r.data_type = 'sleep' AND r.point_key = s.id "
               "WHERE s.is_main = 1 AND s.local_date BETWEEN ? AND ? ORDER BY s.local_date, s.start_utc")
        return [dict(r) for r in self.conn.execute(sql, (start, end))]

    def sleep_stages_for(self, session_ids: Iterable[str]) -> list[dict]:
        out: list[dict] = []
        for chunk in _chunks(sorted(set(session_ids))):
            marks = ",".join("?" * len(chunk))
            sql = f"SELECT * FROM sleep_stages WHERE session_id IN ({marks}) ORDER BY start_utc"
            out.extend(dict(r) for r in self.conn.execute(sql, chunk))
        return out

    def hr_minute_means(self, start: str, end: str) -> list[tuple[str, str, float]]:
        """(local_date, 'YYYY-MM-DDTHH:MM' UTC minute, mean bpm) for every minute with heart-rate samples."""
        sql = ("SELECT local_date, substr(ts_utc, 1, 16) AS minute, AVG(bpm) FROM heart_rate "
               "WHERE local_date BETWEEN ? AND ? GROUP BY local_date, minute ORDER BY minute")
        return [(r[0], r[1], r[2]) for r in self.conn.execute(sql, (start, end))]

    def sedentary_intervals(self, start: str, end: str) -> list[tuple[str, str, str]]:
        sql = ("SELECT local_date, start_utc, end_utc FROM activity_intervals "
               "WHERE metric = 'level_sedentary_min' AND local_date BETWEEN ? AND ? ORDER BY start_utc")
        return [(r[0], r[1], r[2]) for r in self.conn.execute(sql, (start, end))]

    def activity_intervals(self, metrics: Iterable[str], start: str, end: str) -> list[tuple[str, str, str, str, float]]:
        names = list(metrics)
        marks = ",".join("?" * len(names))
        sql = (f"SELECT local_date, metric, start_utc, end_utc, value FROM activity_intervals "
               f"WHERE metric IN ({marks}) AND local_date BETWEEN ? AND ? ORDER BY start_utc")
        return [(r[0], r[1], r[2], r[3], r[4]) for r in self.conn.execute(sql, (*names, start, end))]

    def exercise_sessions(self, start: str, end: str) -> list[dict]:
        sql = "SELECT * FROM exercise_sessions WHERE local_date BETWEEN ? AND ? ORDER BY start_utc"
        return [dict(r) for r in self.conn.execute(sql, (start, end))]

    def sample_values(self, metrics: Iterable[str], start: str, end: str) -> list[tuple[str, str, str, float]]:
        names = list(metrics)
        marks = ",".join("?" * len(names))
        sql = (f"SELECT local_date, ts_utc, metric, value FROM samples WHERE metric IN ({marks}) "
               f"AND local_date BETWEEN ? AND ? ORDER BY ts_utc")
        return [(r[0], r[1], r[2], r[3]) for r in self.conn.execute(sql, (*names, start, end))]

    def runs_between(self, start: str, end: str) -> list[dict]:
        sql = "SELECT * FROM runs WHERE local_date BETWEEN ? AND ? ORDER BY local_date, session_id"
        return [dict(r) for r in self.conn.execute(sql, (start, end))]

    def first_data_date(self, metrics: Iterable[str]) -> str | None:
        """Earliest local date with one of the given daily metrics or a main sleep; None when there is neither."""
        names = sorted(set(metrics))
        marks = ",".join("?" * len(names)) or "NULL"
        sql = (f"SELECT MIN(d) FROM (SELECT MIN(local_date) AS d FROM daily_metrics WHERE metric IN ({marks}) "
               "UNION ALL SELECT MIN(local_date) FROM sleep_sessions WHERE is_main = 1)")
        return self.conn.execute(sql, names).fetchone()[0]

    def metric_values(self, metric: str, start: str, end: str) -> list[tuple[str, float]]:
        """(local_date, value) of an analysis metric over [start, end], skipping empty values."""
        sql = ("SELECT local_date, value FROM metric_daily WHERE metric = ? AND local_date BETWEEN ? AND ? "
               "AND value IS NOT NULL ORDER BY local_date")
        return [(r[0], r[1]) for r in self.conn.execute(sql, (metric, start, end))]

    def alerts_between(self, start: str, end: str) -> list[tuple[str, str]]:
        sql = "SELECT local_date, kind FROM alerts WHERE local_date BETWEEN ? AND ? ORDER BY start_utc"
        return [(r[0], r[1]) for r in self.conn.execute(sql, (start, end))]

    # ---- analysis outputs (write/read) -------------------------------------

    def replace_analysis(self, since: str, metric_rows: list[dict], score_rows: list[dict],
                         insight_rows: list[dict], run_rows: list[dict] | tuple = ()) -> None:
        """Replace every analysis row dated on/after `since` in one transaction."""
        stray = {r.get("local_date") for rows in (metric_rows, score_rows, insight_rows, run_rows) for r in rows
                 if not isinstance(r.get("local_date"), str) or r["local_date"] < since}
        if stray:
            raise ValueError(f"analysis rows dated before since={since}: {sorted(stray, key=str)}")
        seen_runs: set[str] = set()
        for r in run_rows:
            sid = r.get("session_id")
            if not isinstance(sid, str) or not sid:
                raise ValueError(f"run row needs a non-empty string session_id, got {sid!r}")
            extra = set(r) - set(_RUN_COLS)
            if extra:
                raise ValueError(f"run row {sid!r} has unknown keys: {sorted(extra)}")
            if sid in seen_runs:
                raise ValueError(f"duplicate run session_id {sid!r}")
            seen_runs.add(sid)
        now = _now()
        # Serialise first (strict JSON) so a bad row fails before anything is deleted.
        score_values = [(r["local_date"], r["score"], r["value"], r.get("band"), r.get("confidence"), r["status"],
                         json.dumps(r["components"], allow_nan=False), json.dumps(r["evidence_ids"], allow_nan=False))
                        for r in score_rows]
        insight_values = [(r["local_date"], r["kind"], r["severity"], r["title"], r["detail"],
                           json.dumps(r["evidence_ids"], allow_nan=False), now) for r in insight_rows]
        with self.conn:
            for table in ("metric_daily", "scores_daily", "insights", "runs"):
                self.conn.execute(f"DELETE FROM {table} WHERE local_date >= ?", (since,))
            # A recomputed run whose local_date moved from before `since` to after must replace its old row.
            for chunk in _chunks(sorted(seen_runs)):
                self.conn.execute(f"DELETE FROM runs WHERE session_id IN ({','.join('?' * len(chunk))})", chunk)
            self.conn.executemany(
                "INSERT INTO metric_daily (local_date, metric, value, baseline, scale, z, band_status, confidence, "
                "quality, n_baseline) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(r["local_date"], r["metric"], r["value"], r.get("baseline"), r.get("scale"), r.get("z"),
                  r.get("band_status"), r.get("confidence"), r.get("quality"), r.get("n_baseline"))
                 for r in metric_rows])
            self.conn.executemany(
                "INSERT INTO scores_daily (local_date, score, value, band, confidence, status, components_json, "
                "evidence_ids) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", score_values)
            self.conn.executemany(
                "INSERT INTO insights (local_date, kind, severity, title, detail, evidence_ids, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)", insight_values)
            run_cols = _RUN_COLS
            self.conn.executemany(
                f"INSERT INTO runs ({', '.join(run_cols)}) VALUES ({', '.join('?' * len(run_cols))})",
                [tuple(r.get(c) for c in run_cols) for r in run_rows])

    def analysis_for_day(self, day: str) -> dict:
        scores = []
        for r in self.conn.execute("SELECT * FROM scores_daily WHERE local_date = ? ORDER BY score", (day,)):
            scores.append(_score_row(r))
        insights = []
        order = "CASE kind WHEN 'safety' THEN 0 ELSE 1 END, CASE severity WHEN 'alert' THEN 0 WHEN 'watch' THEN 1 ELSE 2 END, id"
        for r in self.conn.execute(f"SELECT * FROM insights WHERE local_date = ? ORDER BY {order}", (day,)):
            row = dict(r)
            row["evidence_ids"] = json.loads(row["evidence_ids"])
            insights.append(row)
        metrics = [dict(r) for r in self.conn.execute(
            "SELECT * FROM metric_daily WHERE local_date = ? ORDER BY metric", (day,))]
        return {"scores": scores, "insights": insights, "metrics": metrics}

    def start_analysis_run(self, since: str) -> int:
        with self.conn:
            cur = self.conn.execute("INSERT INTO analysis_runs (started_at, since) VALUES (?, ?)", (_now(), since))
        return cur.lastrowid

    def finish_analysis_run(self, run_id: int, error: str | None, summary: str | None) -> None:
        with self.conn:
            cur = self.conn.execute("UPDATE analysis_runs SET finished_at = ?, error = ?, summary = ? WHERE id = ?",
                                    (_now(), error, summary, run_id))
            if cur.rowcount != 1:
                raise ValueError(f"unknown analysis run id {run_id}")

    def last_analysis(self) -> dict | None:
        r = self.conn.execute("SELECT * FROM analysis_runs ORDER BY id DESC LIMIT 1").fetchone()
        return None if r is None else dict(r)

    # ---- coach: meta, journal, notes ---------------------------------------

    def set_meta(self, key: str, value: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
                (key, value, _now()))

    def get_meta(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return None if r is None else r[0]

    def add_journal(self, local_date: str, fields: dict) -> dict:
        """Insert one journal entry; `fields` must already be validated (coach.journal)."""
        unknown = set(fields) - set(_JOURNAL_COLS)
        if unknown:
            raise ValueError(f"unknown journal fields: {sorted(unknown)}")
        cols = ["local_date", "created_at", *fields]
        with self.conn:
            cur = self.conn.execute(
                f"INSERT INTO journal ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                (local_date, _now(), *fields.values()))
        return dict(self.conn.execute("SELECT * FROM journal WHERE id = ?", (cur.lastrowid,)).fetchone())

    def journal_between(self, start: str, end: str) -> list[dict]:
        sql = "SELECT * FROM journal WHERE local_date BETWEEN ? AND ? ORDER BY local_date, id"
        return [dict(r) for r in self.conn.execute(sql, (start, end))]

    def add_note(self, kind: str, text: str, review_on: str | None) -> dict:
        with self.conn:
            cur = self.conn.execute("INSERT INTO coach_notes (created_at, kind, text, review_on) VALUES (?, ?, ?, ?)",
                                    (_now(), kind, text, review_on))
        return self.get_note(cur.lastrowid)

    def get_note(self, note_id: int) -> dict | None:
        r = self.conn.execute("SELECT * FROM coach_notes WHERE id = ?", (note_id,)).fetchone()
        return None if r is None else dict(r)

    def notes(self, status: str | None = "active", due_by: str | None = None) -> list[dict]:
        """Notes with dated reviews first (earliest, so overdue, first), undated last."""
        clauses, args = [], []
        if status is not None:
            clauses.append("status = ?")
            args.append(status)
        if due_by is not None:
            clauses.append("review_on IS NOT NULL AND review_on <= ?")
            args.append(due_by)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        sql = f"SELECT * FROM coach_notes {where}ORDER BY review_on IS NULL, review_on, id"
        return [dict(r) for r in self.conn.execute(sql, args)]

    def update_note(self, note_id: int, changes: dict) -> dict | None:
        """Apply validated changes; None when the note doesn't exist."""
        bad = set(changes) - set(_NOTE_UPDATABLE)
        if bad:
            raise ValueError(f"cannot update note fields: {sorted(bad)}")
        sets = ", ".join([*(f"{k} = ?" for k in changes), "updated_at = ?"])
        with self.conn:
            cur = self.conn.execute(f"UPDATE coach_notes SET {sets} WHERE id = ?",
                                    (*changes.values(), _now(), note_id))
        return self.get_note(note_id) if cur.rowcount else None

    # ---- coach reads -------------------------------------------------------

    def last_finished_run(self) -> dict | None:
        r = self.conn.execute(
            "SELECT * FROM sync_runs WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        return None if r is None else dict(r)

    def first_analysis_date(self) -> str | None:
        """Earliest day with analysis rows (scores or metrics)."""
        return self.conn.execute(
            "SELECT MIN(d) FROM (SELECT MIN(local_date) AS d FROM scores_daily "
            "UNION ALL SELECT MIN(local_date) FROM metric_daily)").fetchone()[0]

    def last_analysis_date(self) -> str | None:
        """Latest day with analysis rows (scores or metrics)."""
        return self.conn.execute(
            "SELECT MAX(d) FROM (SELECT MAX(local_date) AS d FROM scores_daily "
            "UNION ALL SELECT MAX(local_date) FROM metric_daily)").fetchone()[0]

    def analysis_metric_names(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT metric FROM metric_daily ORDER BY metric")]

    def score_names(self) -> list[str]:
        return [r[0] for r in self.conn.execute("SELECT DISTINCT score FROM scores_daily ORDER BY score")]

    def metric_rows_between(self, metrics: Iterable[str], start: str, end: str) -> list[dict]:
        names = list(metrics)
        marks = ",".join("?" * len(names)) or "NULL"
        sql = (f"SELECT * FROM metric_daily WHERE metric IN ({marks}) AND local_date BETWEEN ? AND ? "
               "ORDER BY metric, local_date")
        return [dict(r) for r in self.conn.execute(sql, (*names, start, end))]

    def scores_between(self, names: Iterable[str], start: str, end: str) -> list[dict]:
        names = list(names)
        marks = ",".join("?" * len(names)) or "NULL"
        sql = (f"SELECT * FROM scores_daily WHERE score IN ({marks}) AND local_date BETWEEN ? AND ? "
               "ORDER BY score, local_date")
        return [_score_row(r) for r in self.conn.execute(sql, (*names, start, end))]

    def latest_score(self, name: str, day: str) -> dict | None:
        r = self.conn.execute("SELECT * FROM scores_daily WHERE score = ? AND local_date <= ? "
                              "ORDER BY local_date DESC LIMIT 1", (name, day)).fetchone()
        return None if r is None else _score_row(r)

    def sleep_sessions_between(self, start: str, end: str) -> list[dict]:
        sql = "SELECT * FROM sleep_sessions WHERE local_date BETWEEN ? AND ? ORDER BY start_utc"
        return [dict(r) for r in self.conn.execute(sql, (start, end))]

    def count_rows(self, table: str) -> int:
        if table not in _COUNTABLE:
            raise ValueError(f"cannot count table {table!r}")
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def heart_rate_days(self, start: str, end: str) -> int:
        sql = "SELECT COUNT(DISTINCT local_date) FROM heart_rate WHERE local_date BETWEEN ? AND ?"
        return self.conn.execute(sql, (start, end)).fetchone()[0]

    def schema_description(self) -> list[dict]:
        out = []
        for r in self.conn.execute("SELECT name, type FROM sqlite_master WHERE type IN ('table', 'view') "
                                   "AND name NOT LIKE 'sqlite_%' ORDER BY type, name"):
            cols = [{"name": c["name"], "type": c["type"]}
                    for c in self.conn.execute("SELECT name, type FROM pragma_table_info(?)", (r[0],))]
            out.append({"name": r[0], "type": r[1], "columns": cols})
        return out
