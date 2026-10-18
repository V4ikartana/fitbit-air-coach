"""Sync orchestration: backfill each data type's history, then keep it current."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable, Iterable, Protocol

from . import normalize
from .config import DEEP_RESCAN, DEEP_RESCAN_EVERY, HISTORY_FLOOR, MAX_BACKFILL_WINDOW_DAYS, OVERLAP
from .errors import ApiError, NormalizeError, ReauthRequired
from .registry import REGISTRY, DataType
from .store import RawRow, Store, SyncState
from .timeutil import UTC, iso_utc, parse_ts

log = logging.getLogger(__name__)


class Client(Protocol):
    def iter_points(self, dt: DataType, start: datetime, end: datetime) -> Iterable[list[dict]]: ...
    def get_profile(self) -> dict: ...
    def list_paired_devices(self) -> dict: ...


@dataclass
class TypeResult:
    name: str
    status: str  # ok | empty | unavailable | failed
    points: int = 0
    error: str | None = None


@dataclass
class RunResult:
    run_id: int
    results: list[TypeResult] = field(default_factory=list)
    reauth_required: bool = False


def _row_start(row: RawRow) -> datetime:
    """Earliest instant a raw row covers; daily points have no start, so use their date at 00:00 UTC."""
    if row.start_utc:
        return parse_ts(row.start_utc)
    return datetime.fromisoformat(row.local_date).replace(tzinfo=UTC)


def forward_windows(start: datetime, end: datetime, days: int) -> list[tuple[datetime, datetime]]:
    out = []
    step = timedelta(days=days)
    cursor = start
    while cursor < end:
        nxt = min(cursor + step, end)
        out.append((cursor, nxt))
        cursor = nxt
    return out


class Syncer:
    def __init__(self, store: Store, client: Client, registry: Iterable[DataType] = REGISTRY,
                 now: Callable[[], datetime] = lambda: datetime.now(UTC)):
        self.store = store
        self.client = client
        self.registry = tuple(registry)
        self.now = now

    def run(self, types: list[str] | None = None, full_backfill: bool = False) -> RunResult:
        selected = [dt for dt in self.registry if types is None or dt.name in types]
        unfinished = full_backfill or any(
            (st := self.store.get_state(dt.name)) is None or not st.backfill_done for dt in selected)
        result = RunResult(run_id=self.store.start_run("backfill" if unfinished else "incremental"))
        try:
            for dt in selected:
                try:
                    result.results.append(self._sync_type(dt, full_backfill))
                except ReauthRequired as exc:
                    log.error("Google login expired or revoked: %s", exc)
                    result.reauth_required = True
                    self._mark(dt.name, "failed", f"reauth required: {exc}")
                    result.results.append(TypeResult(dt.name, "failed", error=f"reauth required: {exc}"))
                    break
                except ApiError as exc:
                    status = "unavailable" if exc.status in (403, 404) else "failed"
                    log.warning("%s %s: %s", dt.name, status, exc)
                    self._mark(dt.name, status, str(exc))
                    result.results.append(TypeResult(dt.name, status, error=str(exc)))
                except NormalizeError as exc:
                    log.error("%s failed: %s", dt.name, exc)
                    self._mark(dt.name, "failed", str(exc))
                    result.results.append(TypeResult(dt.name, "failed", error=str(exc)))
                except Exception as exc:  # one broken type must not stop the others
                    log.exception("%s failed unexpectedly", dt.name)
                    self._mark(dt.name, "failed", repr(exc))
                    result.results.append(TypeResult(dt.name, "failed", error=repr(exc)))
            if not result.reauth_required:
                self._snapshots()
        finally:
            self._finish(result)  # always record the run, even if something unexpected escapes
        return result

    # ---- per type ----------------------------------------------------------

    def _sync_type(self, dt: DataType, full_backfill: bool) -> TypeResult:
        now = self.now()
        st = self.store.get_state(dt.name) or SyncState(dt.name)
        if st.watermark_utc is not None and st.watermark_utc > now:
            # The clock was wrong on an earlier run (or went backwards). Follow the clock, and force a deep re-scan
            # so nothing between the true time and the bogus watermark is skipped.
            st.watermark_utc, st.deep_scan_utc = now, None
        self._retry_pending(dt)
        if not dt.backfill:
            # Settings-like data exists for every date asked about, so history is meaningless: only keep recent
            # days. The first run starts from `now`, and the deep re-scan below then covers the last 14 days.
            if st.watermark_utc is None:
                st.watermark_utc = now
            st.backfill_done = True
        elif full_backfill:
            st.backfill_done = False
            st.backfill_cursor_utc = None
        points = 0
        if st.watermark_utc is not None:
            deep = st.deep_scan_utc is None or now - st.deep_scan_utc >= DEEP_RESCAN_EVERY
            for start, end in forward_windows(st.watermark_utc - (DEEP_RESCAN if deep else OVERLAP), now,
                                              dt.window_days):
                points += self._fetch(dt, start, end, st)[1]
                st.watermark_utc = end  # only after the window is stored; windows ascend, the last ends at `now`
                self.store.save_state(st)
            if deep:
                st.deep_scan_utc = now  # the wide pass completed; the next one is due in 24 hours
        if not st.backfill_done:
            points += self._backfill(dt, st, now)
        norm_error = self.store.pending_error(dt.name) if self.store.pending_dates(dt.name) else None
        if norm_error:
            st.status, st.last_error = "failed", norm_error
        else:
            st.status = "ok" if st.earliest_data_utc is not None else "empty"
            st.last_error = None
        self.store.save_state(st)
        log.info("%s: %s, %d points", dt.name, st.status, points)
        return TypeResult(dt.name, st.status, points, norm_error)

    def _retry_pending(self, dt: DataType) -> None:
        """Re-run clean rebuilds that failed earlier; they stay pending (and the type failed) until they succeed."""
        pending = self.store.pending_dates(dt.name)
        if not pending:
            return
        size = len(pending) if dt.name == "sleep" else normalize.REBUILD_BATCH_DAYS  # sleep widens, so one call
        for i in range(0, len(pending), size):
            batch = pending[i : i + size]
            try:
                normalize.rebuild(self.store, dt, batch)
            except NormalizeError as exc:
                log.error("%s: pending clean rebuild still failing: %s", dt.name, exc)
                self.store.add_pending(dt.name, pending[i:], str(exc))  # this batch and every later one
                return
            self.store.clear_pending(dt.name, batch)

    def _backfill(self, dt: DataType, st: SyncState, now: datetime) -> int:
        """Walk backwards to HISTORY_FLOOR. Consecutive empty windows double in size (capped), so a type with no
        data costs a handful of requests, but we never stop early: sparse data (e.g. a weigh-in) can be anywhere."""
        cursor = st.backfill_cursor_utc or now
        empty_run = 0
        points = 0
        while cursor > HISTORY_FLOOR:
            days = min(dt.window_days * 2**empty_run, MAX_BACKFILL_WINDOW_DAYS)
            start = max(cursor - timedelta(days=days), HISTORY_FLOOR)
            # Judge emptiness by points RETURNED, not stored. Raw + clean are written before the cursor moves.
            oversized = days > dt.window_days
            # A galloped window that has data is only sampled (first page) and not consumed: the cursor stays and the
            # walk continues in base-size windows, so a huge window never has to be read, parsed and rebuilt at once.
            returned, stored = self._fetch(dt, start, cursor, st, max_pages=1 if oversized else None)
            log.info("%s backfill %s..%s: %d points", dt.name, start.date(), cursor.date(), returned)
            if oversized and returned:
                empty_run = 0
                self.store.save_state(st)
                continue
            points += stored  # a probed window's points are counted when the walk reads them in full
            empty_run = 0 if returned else empty_run + 1
            cursor = start
            st.backfill_cursor_utc = cursor
            if st.watermark_utc is None:
                st.watermark_utc = now  # the first window ended at `now`, so history is covered up to there
            self.store.save_state(st)
        st.backfill_done = True
        return points

    def _fetch(self, dt: DataType, start: datetime, end: datetime, st: SyncState,
               max_pages: int | None = None) -> tuple[int, int]:
        """Fetch one window (at most `max_pages` pages if given).

        Returns (points returned by the API, points parsed and stored)."""
        returned = stored = 0
        dates: set[str] = set()
        earliest: datetime | None = None
        first_error: Exception | None = None
        # Crash safety: within a window the order is upsert_raw -> normalize.rebuild -> (caller) save_state of the
        # cursor/watermark. A crash before save_state just re-fetches the same window later, which is idempotent.
        try:
            with_points = 0  # pages that contained points; an empty page (even one with a next token) doesn't count
            for page in self.client.iter_points(dt, start, end):
                rows: list[RawRow] = []
                dropped = 0
                for point in page:
                    try:
                        rows.append(normalize.to_raw_row(dt, point))
                    except normalize.MALFORMED as exc:
                        dropped += 1
                        first_error = first_error or exc
                if dropped:
                    log.warning("dropping %d unparseable %s point(s) from a page of %d, e.g. %r",
                                dropped, dt.name, len(page), first_error)
                dates |= self.store.upsert_raw(rows)
                returned += len(page)
                stored += len(rows)
                for r in rows:
                    first = _row_start(r)
                    earliest = first if earliest is None or first < earliest else earliest
                with_points += bool(page)
                if max_pages is not None and with_points >= max_pages:
                    break
        except ApiError as exc:
            raise ApiError(exc.status, f"{exc.message} [window {iso_utc(start)}..{iso_utc(end)}]") from exc
        if returned and not stored:
            # Never treat "the API sent data we can't read" as "no data": that would walk past real history.
            raise NormalizeError(f"{dt.name} [{iso_utc(start)}..{iso_utc(end)}): none of {returned} points "
                                 f"could be parsed, e.g. {first_error!r}")
        if earliest is not None and (st.earliest_data_utc is None or earliest < st.earliest_data_utc):
            st.earliest_data_utc = earliest
        if dates:
            try:
                normalize.rebuild(self.store, dt, dates)
            except NormalizeError as exc:
                # Raw rows are stored, so progress is safe to keep; `fitbit-air rebuild` repairs the clean tables.
                log.error("%s: clean rebuild failed, raw data kept: %s", dt.name, exc)
                self.store.add_pending(dt.name, dates, str(exc))
        return returned, stored

    # ---- bookkeeping -------------------------------------------------------

    def _mark(self, name: str, status: str, error: str) -> None:
        st = self.store.get_state(name) or SyncState(name)
        st.status = status
        st.last_error = error
        self.store.save_state(st)

    def _snapshots(self) -> None:
        for kind, fetch in (("profile", self.client.get_profile), ("paired_devices", self.client.list_paired_devices)):
            try:
                self.store.save_snapshot(kind, fetch())
            except (ApiError, ReauthRequired) as exc:
                log.warning("could not fetch %s: %s", kind, exc)

    def _finish(self, result: RunResult) -> None:
        ok = sum(r.status in ("ok", "empty") for r in result.results)
        failed = sum(r.status == "failed" for r in result.results)
        if result.reauth_required:
            summary = "reauth_required"
        else:
            summary = "; ".join(f"{r.name}: {r.error}" for r in result.results if r.error) or None
        self.store.finish_run(result.run_id, sum(r.points for r in result.results), ok, failed, summary)
