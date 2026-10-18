"""HTTP client for the Google Health API v4: auth header, throttling, retries, pagination."""
from __future__ import annotations

import logging
import math
import random
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Callable, Iterator

import httpx

from .config import API_BASE, REQUESTS_PER_SECOND
from .errors import ApiError, ReauthRequired
from .registry import DataType

log = logging.getLogger(__name__)

TokenProvider = Callable[[bool], str]  # force_refresh -> access token
RETRYABLE = {429, 500, 502, 503, 504}
MAX_RETRY_AFTER = 120.0  # seconds; never sleep longer than this on a server-supplied hint


def _message(resp: httpx.Response) -> str:
    try:
        err = resp.json()["error"]
        msg = err["message"]
        status = err.get("status")
        return f"{status}: {msg}" if status else str(msg)
    except (ValueError, KeyError, TypeError, AttributeError):
        return resp.text[:300] or resp.reason_phrase


class HealthClient:
    def __init__(
        self,
        token_provider: TokenProvider,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        rps: float = REQUESTS_PER_SECOND,
        max_attempts: int = 6,
        base_url: str = API_BASE,
    ):
        self._token = token_provider
        self._owns_http = http is None
        self._http = http or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        self._sleep = sleep
        self._clock = clock
        self._now = now
        self._min_gap = 1.0 / rps if rps else 0.0
        self._max_attempts = max_attempts
        self._base = base_url
        self._last: float | None = None
        self._network_down = False

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> HealthClient:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def iter_points(self, dt: DataType, start: datetime, end: datetime) -> Iterator[list[dict]]:
        """Yield pages of data points for `dt` in [start, end)."""
        path = f"/users/me/dataTypes/{dt.name}/dataPoints"
        if dt.method == "reconcile":
            path += ":reconcile"
        params: dict[str, str | int] = {"filter": dt.filter_for(start, end), "pageSize": dt.page_size}
        if dt.data_source_family:
            params["dataSourceFamily"] = dt.data_source_family
        seen: set[str] = set()
        while True:
            data = self._get(path, params)
            yield data.get("dataPoints", [])
            token = data.get("nextPageToken")
            if not token:
                return
            if token in seen:
                raise ApiError(0, f"pagination loop on {dt.name}: repeated pageToken")
            seen.add(token)
            params = {**params, "pageToken": token}

    def get_profile(self) -> dict:
        return self._get("/users/me/profile")

    def list_paired_devices(self) -> dict:
        return self._get("/users/me/pairedDevices")

    # ---- internals ---------------------------------------------------------

    def _throttle(self) -> None:
        if self._last is not None and self._min_gap:
            wait = self._min_gap - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self._last = self._clock()

    def _retry_after(self, value: str | None) -> float | None:
        """Seconds to wait per a Retry-After header (delta-seconds or HTTP-date), clamped; None if unusable."""
        if not value:
            return None
        try:
            seconds = float(value)
        except ValueError:
            try:
                when = parsedate_to_datetime(value)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                seconds = (when - self._now()).total_seconds()
            except (TypeError, ValueError, IndexError, OverflowError):
                return None
        if not math.isfinite(seconds):
            return None
        return min(MAX_RETRY_AFTER, max(0.0, seconds))

    def _backoff(self, attempt: int, retry_after: str | None) -> float:
        hinted = self._retry_after(retry_after)
        if hinted is not None:
            return hinted
        return min(60.0, 2.0 ** (attempt - 1)) + random.uniform(0, 1)

    def _get(self, path: str, params: dict | None = None) -> dict:
        if self._network_down:
            raise ApiError(0, f"{path}: network unavailable (earlier requests failed); skipping")
        force_refresh = False
        refreshed = False
        attempt = 0
        while True:
            attempt += 1
            self._throttle()
            headers = {"Authorization": f"Bearer {self._token(force_refresh)}"}
            force_refresh = False
            try:
                resp = self._http.get(self._base + path, params=params, headers=headers)
            except httpx.RequestError as exc:
                if attempt >= self._max_attempts:
                    self._network_down = True
                    raise ApiError(0, f"{path}: network error: {type(exc).__name__}: {exc}") from exc
                log.warning("network error on %s (attempt %d): %s", path, attempt, exc)
                self._sleep(self._backoff(attempt, None))
                continue
            if resp.status_code == 401:
                if refreshed:
                    raise ReauthRequired(f"{path}: still unauthorized after token refresh")
                refreshed = force_refresh = True
                continue
            if resp.status_code in RETRYABLE:
                if attempt >= self._max_attempts:
                    raise ApiError(resp.status_code, f"{path}: {_message(resp)}")
                log.warning("HTTP %d on %s (attempt %d), backing off", resp.status_code, path, attempt)
                self._sleep(self._backoff(attempt, resp.headers.get("Retry-After")))
                continue
            if resp.status_code >= 400:
                raise ApiError(resp.status_code, f"{path}: {_message(resp)}")
            try:
                data = resp.json()
            except ValueError:
                raise ApiError(resp.status_code, f"{path}: non-JSON response: {resp.text[:200]!r}") from None
            if not isinstance(data, dict):
                raise ApiError(resp.status_code, f"{path}: unexpected JSON type {type(data).__name__}")
            return data
