"""Loading tracks for inference and storing the arrivals it produces.

Kept separate from ontime_sd/arrivals.py so the algorithm stays a set of pure
functions over plain data, testable without a database.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import date

import asyncpg

from ontime_sd.arrivals import (
    InferenceResult,
    Ping,
    ScheduledStop,
    ShapePoint,
    TripTrack,
    infer_arrivals,
)

log = logging.getLogger(__name__)

_PINGS_SQL = """
select ts, lat, lon
from vehicle_positions
where start_date = $1 and trip_id = $2 and vehicle_id = $3
  and lat is not null and lon is not null
order by ts
"""

_SHAPE_SQL = """
select s.shape_pt_lat, s.shape_pt_lon, s.shape_dist_traveled_m
from shapes s
join trips t
  on t.feed_version = s.feed_version and t.shape_id = s.shape_id
where s.feed_version = $1 and t.trip_id = $2
  and s.shape_dist_traveled_m is not null
order by s.shape_pt_sequence
"""

_STOPS_SQL = """
select stop_sequence, stop_id, shape_dist_traveled_m
from stop_times
where feed_version = $1 and trip_id = $2 and shape_dist_traveled_m is not null
order by stop_sequence
"""

# A trip served by two vehicles is a mid route swap, and mixing two buses' GPS
# would produce a track that teleports. The vehicle with the most fixes is taken
# as the one that ran it; the other is counted and skipped. Measured on real data:
# 204 of 23,271 trip records, plus one served by three.
_RUNS_SQL = """
select start_date, trip_id, vehicle_id, count(*) as pings
from vehicle_positions
where trip_id is not null and start_date = any($1::date[])
group by start_date, trip_id, vehicle_id
order by start_date, trip_id, pings desc
"""

_INSERT_ARRIVALS = """
insert into arrivals (
    start_date, trip_id, stop_sequence, feed_version, stop_id, vehicle_id,
    arrived_at, departed_at, method, ping_gap_seconds, nearest_ping_m, stop_offset_m
)
select * from unnest(
    $1::date[], $2::text[], $3::int[], $4::text[], $5::text[], $6::text[],
    $7::timestamptz[], $8::timestamptz[], $9::text[], $10::int[],
    $11::float8[], $12::float8[]
)
on conflict do nothing
"""

_INSERT_LOG = """
insert into inference_log (
    start_date, trip_id, vehicle_id, pings, stops_total, arrivals_written,
    stops_skipped, duration_ms, status, reason
)
values ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
"""


@dataclass(frozen=True, slots=True)
class TripRun:
    start_date: date
    trip_id: str
    vehicle_id: str
    pings: int


async def list_trip_runs(conn: asyncpg.Connection, days: list[date]) -> tuple[list[TripRun], int]:
    """Trips to process, one vehicle each. Returns the runs and how many were
    skipped as secondary vehicles on a shared trip.
    """
    rows = await conn.fetch(_RUNS_SQL, days)

    chosen: dict[tuple[date, str], TripRun] = {}
    skipped = 0
    for row in rows:
        key = (row["start_date"], row["trip_id"])
        run = TripRun(
            start_date=row["start_date"],
            trip_id=row["trip_id"],
            vehicle_id=row["vehicle_id"],
            pings=row["pings"],
        )
        if key in chosen:
            skipped += 1
            continue
        chosen[key] = run

    return list(chosen.values()), skipped


async def load_trip_track(conn: asyncpg.Connection, feed_version: str, run: TripRun) -> TripTrack:
    pings = await conn.fetch(_PINGS_SQL, run.start_date, run.trip_id, run.vehicle_id)
    shape = await conn.fetch(_SHAPE_SQL, feed_version, run.trip_id)
    stops = await conn.fetch(_STOPS_SQL, feed_version, run.trip_id)

    return TripTrack(
        start_date=run.start_date,
        trip_id=run.trip_id,
        vehicle_id=run.vehicle_id,
        feed_version=feed_version,
        pings=tuple(Ping(ts=r["ts"], lat=r["lat"], lon=r["lon"]) for r in pings),
        shape=tuple(
            ShapePoint(
                lat=r["shape_pt_lat"],
                lon=r["shape_pt_lon"],
                offset_m=r["shape_dist_traveled_m"],
            )
            for r in shape
        ),
        stops=tuple(
            ScheduledStop(
                stop_sequence=r["stop_sequence"],
                stop_id=r["stop_id"],
                offset_m=r["shape_dist_traveled_m"],
            )
            for r in stops
        ),
    )


async def store_result(
    conn: asyncpg.Connection,
    track: TripTrack,
    result: InferenceResult,
    duration_ms: int,
) -> int:
    """Write arrivals and the per trip log row. Idempotent."""
    written = 0
    if result.arrivals:
        rows = result.arrivals
        status = await conn.execute(
            _INSERT_ARRIVALS,
            [track.start_date] * len(rows),
            [track.trip_id] * len(rows),
            [r.stop_sequence for r in rows],
            [track.feed_version] * len(rows),
            [r.stop_id for r in rows],
            [track.vehicle_id] * len(rows),
            [r.arrived_at for r in rows],
            [r.departed_at for r in rows],
            [r.method for r in rows],
            [r.ping_gap_seconds for r in rows],
            [r.nearest_ping_m for r in rows],
            [r.stop_offset_m for r in rows],
        )
        try:
            written = int(status.split()[-1])
        except (ValueError, IndexError):
            written = 0

    await conn.execute(
        _INSERT_LOG,
        track.start_date,
        track.trip_id,
        track.vehicle_id,
        result.pings_used,
        len(track.stops),
        written,
        result.stops_skipped,
        duration_ms,
        result.status,
        result.reason,
    )
    return written


async def infer_for_days(
    pool: asyncpg.Pool, days: list[date], *, feed_version: str | None = None
) -> dict[str, int]:
    """Run inference over whole service days. Safe to re-run."""
    totals = {
        "trips": 0,
        "arrivals": 0,
        "skipped_stops": 0,
        "offroute_pings": 0,
        "clamped": 0,
        "secondary_vehicles_skipped": 0,
    }
    by_status: dict[str, int] = {}

    async with pool.acquire() as conn:
        if feed_version is None:
            feed_version = await conn.fetchval(
                "select feed_version from feed_versions where loaded_at is not null "
                "order by loaded_at desc limit 1"
            )
        if feed_version is None:
            raise RuntimeError("no loaded GTFS schedule, run `make load-gtfs` first")

        runs, secondary = await list_trip_runs(conn, days)
        totals["secondary_vehicles_skipped"] = secondary

    log.info("inferring arrivals", extra={"trips": len(runs), "days": len(days)})

    for run in runs:
        started = time.monotonic()
        async with pool.acquire() as conn:
            track = await load_trip_track(conn, feed_version, run)
            result = infer_arrivals(track)
            duration_ms = int((time.monotonic() - started) * 1000)
            written = await store_result(conn, track, result, duration_ms)

        totals["trips"] += 1
        totals["arrivals"] += written
        totals["skipped_stops"] += result.stops_skipped
        totals["offroute_pings"] += result.pings_offroute
        totals["clamped"] += result.clamped
        by_status[result.status] = by_status.get(result.status, 0) + 1

    log.info("inference complete", extra={**totals, "by_status": by_status})
    totals.update({f"status_{k}": v for k, v in by_status.items()})
    return totals


def main() -> None:
    """Entry point for `make infer-arrivals`."""
    import argparse
    import asyncio
    from datetime import timedelta

    from ontime_sd.config import Settings
    from ontime_sd.db import create_pool
    from ontime_sd.logging_setup import configure_logging

    parser = argparse.ArgumentParser(description="Infer arrivals from GPS traces")
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

    async def run() -> dict[str, int]:
        pool = await create_pool(settings)
        try:
            return await infer_for_days(pool, days)
        finally:
            await pool.close()

    totals = asyncio.run(run())
    print(f"processed {totals['trips']:,} trips over {len(days)} day(s)")
    for key, value in sorted(totals.items()):
        if key != "trips":
            print(f"  {key:<30} {value:>10,}")
