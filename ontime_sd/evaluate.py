"""Phase 4: scoring MTS's predictions against what actually happened.

This produces the number the project exists to state, and the baseline Phase 5 has
to beat.

The whole computation is one statement, deliberately. The data is already in
Postgres, the lookup is an index probe per (arrival, horizon), and expressing it as
SQL keeps the definition in one readable place rather than split between a query
and a Python loop that could drift from it. See DESIGN.md ADR-0037.
"""

from __future__ import annotations

import logging
from datetime import date

import asyncpg

log = logging.getLogger(__name__)

HORIZONS_MINUTES = (1, 5, 10, 20)

# The threshold separating a well observed arrival from an interpolated guess.
# Phase 3 measured a median ping gap of 195 seconds behind an arrival, with 24.6%
# over 10 minutes, so this matters to what the headline number means.
TIGHT_LABEL_SECONDS = 180

# A prediction MTS has not revised in this long is not meaningfully "the
# prediction in force": it is a leftover. Measured on real data, legitimate pairs
# have a median prediction age of 4.1 minutes and p95 of 23.4 minutes, so two
# hours is generous and excludes nothing real. Without this guard a residual
# service-day misalignment produced errors of exactly 24 hours, which destroyed
# the mean while leaving the median untouched. See ADR-0037.
MAX_PREDICTION_AGE_SECONDS = 7200

_EVALUATE_SQL = """
insert into prediction_errors (
    start_date, trip_id, stop_sequence, horizon_minutes,
    feed_version, stop_id, route_id,
    arrived_at, predicted_arrival, predicted_at,
    error_seconds, abs_error_seconds, ping_gap_seconds,
    service_minute, is_weekend, has_all_horizons
)
select
    paired.start_date,
    paired.trip_id,
    paired.stop_sequence,
    paired.horizon_minutes,
    paired.feed_version,
    paired.stop_id,
    paired.route_id,
    paired.arrived_at,
    paired.predicted_arrival,
    paired.predicted_at,
    paired.error_seconds,
    abs(paired.error_seconds),
    paired.ping_gap_seconds,
    paired.service_minute,
    paired.is_weekend,
    paired.horizons_found = %(horizon_count)s
from (
    select
        a.start_date,
        a.trip_id,
        a.stop_sequence,
        h.horizon_minutes,
        a.feed_version,
        a.stop_id,
        t.route_id,
        a.arrived_at,
        p.arrival_time  as predicted_arrival,
        p.observed_at   as predicted_at,
        a.ping_gap_seconds,

        -- Signed, so a systematic bias stays visible.
        round(extract(epoch from p.arrival_time - a.arrived_at))::int as error_seconds,

        -- Minutes past service-day midnight. Exceeds 1440 for an arrival after
        -- midnight on a trip that began the previous evening, which is correct:
        -- it belongs to the earlier service day.
        (extract(epoch from (a.arrived_at at time zone 'America/Los_Angeles')
                 - a.start_date::timestamp) / 60)::int as service_minute,
        extract(isodow from a.start_date) in (6, 7) as is_weekend,

        count(*) over (
            partition by a.start_date, a.trip_id, a.stop_sequence
        ) as horizons_found
    from arrivals a
    join trips t
      on t.feed_version = a.feed_version and t.trip_id = a.trip_id
    cross join unnest($2::int[]) as h(horizon_minutes)
    -- An inner lateral join, so an arrival with no qualifying prediction yields
    -- no row at all. Absence must be absence: a zero would drag the mean down and
    -- make MTS look better the less data we have.
    join lateral (
        select p.arrival_time, p.observed_at
        from predictions p
        where p.start_date = a.start_date
          and p.trip_id = a.trip_id
          and p.stop_sequence = a.stop_sequence
          and p.arrival_time is not null
          -- The most recent prediction at or before the cutoff is the one in
          -- force. A backward scan of the predictions primary key.
          and p.observed_at <= a.arrived_at - (h.horizon_minutes * interval '1 minute')
          -- Not a leftover from another run of the same trip id.
          and p.observed_at >= a.arrived_at - ($3::int * interval '1 second')
        order by p.observed_at desc
        limit 1
    ) p on true
    where a.start_date = any($1::date[])
) paired
on conflict do nothing
"""


async def evaluate_on_connection(
    conn: asyncpg.Connection,
    days: list[date],
    horizons: tuple[int, ...] = HORIZONS_MINUTES,
    max_prediction_age_seconds: int = MAX_PREDICTION_AGE_SECONDS,
) -> int:
    """Score every arrival on the given service days. Returns rows written.

    Idempotent: re-running after a Phase 3 re-run adds only what is missing.

    Takes a connection rather than a pool so the whole definition can be exercised
    inside a test transaction that rolls back.
    """
    statement = _EVALUATE_SQL % {"horizon_count": len(horizons)}
    status = await conn.execute(statement, days, list(horizons), max_prediction_age_seconds)

    try:
        written = int(status.split()[-1])
    except (ValueError, IndexError):
        written = 0

    log.info(
        "evaluation complete",
        extra={"days": len(days), "horizons": list(horizons), "rows_written": written},
    )
    return written


async def evaluate_days(
    pool: asyncpg.Pool,
    days: list[date],
    horizons: tuple[int, ...] = HORIZONS_MINUTES,
    max_prediction_age_seconds: int = MAX_PREDICTION_AGE_SECONDS,
) -> int:
    async with pool.acquire() as conn:
        return await evaluate_on_connection(conn, days, horizons, max_prediction_age_seconds)


async def headline(
    pool: asyncpg.Pool,
    *,
    comparable_only: bool = True,
    max_ping_gap_seconds: int | None = TIGHT_LABEL_SECONDS,
) -> list[dict[str, float]]:
    """MAE and p90 per horizon.

    By default this is the headline figure: the comparable subset, so the horizons
    describe one population, and well observed arrivals only, so it measures MTS
    rather than our own interpolation.
    """
    filters = []
    if comparable_only:
        filters.append("has_all_horizons")
    if max_ping_gap_seconds is not None:
        filters.append(f"ping_gap_seconds <= {int(max_ping_gap_seconds)}")
    where = ("where " + " and ".join(filters)) if filters else ""

    rows = await pool.fetch(
        f"""
        select horizon_minutes,
               count(*) as n,
               round(avg(abs_error_seconds))::int as mae_seconds,
               round(percentile_cont(0.9)
                     within group (order by abs_error_seconds))::int as p90_seconds,
               round(avg(error_seconds))::int as mean_signed_seconds
        from prediction_errors
        {where}
        group by horizon_minutes
        order by horizon_minutes
        """
    )
    return [dict(row) for row in rows]


def main() -> None:
    """Entry point for `make evaluate`."""
    import argparse
    import asyncio
    from datetime import timedelta

    from ontime_sd.config import Settings
    from ontime_sd.db import BATCH_COMMAND_TIMEOUT, create_pool
    from ontime_sd.logging_setup import configure_logging

    parser = argparse.ArgumentParser(description="Score MTS predictions against inferred arrivals")
    parser.add_argument("--from", dest="start", help="first service day, YYYY-MM-DD")
    parser.add_argument("--to", dest="end", help="last service day, YYYY-MM-DD")
    parser.add_argument("--days", type=int, default=1, help="days back from today")
    args = parser.parse_args()

    if args.start:
        first = date.fromisoformat(args.start)
        last = date.fromisoformat(args.end) if args.end else first
    else:
        last = date.today()
        first = last - timedelta(days=args.days - 1)

    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]

    settings = Settings.from_env()
    configure_logging(settings.log_level)

    async def run() -> tuple[int, list[dict[str, float]]]:
        pool = await create_pool(settings, command_timeout=BATCH_COMMAND_TIMEOUT)
        try:
            written = await evaluate_days(pool, days)
            return written, await headline(pool)
        finally:
            await pool.close()

    written, table = asyncio.run(run())
    print(f"scored {written:,} (arrival, horizon) pairs over {len(days)} day(s)")
    print()
    print("MTS prediction error, comparable subset, well observed arrivals only")
    print(f"{'horizon':>9}  {'N':>8}  {'MAE':>8}  {'p90':>8}  {'bias':>8}")
    for row in table:
        print(
            f"{row['horizon_minutes']:>7}m  {row['n']:>8,}  "
            f"{row['mae_seconds'] / 60:>7.2f}m  {row['p90_seconds'] / 60:>7.2f}m  "
            f"{row['mean_signed_seconds'] / 60:>+7.2f}m"
        )
