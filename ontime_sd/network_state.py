"""Current running conditions per route, as known at a given instant.

The model's own-vehicle information is stale by construction. Its anchor is the
last stop whose interpolation window had closed before the cutoff, which at the
10 minute horizon is a median of 150 seconds old and 11 stops back. MTS does
better than that because it can see the vehicle's live position. This module
supplies the only comparable freshness available without touching GPS: what the
*rest of the fleet* on the same route was observed doing just before the cutoff.

Why this is not a leak, which is the first question it should face:

  - Every observation is gated on `arrived_at + ping_gap_seconds <= bucket_start`,
    the same window-closure test ADR-0038 settled on, so nothing enters a bucket
    that was not knowable at its start.
  - A bucket is therefore an *online* quantity rather than a fitted statistic.
    Unlike the segment means in ontime_sd/segments.py it is safe to compute over
    the test window, because each bucket reads only observations that preceded it.
    There is no `fit_through` to check, and adding one would be meaningless here.
  - The target's own arrival cannot appear in its own bucket. The bucket ends at
    the cutoff and the target arrives `horizon` minutes after it, so its window
    closes strictly later. The same argument excludes every later stop on the
    trip, since those arrive after the target.

Deliberately NOT included: the elapsed time since the anchor. It is available at
the cutoff and would help in production, but this evaluation defines the cutoff as
`arrived_at - horizon`, which makes elapsed time equal to `label - horizon` and
turns it into a restatement of the answer. See DESIGN.md ADR-0044.
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import asyncpg

log = logging.getLogger(__name__)

# Five minute buckets. Finer buckets cost memory for precision the signal does not
# have, since a route only produces a handful of arrivals per minute.
BUCKET_SECONDS = 300

# How far back a bucket looks. Thirty minutes is long enough that a quiet route
# still has observations and short enough to describe now rather than today.
WINDOW_SECONDS = 1800


@dataclass(slots=True)
class RouteConditions:
    """Mean observed delay per (route, bucket), with the sample size behind it.

    `lookup` returns NaN-free values plus a count, so a caller can tell "the route
    is running two minutes late" apart from "nothing was observed", which are very
    different inputs to a model and must not collapse to the same number.
    """

    bucket_seconds: int = BUCKET_SECONDS
    window_seconds: int = WINDOW_SECONDS
    # (route_id, bucket_index) -> (mean delay seconds, observation count)
    _buckets: dict[tuple[str, int], tuple[float, int]] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self._buckets)

    def _index(self, instant: datetime) -> int:
        return int(instant.timestamp()) // self.bucket_seconds

    def lookup(self, route_id: str | None, cutoff: datetime) -> tuple[float, int]:
        """Conditions on `route_id` as of `cutoff`.

        Falls back to the bucket before the one containing the cutoff when that
        bucket is empty, because a route with a five minute gap in arrivals is
        common and the previous bucket is still a better estimate than nothing.
        """
        if route_id is None:
            return 0.0, 0
        index = self._index(cutoff)
        for candidate in (index, index - 1):
            found = self._buckets.get((route_id, candidate))
            if found is not None:
                return found
        return 0.0, 0


async def load_observations(
    conn: asyncpg.Connection,
    first_day: date,
    last_day: date,
    *,
    max_ping_gap: int = 180,
) -> list[asyncpg.Record]:
    """Every well observed arrival in the range, with its delay and the instant it
    became knowable.

    `known_at` is the arrival plus its ping gap: the interpolation that produced
    the arrival time needed the later of the two bracketing pings, so the value
    does not exist until then.
    """
    return await conn.fetch(
        """
        select
            t.route_id,
            a.arrived_at,
            a.arrived_at + (coalesce(a.ping_gap_seconds, 0) * interval '1 second')
                as known_at,
            extract(epoch from (
                a.arrived_at at time zone 'America/Los_Angeles'
                - a.start_date::timestamp
            )) - st.arrival_seconds as delay_seconds
        from arrivals a
        join trips t
          on t.feed_version = a.feed_version and t.trip_id = a.trip_id
        join stop_times st
          on st.feed_version = a.feed_version and st.trip_id = a.trip_id
         and st.stop_sequence = a.stop_sequence
        where a.start_date between $1 and $2
          and a.ping_gap_seconds is not null
          and a.ping_gap_seconds <= $3
          and st.arrival_seconds is not null
        order by t.route_id, known_at
        """,
        first_day,
        last_day,
        max_ping_gap,
    )


def build_conditions(
    rows: list[asyncpg.Record],
    *,
    bucket_seconds: int = BUCKET_SECONDS,
    window_seconds: int = WINDOW_SECONDS,
) -> RouteConditions:
    """Precompute every bucket a lookup might ask for.

    Done as one sweep per route rather than a correlated subquery per prediction
    row. There are 1.8M training rows and roughly 150k buckets, so computing the
    window once per bucket instead of once per row is the difference between
    seconds and hours.
    """
    conditions = RouteConditions(bucket_seconds=bucket_seconds, window_seconds=window_seconds)

    by_route: dict[str, list[tuple[float, float]]] = defaultdict(list)
    for row in rows:
        delay = row["delay_seconds"]
        if delay is None:
            continue
        by_route[row["route_id"]].append((row["known_at"].timestamp(), float(delay)))

    for route_id, observations in by_route.items():
        observations.sort()
        known = [instant for instant, _ in observations]
        delays = [delay for _, delay in observations]

        # Prefix sums turn each bucket's mean into two lookups and a subtraction.
        running = [0.0]
        for delay in delays:
            running.append(running[-1] + delay)

        first_bucket = int(known[0]) // bucket_seconds
        last_bucket = int(known[-1]) // bucket_seconds + 1
        for bucket in range(first_bucket, last_bucket + 1):
            start = bucket * bucket_seconds
            # Strictly before the bucket starts, so every observation in it was
            # knowable at any cutoff inside the bucket.
            high = bisect_right(known, float(start))
            low = bisect_right(known, float(start - window_seconds))
            count = high - low
            if count <= 0:
                continue
            total = running[high] - running[low]
            conditions._buckets[(route_id, bucket)] = (total / count, count)

    log.info(
        "route conditions built",
        extra={"routes": len(by_route), "buckets": len(conditions)},
    )
    return conditions


async def load_route_conditions(
    conn: asyncpg.Connection,
    first_day: date,
    last_day: date,
    *,
    max_ping_gap: int = 180,
) -> RouteConditions:
    """Build conditions covering the range, padded so early cutoffs have a window.

    The pad matters: without it the first half hour of each day has no prior
    observations and every lookup there returns the empty fallback.
    """
    rows = await load_observations(
        conn,
        first_day - timedelta(days=1),
        last_day,
        max_ping_gap=max_ping_gap,
    )
    return build_conditions(rows)
