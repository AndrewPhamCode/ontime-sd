"""Writing parsed feed rows to Postgres.

Both writers use a single INSERT over unnested arrays rather than executemany.
That is one round trip instead of one per row, and because Postgres reports the
affected row count, it yields the true number of rows actually inserted rather
than the number offered. That count is what poll_log records, and the difference
between offered and inserted is the deduplication working. See ADR-0006.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Hashable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TypeVar

import asyncpg

from ontime_sd.feeds import PredictionRow, VehiclePositionRow

log = logging.getLogger(__name__)

_Row = TypeVar("_Row")

_POSITION_INSERT = """
insert into vehicle_positions (
    vehicle_id, ts, trip_id, route_id, start_date, lat, lon, bearing, speed,
    current_stop_sequence, current_status, occupancy_status
)
select * from unnest(
    $1::text[], $2::timestamptz[], $3::text[], $4::text[], $5::date[],
    $6::float8[], $7::float8[], $8::real[], $9::real[],
    $10::int[], $11::smallint[], $12::smallint[]
)
on conflict do nothing
"""

_PREDICTION_INSERT = """
insert into predictions (
    start_date, trip_id, stop_sequence, observed_at, stop_id, route_id,
    arrival_time, departure_time, delay_seconds, schedule_relationship, vehicle_id
)
select * from unnest(
    $1::date[], $2::text[], $3::int[], $4::timestamptz[], $5::text[], $6::text[],
    $7::timestamptz[], $8::timestamptz[], $9::int[], $10::smallint[], $11::text[]
)
on conflict do nothing
"""


def _inserted_count(status: str) -> int:
    """Parse the row count out of an 'INSERT 0 12' command status."""
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        log.warning("could not parse insert status", extra={"status": status})
        return 0


def _dedupe(rows: Sequence[_Row], key: Callable[[_Row], Hashable]) -> list[_Row]:
    """Keep the first row per key.

    Duplicate keys inside one statement are a feed anomaly rather than an
    expected case, but deduplicating here keeps the insert honest: the reported
    count then reflects distinct rows offered.
    """
    seen: set[Hashable] = set()
    out: list[_Row] = []
    for row in rows:
        k = key(row)
        if k in seen:
            continue
        seen.add(k)
        out.append(row)
    return out


async def write_positions(conn: asyncpg.Connection, rows: Sequence[VehiclePositionRow]) -> int:
    """Insert vehicle positions, returning how many were new."""
    if not rows:
        return 0

    unique = _dedupe(rows, lambda r: (r.vehicle_id, r.ts))
    status = await conn.execute(
        _POSITION_INSERT,
        [r.vehicle_id for r in unique],
        [r.ts for r in unique],
        [r.trip_id for r in unique],
        [r.route_id for r in unique],
        [r.start_date for r in unique],
        [r.lat for r in unique],
        [r.lon for r in unique],
        [r.bearing for r in unique],
        [r.speed for r in unique],
        [r.current_stop_sequence for r in unique],
        [r.current_status for r in unique],
        [r.occupancy_status for r in unique],
    )
    return _inserted_count(status)


async def write_predictions(conn: asyncpg.Connection, rows: Sequence[PredictionRow]) -> int:
    """Insert predictions, returning how many were new."""
    if not rows:
        return 0

    unique = _dedupe(rows, lambda r: (*r.cache_key, r.observed_at))
    status = await conn.execute(
        _PREDICTION_INSERT,
        [r.start_date for r in unique],
        [r.trip_id for r in unique],
        [r.stop_sequence for r in unique],
        [r.observed_at for r in unique],
        [r.stop_id for r in unique],
        [r.route_id for r in unique],
        [r.arrival_time for r in unique],
        [r.departure_time for r in unique],
        [r.delay_seconds for r in unique],
        [r.schedule_relationship for r in unique],
        [r.vehicle_id for r in unique],
    )
    return _inserted_count(status)


@dataclass(slots=True)
class PredictionCache:
    """Last written prediction per (start_date, trip_id, stop_sequence).

    This is what makes change-only storage work: without it, every poll would
    rewrite a near identical copy of every upcoming stop time, which is tens of
    millions of rows a day. See ADR-0005.

    select_changed does not mutate state and remember does, deliberately. The
    caller only remembers rows after the write succeeds, because remembering a
    row that failed to insert would suppress it from every future poll and lose
    it permanently.
    """

    threshold_seconds: int = 30
    _last: dict[tuple[date, str, int], tuple[datetime | None, int | None]] = field(
        default_factory=dict
    )

    def __len__(self) -> int:
        return len(self._last)

    def _differs(self, previous: tuple[datetime | None, int | None], row: PredictionRow) -> bool:
        previous_arrival, previous_delay = previous

        if row.arrival_time is not None and previous_arrival is not None:
            shift = abs((row.arrival_time - previous_arrival).total_seconds())
            return shift >= self.threshold_seconds

        # An arrival time appearing or disappearing is a real change regardless
        # of magnitude, so it is always written.
        if (row.arrival_time is None) != (previous_arrival is None):
            return True

        if row.delay_seconds is not None and previous_delay is not None:
            return abs(row.delay_seconds - previous_delay) >= self.threshold_seconds

        return row.delay_seconds != previous_delay

    def select_changed(self, rows: Iterable[PredictionRow]) -> list[PredictionRow]:
        """Rows worth writing: new keys, or a shift of at least the threshold."""
        changed = []
        for row in rows:
            previous = self._last.get(row.cache_key)
            if previous is None or self._differs(previous, row):
                changed.append(row)
        return changed

    def remember(self, rows: Iterable[PredictionRow]) -> None:
        for row in rows:
            self._last[row.cache_key] = (row.arrival_time, row.delay_seconds)

    def prune(self, before: date) -> int:
        """Drop entries for service days that have closed.

        Without this the cache grows for as long as the process runs, since a
        finished trip's key is never revisited. See ADR-0005.
        """
        stale = [key for key in self._last if key[0] < before]
        for key in stale:
            del self._last[key]
        return len(stale)
