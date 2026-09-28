"""Resolving a feed stop_id to its stop_sequence within a trip.

The real MTS trip update feed never sends `stop_sequence`. It identifies each
predicted stop by `stop_id` alone, which GTFS-Realtime permits. But
`stop_sequence` is part of the `predictions` primary key, because it is what
Phase 4 needs to match a prediction to a specific arrival.

`stop_id` alone cannot substitute for it: measured against the real schedule,
3,350 trips (7.2%) visit the same stop twice. Trip 19261672 calls at stop 94031 at
both sequence 1 and sequence 4, so keying on stop_id would silently merge two
different arrivals into one row and make Phase 4 compare the wrong pair.

The feed does, however, list a trip's remaining stops in order. So the sequence can
be recovered by walking the feed's stop list against the trip's scheduled stop list
in order, never going backwards. See DESIGN.md ADR-0033.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import date

import asyncpg

from ontime_sd.feeds import PredictionRow

log = logging.getLogger(__name__)

# Trips kept in the lookup cache. The real feed carries about 450 active trips at
# once, so this holds a whole service period comfortably while staying bounded.
MAX_CACHED_TRIPS = 4000

_TRIP_STOPS_SQL = """
select stop_sequence, stop_id
from stop_times
where feed_version = $1 and trip_id = $2
order by stop_sequence
"""

_CURRENT_FEED_SQL = """
select feed_version
from feed_versions
where loaded_at is not null
  and ($1::date between feed_start_date and feed_end_date
       or feed_start_date is null or feed_end_date is null)
order by loaded_at desc
limit 1
"""


def align_stop_sequences(
    feed_stop_ids: Sequence[str], scheduled: Sequence[tuple[int, str]]
) -> list[int | None]:
    """Match feed stop ids to scheduled stop sequences, in order.

    Walks both lists forward together. A feed stop matches the next scheduled stop
    with the same id at or after the current position, so a repeated stop resolves
    to a different sequence on each visit.

    Returns one entry per feed stop, None where no match exists at or after the
    current position. Never reorders and never goes backwards, which is what makes
    a repeated stop unambiguous.

    >>> align_stop_sequences(["a", "b", "a"], [(1, "a"), (2, "b"), (3, "c"), (4, "a")])
    [1, 2, 4]
    """
    scheduled_ids = [stop_id for _, stop_id in scheduled]
    sequences = [sequence for sequence, _ in scheduled]

    resolved: list[int | None] = []
    position = 0

    for stop_id in feed_stop_ids:
        found: int | None = None
        probe = position
        while probe < len(scheduled_ids):
            if scheduled_ids[probe] == stop_id:
                found = sequences[probe]
                # Advance past the match so the next visit to the same stop
                # cannot resolve to this one again.
                position = probe + 1
                break
            probe += 1
        resolved.append(found)

    return resolved


class StopSequenceResolver:
    """Looks up a trip's scheduled stops, with an in memory cache.

    One database round trip per trip per process lifetime. The real feed repeats
    the same 450ish trips every 30 seconds, so without the cache this would be
    450 queries every poll for data that does not change within a service day.
    """

    def __init__(self, max_cached_trips: int = MAX_CACHED_TRIPS) -> None:
        self.max_cached_trips = max_cached_trips
        self._trips: dict[tuple[str, str], list[tuple[int, str]]] = {}
        self._feed_version: str | None = None
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._trips)

    def clear(self) -> None:
        self._trips.clear()
        self._feed_version = None

    async def current_feed_version(self, conn: asyncpg.Connection, service_day: date) -> str | None:
        """The loaded schedule covering this day, most recent first.

        Cached for the process lifetime. A new schedule loaded by the weekly agent
        is therefore not picked up until the collector restarts, which is an
        accepted tradeoff: schedules change monthly at most, and re-querying every
        poll to catch a monthly event is the wrong trade. Recorded in ADR-0033.
        """
        if self._feed_version is None:
            self._feed_version = await conn.fetchval(_CURRENT_FEED_SQL, service_day)
            if self._feed_version is None:
                log.warning(
                    "no loaded schedule covers this day, predictions cannot resolve",
                    extra={"service_day": service_day.isoformat()},
                )
        return self._feed_version

    async def scheduled_stops(
        self, conn: asyncpg.Connection, feed_version: str, trip_id: str
    ) -> list[tuple[int, str]]:
        key = (feed_version, trip_id)
        cached = self._trips.get(key)
        if cached is not None:
            self.hits += 1
            return cached

        self.misses += 1
        rows = await conn.fetch(_TRIP_STOPS_SQL, feed_version, trip_id)
        stops = [(row["stop_sequence"], row["stop_id"]) for row in rows]

        # Crude bound rather than a true LRU. Trips age out of the feed within a
        # service day, so the cache is naturally short lived and a full clear is
        # cheap compared to tracking recency on every hit.
        if len(self._trips) >= self.max_cached_trips:
            log.info("stop sequence cache full, clearing", extra={"entries": len(self._trips)})
            self._trips.clear()

        self._trips[key] = stops
        return stops


async def resolve_predictions(
    conn: asyncpg.Connection,
    resolver: StopSequenceResolver,
    rows: Sequence[PredictionRow],
) -> tuple[list[PredictionRow], dict[str, int]]:
    """Fill in stop_sequence for rows that arrived without one.

    Groups rows by trip, aligns each trip's ordered feed stops against its
    scheduled stops, and returns the rows that resolved plus a count of why any
    were dropped. Rows that already carry a stop_sequence pass through untouched,
    so a feed that does send it is unaffected.
    """
    from dataclasses import replace

    if not rows:
        return [], {}

    resolved: list[PredictionRow] = []
    dropped: dict[str, int] = {}

    def drop(reason: str, count: int = 1) -> None:
        dropped[reason] = dropped.get(reason, 0) + count

    already_keyed = [row for row in rows if row.resolved]
    resolved.extend(already_keyed)

    needs_lookup = [row for row in rows if not row.resolved]
    if not needs_lookup:
        return resolved, dropped

    feed_version = await resolver.current_feed_version(conn, needs_lookup[0].start_date)
    if feed_version is None:
        # No schedule loaded for this day, so nothing can be resolved. Counted
        # rather than silently discarded, because this is an operator problem.
        drop("no_schedule_loaded", len(needs_lookup))
        return resolved, dropped

    by_trip: dict[tuple[date, str], list[PredictionRow]] = {}
    for row in needs_lookup:
        by_trip.setdefault((row.start_date, row.trip_id), []).append(row)

    for (_, trip_id), trip_rows in by_trip.items():
        trip_rows.sort(key=lambda row: row.feed_order)
        scheduled = await resolver.scheduled_stops(conn, feed_version, trip_id)

        if not scheduled:
            # The feed is reporting a trip the loaded schedule does not contain,
            # for example an added trip. Nothing to align against.
            drop("trip_not_in_schedule", len(trip_rows))
            continue

        sequences = align_stop_sequences([row.stop_id or "" for row in trip_rows], scheduled)
        for row, sequence in zip(trip_rows, sequences, strict=True):
            if sequence is None:
                drop("stop_not_on_trip")
                continue
            resolved.append(replace(row, stop_sequence=sequence))

    return resolved, dropped
