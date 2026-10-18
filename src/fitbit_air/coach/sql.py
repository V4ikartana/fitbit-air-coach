"""Read-only SQL for the coach: ro connection, an authorizer that denies every write, one statement, caps."""
from __future__ import annotations

import math
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

MAX_ROWS = 500
MAX_CELL_CHARS = 2000
MAX_VALUE_BYTES = 100_000  # SQLITE_LIMIT_LENGTH: no single string/blob/row bigger than this
TIME_LIMIT_S = 5.0
_ALLOWED = {sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_RECURSIVE}
_ALLOWED_PRAGMAS = {"table_info", "table_xinfo", "index_list", "index_info"}


class SqlError(Exception):
    pass


def _authorizer(action: int, arg1, arg2, _db, _trigger) -> int:
    if action in _ALLOWED:
        return sqlite3.SQLITE_OK
    if action == sqlite3.SQLITE_PRAGMA and str(arg1).lower() in _ALLOWED_PRAGMAS:  # arg2 is the table name
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _clean(value):
    if isinstance(value, bytes):
        return f"<blob {len(value)} bytes>"
    if isinstance(value, float):
        return round(value, 4) if math.isfinite(value) else None  # inf/nan are not valid JSON
    if isinstance(value, str) and len(value) > MAX_CELL_CHARS:
        return f"{value[:MAX_CELL_CHARS]}…<truncated, {len(value)} chars>"
    return value


def _check_not_empty(sql: str) -> None:
    if not sql.strip().strip(";").strip():
        raise SqlError("empty query")


def run_query(db: Path, sql: str, time_limit_s: float = TIME_LIMIT_S, max_rows: int = MAX_ROWS) -> dict:
    db = Path(db)
    if not db.exists():
        raise SqlError(f"database not found at {db}")
    _check_not_empty(sql)
    conn = sqlite3.connect(f"file:{quote(str(db.resolve()))}?mode=ro", uri=True)  # quote: '?' and '#' in paths
    try:
        conn.execute("PRAGMA query_only = ON")
        conn.set_authorizer(_authorizer)
        conn.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, MAX_VALUE_BYTES)
        deadline = time.monotonic() + time_limit_s
        conn.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
        try:
            cur = conn.execute(sql)  # unchanged: sqlite3 itself refuses a second statement
            rows = cur.fetchmany(max_rows + 1)
        except sqlite3.ProgrammingError as exc:
            if "one statement" in str(exc):
                raise SqlError("only one statement is allowed") from exc
            raise SqlError(f"query refused or failed: {exc}") from exc
        except sqlite3.DatabaseError as exc:
            if "interrupted" in str(exc) and time.monotonic() > deadline:
                raise SqlError(f"query stopped: over the {time_limit_s:g} s time limit") from exc
            raise SqlError(f"query refused or failed: {exc}") from exc
        columns = [d[0] for d in cur.description or ()]
        return {"columns": columns, "rows": [[_clean(v) for v in r] for r in rows[:max_rows]],
                "truncated": len(rows) > max_rows}
    finally:
        conn.close()
