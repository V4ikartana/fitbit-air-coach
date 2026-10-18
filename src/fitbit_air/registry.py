"""The Google Health API data types we sync, and how to query each one."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .timeutil import UTC, canonical_ts, date_str, iso_utc, require_aware


class Kind(str, Enum):
    INTERVAL = "interval"
    SAMPLE = "sample"
    DAILY = "daily"
    SESSION = "session"
    SLEEP = "sleep"


@dataclass(frozen=True)
class DataType:
    name: str  # kebab-case API name, e.g. "heart-rate"
    kind: Kind
    window_days: int  # time span covered by one request window
    method: str = "reconcile"  # or "list" for types without reconcile support
    discriminator: str | None = None  # payload field that distinguishes points sharing a start time
    page_size: int = 10000
    keyed_by_name: bool = False  # points carry a stable resource `name` that is used as their key
    backfill: bool = True  # False for settings-like types Google returns for every requested date
    data_source_family: str | None = None  # optional `dataSourceFamily` query parameter (list and reconcile)

    @property
    def field(self) -> str:
        """JSON key holding this type's payload inside a data point, e.g. heart-rate -> heartRate."""
        head, *rest = self.name.split("-")
        return head + "".join(part.capitalize() for part in rest)

    @property
    def filter_name(self) -> str:
        return self.name.replace("-", "_")

    def filter_for(self, start: datetime, end: datetime) -> str:
        """AIP-160 filter selecting points in [start, end).

        Date and civil-time filters are in the user's local time, so they are widened by a day on each
        side; upserts make the overlap harmless.
        """
        require_aware(start)
        require_aware(end)
        if start >= end:
            raise ValueError(f"empty window: start {start!r} >= end {end!r}")
        f = self.filter_name
        day = timedelta(days=1)
        if self.kind is Kind.INTERVAL:
            attr, lo, hi = f"{f}.interval.start_time", iso_utc(start), iso_utc(end)
        elif self.kind is Kind.SAMPLE:
            attr, lo, hi = f"{f}.sample_time.physical_time", iso_utc(start), iso_utc(end)
        elif self.kind is Kind.DAILY:
            attr = f"{f}.date"
            lo = (start - day).astimezone(UTC).date().isoformat()
            hi = (end + day).astimezone(UTC).date().isoformat()
        elif self.kind is Kind.SESSION:
            attr, lo, hi = f"{f}.interval.civil_start_time", _civil(start - day), _civil(end + day)
        elif self.kind is Kind.SLEEP:
            attr, lo, hi = f"{f}.interval.end_time", iso_utc(start), iso_utc(end)
        else:
            raise ValueError(f"unknown kind {self.kind}")
        return f'{attr} >= "{lo}" AND {attr} < "{hi}"'

    def point_key(self, body: dict, name: str | None = None) -> str:
        """Stable identity of a data point, used as the raw-table primary key.

        `body` is the inner payload, i.e. `data_point[self.field]`, not the whole data point.
        `name` (the point's resource name) is used only for types with `keyed_by_name`.
        """
        if self.keyed_by_name and name:
            return name
        if self.kind is Kind.DAILY:
            key = date_str(body["date"])
        elif self.kind is Kind.SAMPLE:
            key = canonical_ts(body["sampleTime"]["physicalTime"])
        else:
            key = canonical_ts(body["interval"]["startTime"])
        if self.discriminator:
            if self.discriminator not in body:
                raise KeyError(f"{self.name}: missing {self.discriminator}")
            key += "|" + str(body[self.discriminator])
        return key


def _civil(moment: datetime) -> str:
    return require_aware(moment).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S")


_D = DataType
REGISTRY: tuple[DataType, ...] = (
    # Heart
    _D("heart-rate", Kind.SAMPLE, 1),
    _D("heart-rate-variability", Kind.SAMPLE, 7),
    _D("daily-heart-rate-variability", Kind.DAILY, 30),
    _D("daily-resting-heart-rate", Kind.DAILY, 30),
    _D("daily-heart-rate-zones", Kind.DAILY, 30, backfill=False),  # zone settings, synthesised for any date
    _D("time-in-heart-rate-zone", Kind.INTERVAL, 7, discriminator="heartRateZoneType"),
    _D("irregular-rhythm-notification", Kind.SESSION, 90, method="list"),
    # Sleep & recovery
    _D("sleep", Kind.SLEEP, 90, page_size=25, keyed_by_name=True),
    _D("daily-sleep-temperature-derivations", Kind.DAILY, 30),
    _D("daily-respiratory-rate", Kind.DAILY, 30),
    _D("daily-oxygen-saturation", Kind.DAILY, 30),
    _D("respiratory-rate-sleep-summary", Kind.SAMPLE, 7),
    _D("oxygen-saturation", Kind.SAMPLE, 7),
    # Activity
    _D("steps", Kind.INTERVAL, 7),
    _D("distance", Kind.INTERVAL, 7),
    _D("active-zone-minutes", Kind.INTERVAL, 7, discriminator="heartRateZone"),
    _D("active-minutes", Kind.INTERVAL, 7),
    _D("sedentary-period", Kind.INTERVAL, 7),
    _D("activity-level", Kind.INTERVAL, 7, discriminator="activityLevelType"),
    _D("active-energy-burned", Kind.INTERVAL, 7),
    _D("exercise", Kind.SESSION, 90, page_size=25, keyed_by_name=True),
    # Fitness & body
    _D("daily-vo2-max", Kind.DAILY, 30),
    _D("vo2-max", Kind.SAMPLE, 90),
    _D("run-vo2-max", Kind.SAMPLE, 90),
    _D("weight", Kind.SAMPLE, 90),
    _D("body-fat", Kind.SAMPLE, 90),
    _D("height", Kind.SAMPLE, 90),
)
BY_NAME: dict[str, DataType] = {dt.name: dt for dt in REGISTRY}
