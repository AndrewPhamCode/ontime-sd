"""Historical travel time between consecutive stops.

This is the charter's Phase 5 baseline and also the source of several model
features. Both uses carry the same hazard: a "historical mean" computed over data
that includes the evaluation period has already seen the future, so the result
would be quietly wrong in the flattering direction.

The guard is structural rather than procedural. `fit_through_date` is part of the
`segment_stats` primary key, so every stat carries the last day of data that
produced it, and the evaluation path asserts that date precedes the test window.
See DESIGN.md ADR-0038.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

import asyncpg

log = logging.getLogger(__name__)

# A segment duration is only as good as the two arrivals bounding it. Both ends
# must be well observed, otherwise the "travel time" is mostly interpolation
# error. Phase 3 measured 24.6% of arrivals sitting behind ping gaps over 10
# minutes, so this filter matters.
MAX_ENDPOINT_PING_GAP_SECONDS = 180

# Durations outside this range are not plausible stop-to-stop travel for a bus and
# indicate a bad arrival pair rather than slow traffic.
MIN_SEGMENT_SECONDS = 1
MAX_SEGMENT_SECONDS = 1800

_FIT_SQL = """
insert into segment_stats (
    feed_version, from_stop_id, to_stop_id, hour_bin, is_weekend,
    fit_through_date, n, mean_seconds, median_seconds, stddev_seconds
)
select
    feed_version,
    from_stop_id,
    to_stop_id,
    hour_bin,
    is_weekend,
    $2::date as fit_through_date,
    count(*)                                                      as n,
    avg(seconds)                                                  as mean_seconds,
    percentile_cont(0.5) within group (order by seconds)          as median_seconds,
    stddev_samp(seconds)                                          as stddev_seconds
from (
    select
        a.feed_version,
        a.stop_id                                                 as from_stop_id,
        lead(a.stop_id)       over w                              as to_stop_id,
        extract(epoch from lead(a.arrived_at) over w - a.arrived_at) as seconds,
        -- Binned by the hour the vehicle left the first stop, in service time, so
        -- it may exceed 23 for a trip running past midnight.
        ((extract(epoch from (a.arrived_at at time zone 'America/Los_Angeles')
                  - a.start_date::timestamp) / 3600)::int)        as hour_bin,
        extract(isodow from a.start_date) in (6, 7)               as is_weekend,
        a.ping_gap_seconds                                        as from_gap,
        lead(a.ping_gap_seconds) over w                           as to_gap
    from arrivals a
    where a.feed_version = $1
      -- The train-only boundary. Nothing after this date may influence a stat.
      and a.start_date <= $2::date
    window w as (partition by a.start_date, a.trip_id order by a.stop_sequence)
) pairs
where to_stop_id is not null
  and seconds between $3 and $4
  and from_gap <= $5
  and to_gap <= $5
  and hour_bin between 0 and 29
group by feed_version, from_stop_id, to_stop_id, hour_bin, is_weekend
on conflict do nothing
"""


async def fit_segment_stats(
    conn: asyncpg.Connection,
    feed_version: str,
    fit_through: date,
    *,
    max_endpoint_ping_gap: int = MAX_ENDPOINT_PING_GAP_SECONDS,
) -> int:
    """Compute segment travel time statistics from data up to and including a date.

    Returns the number of stat rows written. Idempotent for a given
    (feed_version, fit_through).
    """
    status = await conn.execute(
        _FIT_SQL,
        feed_version,
        fit_through,
        MIN_SEGMENT_SECONDS,
        MAX_SEGMENT_SECONDS,
        max_endpoint_ping_gap,
    )
    try:
        written = int(status.split()[-1])
    except (ValueError, IndexError):
        written = 0

    log.info(
        "fitted segment stats",
        extra={
            "feed_version": feed_version[:12],
            "fit_through": fit_through.isoformat(),
            "rows": written,
        },
    )
    return written


# Fallback levels, in order of preference. Reported alongside each lookup so the
# share of predictions resting on a coarse estimate is measurable rather than
# assumed.
LEVEL_EXACT = "exact"
LEVEL_SEGMENT = "segment"
LEVEL_GLOBAL = "global"


@dataclass(slots=True)
class SegmentMeans:
    """Loaded statistics with their fallback chain.

    A thin segment should not silently inherit a wildly different estimate, so the
    chain is deliberately short: the same segment at the same hour and day type,
    then the same segment at any time, then the global mean. There is no route
    level fallback, because two segments on one route can differ by an order of
    magnitude and averaging them would be worse than the global figure.
    """

    fit_through: date
    exact: dict[tuple[str, str, int, bool], float] = field(default_factory=dict)
    by_segment: dict[tuple[str, str], float] = field(default_factory=dict)
    global_mean: float = 60.0

    def __len__(self) -> int:
        return len(self.exact)

    def lookup(
        self, from_stop: str, to_stop: str, hour_bin: int, is_weekend: bool
    ) -> tuple[float, str]:
        value = self.exact.get((from_stop, to_stop, hour_bin, is_weekend))
        if value is not None:
            return value, LEVEL_EXACT

        value = self.by_segment.get((from_stop, to_stop))
        if value is not None:
            return value, LEVEL_SEGMENT

        return self.global_mean, LEVEL_GLOBAL


async def load_segment_means(
    conn: asyncpg.Connection, feed_version: str, fit_through: date
) -> SegmentMeans:
    """Load one fit into memory, with its fallback aggregates.

    Fallbacks are derived from the same rows, so they inherit the same
    train-only boundary rather than being computed separately and risking a leak.
    """
    rows = await conn.fetch(
        """
        select from_stop_id, to_stop_id, hour_bin, is_weekend, n, mean_seconds
        from segment_stats
        where feed_version = $1 and fit_through_date = $2
        """,
        feed_version,
        fit_through,
    )

    means = SegmentMeans(fit_through=fit_through)
    weighted: dict[tuple[str, str], tuple[float, int]] = {}
    total_seconds = 0.0
    total_n = 0

    for row in rows:
        key = (row["from_stop_id"], row["to_stop_id"], row["hour_bin"], row["is_weekend"])
        means.exact[key] = row["mean_seconds"]

        segment = (row["from_stop_id"], row["to_stop_id"])
        accumulated, count = weighted.get(segment, (0.0, 0))
        weighted[segment] = (
            accumulated + row["mean_seconds"] * row["n"],
            count + row["n"],
        )

        total_seconds += row["mean_seconds"] * row["n"]
        total_n += row["n"]

    means.by_segment = {
        segment: total / count for segment, (total, count) in weighted.items() if count
    }
    if total_n:
        means.global_mean = total_seconds / total_n

    log.info(
        "loaded segment means",
        extra={
            "fit_through": fit_through.isoformat(),
            "exact": len(means.exact),
            "segments": len(means.by_segment),
            "global_mean_seconds": round(means.global_mean, 1),
        },
    )
    return means
