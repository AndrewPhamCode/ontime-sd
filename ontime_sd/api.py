"""Phase 6: a read-only API over the results.

Every endpoint is shaped for exactly one panel, so the frontend renders what it is
given and does no aggregation of its own. That keeps the numbers on screen
identical to the numbers `make compare` prints, which is the property that matters:
if the page and the SQL disagree, the page is wrong.

One deliberate constraint runs through this file. The comparison is only valid over
the window the model was tested on, because MTS has predictions for every day while
our predictors only have the test days. Comparing MTS over five days against the
model over two would flatter or damn either one at random, so the default window is
read from the most recent `model_runs` row rather than being "all data".
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from typing import Any

import asyncpg
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

from ontime_sd.config import SERVICE_TZ, Settings
from ontime_sd.db import create_pool
from ontime_sd.evaluate import TIGHT_LABEL_SECONDS

log = logging.getLogger(__name__)

# Aggregates run over millions of rows and only change when the pipeline runs, so
# a short cache turns a 500 ms page load into an instant one without any risk of
# showing meaningfully stale numbers.
CACHE_TTL_SECONDS = 60.0

SOURCES = ("mts", "persist_delay", "segment_mean", "lgbm")

# Time of day bands, in minutes past the service day's midnight.
#
# The charter asks for error broken down by time of day, because a prediction
# problem at 17:30 is a different problem from one at 11:00 and an all-day average
# hides both. `prediction_errors.service_minute` already stores the key, so this is
# a filter rather than new data.
#
# Bands are **exhaustive and non-overlapping**, which is not decoration: it means
# the per-band row counts sum to the all-day count, and a test asserts it. A
# visitor who sees four bands that do not add up has no reason to trust any of the
# numbers on the page. `late` is the awkward one and the reason the property needs
# checking: GTFS times run past 24:00:00 for trips that cross midnight, so service
# minute 1500 is 01:00 on the following calendar day and belongs with the small
# hours, not after the evening.
TIME_BANDS: dict[str, tuple[int, int] | None] = {
    "all": None,
    "am_rush": (360, 540),
    "midday": (540, 900),
    "pm_rush": (900, 1140),
    "evening": (1140, 1440),
    "late": (1440, 360),
}


def validate_time_band(band: str) -> str:
    """Reject an unknown band instead of silently returning every row.

    Falling back to "all" would be worse than a 422: the page would show the
    unfiltered number under a band's label, which is a wrong answer presented
    confidently. Same reasoning as validate_source.
    """
    if band not in TIME_BANDS:
        raise HTTPException(
            status_code=422,
            detail=f"unknown time band {band!r}, expected one of {', '.join(TIME_BANDS)}",
        )
    return band


def time_band_clause(band: str, param_index: int) -> tuple[str, list[int]]:
    """SQL fragment and parameters restricting rows to a band.

    Returns an empty fragment for "all" so the common case adds nothing to the
    query. `late` wraps around midnight, so it is the one band expressed as a
    disjunction rather than a range.
    """
    bounds = TIME_BANDS[band]
    if bounds is None:
        return "", []
    low, high = bounds
    if low < high:
        return f" and service_minute >= ${param_index} and service_minute < ${param_index + 1}", [
            low,
            high,
        ]
    # Wraps: on or after `low`, or before `high`.
    return f" and (service_minute >= ${param_index} or service_minute < ${param_index + 1})", [
        low,
        high,
    ]


class _TtlCache:
    """Tiny in-process cache. Not shared between workers, which is fine for one."""

    def __init__(self, ttl: float = CACHE_TTL_SECONDS) -> None:
        self.ttl = ttl
        self._entries: dict[str, tuple[float, Any]] = {}

    async def get(self, key: str, produce: Callable[[], Any]) -> Any:
        now = time.monotonic()
        hit = self._entries.get(key)
        if hit is not None and now - hit[0] < self.ttl:
            return hit[1]
        value = await produce()
        self._entries[key] = (now, value)
        return value

    def clear(self) -> None:
        self._entries.clear()


# --- response models ---------------------------------------------------------


class Window(BaseModel):
    """The evaluation window every comparison is computed over."""

    test_from: date
    test_to: date
    train_from: date | None = None
    train_to: date | None = None
    source_of_window: str = Field(
        description="Where the window came from: the model run, or a fallback."
    )


class SourceHorizon(BaseModel):
    source: str
    horizon_minutes: int
    n: int
    mae_seconds: float
    median_seconds: float
    p90_seconds: float
    bias_seconds: float


class Headline(BaseModel):
    window: Window
    rows: list[SourceHorizon]
    label_filter_seconds: int = Field(
        description="Arrivals with a wider ping gap than this are excluded."
    )
    time_band: str = Field(
        default="all",
        description="Time of day band these figures cover. Echoed back so the page "
        "can state which slice it is showing rather than implying all day.",
    )


class RouteComparison(BaseModel):
    route_id: str
    route_name: str | None
    n: int
    mts_mae_seconds: float
    model_mae_seconds: float | None
    improvement_seconds: float | None


class DistributionBucket(BaseModel):
    source: str
    upper_bound_seconds: int
    n: int


class CoverageDay(BaseModel):
    day: date
    successful_polls: int
    hours_lost: float
    # Null when the day holds a single poll, because coverage is measured against
    # the span from the first poll to the last and that span is then zero. The
    # query's nullif guards the division; this makes the resulting null a value
    # the model accepts rather than a 500.
    #
    # Deliberately not coalesced to 100. One poll in a day is not full coverage,
    # and saying so would be a lie on the one panel whose whole job is to admit
    # what the data does not support.
    coverage_pct: float | None = None


class LabelBand(BaseModel):
    band: str
    n: int
    pct: float


class DataQuality(BaseModel):
    coverage: list[CoverageDay]
    label_bands: list[LabelBand]
    arrivals_total: int
    caveats: list[str]


class ModelRun(BaseModel):
    trained_at: datetime
    train_from: date
    train_to: date
    test_from: date
    test_to: date
    train_rows: int | None
    test_rows: int | None
    features: list[str] | None
    notes: str | None


class Stop(BaseModel):
    stop_id: str
    stop_name: str | None
    lat: float
    lon: float
    arrivals: int


class StopArrival(BaseModel):
    trip_id: str
    route_id: str | None
    stop_sequence: int
    arrived_at: datetime
    scheduled_at: datetime | None
    horizon_minutes: int
    mts_predicted: datetime | None
    model_predicted: datetime | None
    mts_error_seconds: int | None
    model_error_seconds: int | None


class StopDetail(BaseModel):
    stop_id: str
    stop_name: str | None
    recent: list[StopArrival]


class Vehicle(BaseModel):
    vehicle_id: str
    route_id: str | None
    route_short_name: str | None
    # GTFS route_type: 0 is a trolley, 3 is a bus. Drawn differently.
    route_type: int | None
    trip_id: str | None
    lat: float
    lon: float
    ts: datetime


class UpcomingArrival(BaseModel):
    """One service due at a stop, with MTS's estimate and our corrected one."""

    trip_id: str
    route_id: str | None
    route_short_name: str | None
    route_type: int | None
    headsign: str | None
    stop_sequence: int

    mts_arrival: datetime
    predicted_at: datetime

    corrected_arrival: datetime | None
    correction_seconds: float | None
    # Which level of the fallback chain produced the correction, and how many
    # observations back it. Shown to the rider so a thin estimate is identifiable.
    correction_basis: str
    correction_sample: int | None


class StopUpcoming(BaseModel):
    stop_id: str
    stop_name: str | None
    lat: float | None
    lon: float | None
    arrivals: list[UpcomingArrival]
    as_of: datetime


class StopSearchResult(BaseModel):
    stop_id: str
    stop_name: str | None
    lat: float
    lon: float
    arrivals: int


class ShapePoint(BaseModel):
    lat: float
    lon: float


class RouteShape(BaseModel):
    route_id: str
    points: list[ShapePoint]


# --- the ETA correction ------------------------------------------------------
#
# MTS's live prediction is adjusted by the bias we measured for that stop and
# route in Phase 4: corrected = predicted - mean(predicted - actual). MTS runs
# optimistic, so the bias is usually negative and subtracting it pushes the
# estimate later, which is the direction a rider needs.
#
# This is a bias correction derived from batch statistics, NOT live model
# inference. Phase 5's model is at parity on mean absolute error, and the app says
# so rather than implying a live model.

# The horizon the bias is measured at. Ten minutes is the middle of the range and
# the lead time a rider actually plans around.
BIAS_HORIZON_MINUTES = 10

# A correction needs enough observations to be worth more than nothing. Below
# these, fall back a level rather than correcting on noise.
MIN_STOP_ROUTE_SAMPLE = 10
MIN_ROUTE_SAMPLE = 30

BASIS_STOP_ROUTE = "stop_and_route"
BASIS_ROUTE = "route"
BASIS_NONE = "none"


def choose_bias(
    stop_route_n: int | None,
    stop_route_bias: float | None,
    route_n: int | None,
    route_bias: float | None,
    min_stop_route: int = MIN_STOP_ROUTE_SAMPLE,
    min_route: int = MIN_ROUTE_SAMPLE,
) -> tuple[float | None, str, int | None]:
    """Pick the most specific bias with enough evidence behind it.

    Returns the bias in seconds, which level produced it, and the sample size.
    When nothing qualifies the answer is None, and the caller shows MTS unchanged
    rather than inventing a correction.
    """
    if stop_route_bias is not None and stop_route_n is not None and stop_route_n >= min_stop_route:
        return stop_route_bias, BASIS_STOP_ROUTE, stop_route_n
    if route_bias is not None and route_n is not None and route_n >= min_route:
        return route_bias, BASIS_ROUTE, route_n
    return None, BASIS_NONE, None


def validate_source(source: str) -> str:
    """Reject an unknown predictor loudly.

    Returning an empty column instead would read on screen as "the model has no
    data" rather than "you asked for something that does not exist".
    """
    if source not in SOURCES:
        raise HTTPException(
            422, f"unknown predictor {source!r}, expected one of {', '.join(SOURCES)}"
        )
    return source


# --- app ---------------------------------------------------------------------


def create_app(settings: Settings | None = None, pool: asyncpg.Pool | None = None) -> FastAPI:
    """Build the app. An existing pool can be injected, which tests use."""
    resolved = settings or Settings.from_env()
    state: dict[str, Any] = {"pool": pool, "cache": _TtlCache()}

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        owns_pool = state["pool"] is None
        if owns_pool:
            state["pool"] = await create_pool(resolved)
        try:
            yield
        finally:
            if owns_pool and state["pool"] is not None:
                await state["pool"].close()

    app = FastAPI(
        title="OnTime SD",
        description=(
            "Read-only API over the San Diego MTS arrival prediction results. "
            "Every comparison is computed over the window the model was tested on."
        ),
        version="0.1.0",
        lifespan=lifespan,
    )

    def db() -> asyncpg.Pool:
        if state["pool"] is None:
            raise HTTPException(503, "database pool not ready")
        return state["pool"]

    cache: _TtlCache = state["cache"]

    async def _load_bias(horizon: int, max_ping_gap: int) -> dict[str, dict]:
        """Measured bias per stop-and-route and per route, in seconds.

        One aggregate rather than a query per arrival. Only changes when the
        pipeline runs, so it sits behind the same TTL cache as everything else.
        """
        pair_rows = await db().fetch(
            """
            select stop_id, route_id, count(*) as n, avg(error_seconds)::float8 as bias
            from prediction_errors
            where source = 'mts' and horizon_minutes = $1 and ping_gap_seconds <= $2
              and route_id is not null
            group by stop_id, route_id
            """,
            horizon,
            max_ping_gap,
        )
        route_rows = await db().fetch(
            """
            select route_id, count(*) as n, avg(error_seconds)::float8 as bias
            from prediction_errors
            where source = 'mts' and horizon_minutes = $1 and ping_gap_seconds <= $2
              and route_id is not null
            group by route_id
            """,
            horizon,
            max_ping_gap,
        )
        return {
            "stop_route": {
                (row["stop_id"], row["route_id"]): (row["n"], row["bias"]) for row in pair_rows
            },
            "route": {row["route_id"]: (row["n"], row["bias"]) for row in route_rows},
        }

    async def resolve_window() -> Window:
        """The window every comparison uses.

        Taken from the newest model run, because that is the only period where all
        four sources have predictions. Falling back to the full range of scored
        data would compare MTS over five days against the model over two.
        """
        row = await db().fetchrow(
            "select train_from, train_to, test_from, test_to from model_runs "
            "order by trained_at desc limit 1"
        )
        if row is not None:
            return Window(
                test_from=row["test_from"],
                test_to=row["test_to"],
                train_from=row["train_from"],
                train_to=row["train_to"],
                source_of_window="latest model run",
            )

        span = await db().fetchrow(
            "select min(start_date) as lo, max(start_date) as hi from prediction_errors"
        )
        if span is None or span["lo"] is None:
            today = date.today()
            return Window(test_from=today, test_to=today, source_of_window="no data")
        return Window(
            test_from=span["lo"],
            test_to=span["hi"],
            source_of_window="all scored data, no model run found",
        )

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        try:
            await db().fetchval("select 1")
        except (asyncpg.PostgresError, OSError, HTTPException) as exc:
            raise HTTPException(503, f"database unavailable: {exc}") from exc
        return {"status": "ok"}

    @app.get("/api/window", response_model=Window)
    async def window() -> Window:
        return await cache.get("window", resolve_window)

    @app.get("/api/headline", response_model=Headline)
    async def headline(
        max_ping_gap: int = Query(
            TIGHT_LABEL_SECONDS,
            ge=30,
            le=3600,
            description="Widest GPS gap behind an arrival that still counts",
        ),
        time_band: str = Query("all", description="Time of day band, see TIME_BANDS"),
    ) -> Headline:
        validate_time_band(time_band)

        async def produce() -> Headline:
            win = await resolve_window()
            band_sql, band_args = time_band_clause(time_band, 4)
            # Matched population, for the reason ADR-0043 records: averaging each
            # source over its own rows is not a comparison. Our predictors only
            # score where an anchor existed, and the rows they decline are the
            # hard ones, so MTS gets charged for cases they never attempted.
            #
            # `present` makes this degrade correctly rather than needing a
            # special case. With only MTS loaded, one source is present, every
            # key has one source, and the restriction is a no-op. With all four
            # loaded it becomes the full intersection. Hardcoding 4 would have
            # emptied the page whenever a model had not been run yet, which is
            # exactly the state a fresh deployment is in.
            rows = await db().fetch(
                f"""
                with win as (
                    select * from prediction_errors
                    where has_all_horizons
                      and ping_gap_seconds <= $3
                      and start_date between $1 and $2
                ),
                present as (select count(distinct source) as sources from win),
                matched as (
                    select start_date, trip_id, stop_sequence, horizon_minutes
                    from win
                    group by 1, 2, 3, 4
                    having count(distinct source) = (select sources from present)
                ),
                scoped as (
                    select * from win
                    join matched using (start_date, trip_id, stop_sequence, horizon_minutes)
                    where true {band_sql}
                )
                select source, horizon_minutes,
                       count(*)                                        as n,
                       avg(abs_error_seconds)::float8                  as mae_seconds,
                       (percentile_cont(0.5) within group
                        (order by abs_error_seconds))::float8           as median_seconds,
                       (percentile_cont(0.9) within group
                        (order by abs_error_seconds))::float8           as p90_seconds,
                       avg(error_seconds)::float8                      as bias_seconds
                from scoped
                group by source, horizon_minutes
                order by source, horizon_minutes
                """,
                win.test_from,
                win.test_to,
                max_ping_gap,
                *band_args,
            )
            return Headline(
                window=win,
                rows=[SourceHorizon(**dict(row)) for row in rows],
                label_filter_seconds=max_ping_gap,
                time_band=time_band,
            )

        # The key carries every parameter that changes the result. Without that,
        # moving a setting would return the previous setting's numbers for up to
        # a minute with nothing on screen saying so.
        return await cache.get(f"headline:{max_ping_gap}:{time_band}", produce)

    @app.get("/api/routes", response_model=list[RouteComparison])
    async def routes(
        horizon: int = Query(10, description="Horizon in minutes"),
        min_n: int = Query(100, description="Minimum pairs for a route to appear"),
        max_ping_gap: int = Query(TIGHT_LABEL_SECONDS, ge=30, le=3600),
        compare: str = Query("lgbm", description="Predictor to compare MTS against"),
        time_band: str = Query("all", description="Time of day band, see TIME_BANDS"),
    ) -> list[RouteComparison]:
        validate_source(compare)
        validate_time_band(time_band)

        async def produce() -> list[RouteComparison]:
            win = await resolve_window()
            band_sql, band_args = time_band_clause(time_band, 7)
            # Matched population, same reasoning as the headline and ADR-0043.
            # Per route this matters more, not less: a route where the model has
            # an anchor for only half the arrivals would otherwise be compared
            # against MTS scored on all of them, and the per route table is
            # exactly where someone looks to find where the model wins.
            rows = await db().fetch(
                f"""
                with win as (
                    select * from prediction_errors
                    where horizon_minutes = $3
                      and has_all_horizons
                      and ping_gap_seconds <= $4
                      and start_date between $1 and $2
                      and route_id is not null
                ),
                present as (select count(distinct source) as sources from win),
                matched as (
                    select start_date, trip_id, stop_sequence, horizon_minutes
                    from win
                    group by 1, 2, 3, 4
                    having count(distinct source) = (select sources from present)
                ),
                scoped as (
                    select * from win
                    join matched using (start_date, trip_id, stop_sequence, horizon_minutes)
                    where true {band_sql}
                )
                select pe.route_id,
                       max(r.route_long_name)                             as route_name,
                       count(*) filter (where pe.source = 'mts')          as n,
                       (avg(pe.abs_error_seconds)
                        filter (where pe.source = 'mts'))::float8         as mts_mae_seconds,
                       (avg(pe.abs_error_seconds)
                        filter (where pe.source = $6))::float8            as model_mae_seconds
                from scoped pe
                left join routes r
                       on r.feed_version = pe.feed_version and r.route_id = pe.route_id
                group by pe.route_id
                having count(*) filter (where pe.source = 'mts') >= $5
                order by (avg(pe.abs_error_seconds) filter (where pe.source = 'mts')) desc
                """,
                win.test_from,
                win.test_to,
                horizon,
                max_ping_gap,
                min_n,
                compare,
                *band_args,
            )
            out = []
            for row in rows:
                mts = row["mts_mae_seconds"]
                model = row["model_mae_seconds"]
                out.append(
                    RouteComparison(
                        route_id=row["route_id"],
                        route_name=row["route_name"],
                        n=row["n"],
                        mts_mae_seconds=mts,
                        model_mae_seconds=model,
                        improvement_seconds=(mts - model) if model is not None else None,
                    )
                )
            return out

        return await cache.get(
            f"routes:{horizon}:{min_n}:{max_ping_gap}:{compare}:{time_band}", produce
        )

    @app.get("/api/error-distribution", response_model=list[DistributionBucket])
    async def error_distribution(
        horizon: int = Query(10, description="Horizon in minutes"),
        max_ping_gap: int = Query(TIGHT_LABEL_SECONDS, ge=30, le=3600),
        time_band: str = Query("all", description="Time of day band, see TIME_BANDS"),
    ) -> list[DistributionBucket]:
        validate_time_band(time_band)

        async def produce() -> list[DistributionBucket]:
            win = await resolve_window()
            band_sql, band_args = time_band_clause(time_band, 5)
            rows = await db().fetch(
                f"""
                select source,
                       width_bucket(abs_error_seconds, 0, 600, 10) as bucket,
                       count(*) as n
                from prediction_errors
                where horizon_minutes = $3
                  and has_all_horizons
                  and ping_gap_seconds <= $4
                  and start_date between $1 and $2
                  {band_sql}
                group by source, bucket
                order by source, bucket
                """,
                win.test_from,
                win.test_to,
                horizon,
                max_ping_gap,
                *band_args,
            )
            return [
                DistributionBucket(
                    source=row["source"],
                    upper_bound_seconds=min(row["bucket"], 10) * 60,
                    n=row["n"],
                )
                for row in rows
            ]

        return await cache.get(f"dist:{horizon}:{max_ping_gap}:{time_band}", produce)

    @app.get("/api/data-quality", response_model=DataQuality)
    async def data_quality(
        max_ping_gap: int = Query(
            TIGHT_LABEL_SECONDS,
            ge=30,
            le=3600,
            description="The label quality filter the caller is applying elsewhere",
        ),
    ) -> DataQuality:
        async def produce() -> DataQuality:
            coverage = await db().fetch(
                """
                with s as (
                  select started_at::date as day, started_at,
                         extract(epoch from started_at
                                 - lag(started_at) over (order by started_at)) as gap
                  from poll_log
                  where status in ('ok', 'skipped_unchanged')
                    and feed = 'vehicle_positions')
                select day,
                       count(*)                                           as successful_polls,
                       (coalesce(sum(gap) filter (where gap > 120), 0) / 3600.0)::float8
                                                                          as hours_lost,
                       (100.0 * (1 - coalesce(sum(gap) filter (where gap > 120), 0)
                        / nullif(extract(epoch from max(started_at) - min(started_at)), 0)))::float8
                                                                          as coverage_pct
                from s group by day order by day
                """
            )
            bands = await db().fetch(
                """
                select case
                         when ping_gap_seconds is null then 'unknown'
                         when ping_gap_seconds <= 60 then 'tight (<=60s)'
                         when ping_gap_seconds <= 180 then 'ok (<=3min)'
                         when ping_gap_seconds <= 600 then 'loose (<=10min)'
                         else 'poor (>10min)'
                       end as band,
                       count(*) as n,
                       (100.0 * count(*) / sum(count(*)) over ())::float8 as pct
                from arrivals group by 1 order by 2 desc
                """
            )
            total = await db().fetchval("select count(*) from arrivals")

            return DataQuality(
                coverage=[CoverageDay(**dict(row)) for row in coverage],
                label_bands=[LabelBand(**dict(row)) for row in bands],
                arrivals_total=total or 0,
                caveats=[
                    "Collection runs on a laptop, so sleep produces gaps. "
                    "Coverage per day is shown rather than assumed.",
                    "Arrival times are inferred from GPS by interpolation, so a wide "
                    "ping gap means a weaker label. The headline currently excludes "
                    f"arrivals with gaps over {max_ping_gap // 60} min "
                    f"{max_ping_gap % 60}s.",
                    "The model is at parity with MTS, not ahead. Differences of a few "
                    "percent on this much data are inside the noise.",
                    "No weekend data yet: collection began on a Monday.",
                ],
            )

        return await cache.get(f"quality:{max_ping_gap}", produce)

    @app.get("/api/model-run", response_model=ModelRun | None)
    async def model_run() -> ModelRun | None:
        async def produce() -> ModelRun | None:
            row = await db().fetchrow(
                "select trained_at, train_from, train_to, test_from, test_to, "
                "train_rows, test_rows, features, notes "
                "from model_runs order by trained_at desc limit 1"
            )
            if row is None:
                return None

            features = row["features"]
            return ModelRun(
                trained_at=row["trained_at"],
                train_from=row["train_from"],
                train_to=row["train_to"],
                test_from=row["test_from"],
                test_to=row["test_to"],
                train_rows=row["train_rows"],
                test_rows=row["test_rows"],
                features=json.loads(features) if isinstance(features, str) else features,
                notes=row["notes"],
            )

        return await cache.get("model_run", produce)

    @app.get("/api/stops", response_model=list[Stop])
    async def stops(
        limit: int = Query(2000, le=5000, description="Maximum stops returned"),
    ) -> list[Stop]:
        async def produce() -> list[Stop]:
            rows = await db().fetch(
                """
                select a.stop_id,
                       max(s.stop_name)      as stop_name,
                       max(s.stop_lat)::float8 as lat,
                       max(s.stop_lon)::float8 as lon,
                       count(*)              as arrivals
                from arrivals a
                join stops s
                  on s.feed_version = a.feed_version and s.stop_id = a.stop_id
                where s.stop_lat is not null and s.stop_lon is not null
                group by a.stop_id
                order by count(*) desc
                limit $1
                """,
                limit,
            )
            return [Stop(**dict(row)) for row in rows]

        return await cache.get(f"stops:{limit}", produce)

    # Declared before /api/stops/{stop_id}: FastAPI matches routes in order, so a
    # literal segment placed after the parameterised one is never reached and
    # "search" would be treated as a stop id.
    @app.get("/api/stops/search", response_model=list[StopSearchResult])
    async def stop_search(
        q: str = Query(..., min_length=2, description="Part of a stop name"),
        limit: int = Query(12, le=50),
    ) -> list[StopSearchResult]:
        """Find a stop by name. Busiest matches first, since those are the ones
        someone is most likely looking for."""
        rows = await db().fetch(
            """
            select a.stop_id,
                   max(s.stop_name)        as stop_name,
                   max(s.stop_lat)::float8 as lat,
                   max(s.stop_lon)::float8 as lon,
                   count(*)                as arrivals
            from arrivals a
            join stops s
              on s.feed_version = a.feed_version and s.stop_id = a.stop_id
            where s.stop_name ilike '%' || $1 || '%'
              and s.stop_lat is not null and s.stop_lon is not null
            group by a.stop_id
            order by count(*) desc
            limit $2
            """,
            q,
            limit,
        )
        return [StopSearchResult(**dict(row)) for row in rows]

    @app.get("/api/stops/{stop_id}", response_model=StopDetail)
    async def stop_detail(
        stop_id: str,
        horizon: int = Query(10, description="Horizon in minutes"),
        limit: int = Query(25, le=200),
        compare: str = Query("lgbm", description="Predictor to compare MTS against"),
    ) -> StopDetail:
        validate_source(compare)
        name = await db().fetchval("select max(stop_name) from stops where stop_id = $1", stop_id)
        rows = await db().fetch(
            """
            select pe.trip_id, pe.route_id, pe.stop_sequence, pe.arrived_at,
                   pe.horizon_minutes,
                   max(pe.predicted_arrival) filter (where pe.source = 'mts')
                                                          as mts_predicted,
                   max(pe.predicted_arrival) filter (where pe.source = $4)
                                                          as model_predicted,
                   max(pe.error_seconds) filter (where pe.source = 'mts')
                                                          as mts_error_seconds,
                   max(pe.error_seconds) filter (where pe.source = $4)
                                                          as model_error_seconds,
                   max(st.arrival_seconds)                as scheduled_seconds,
                   max(pe.start_date)                     as start_date
            from prediction_errors pe
            left join stop_times st
                   on st.feed_version = pe.feed_version
                  and st.trip_id = pe.trip_id
                  and st.stop_sequence = pe.stop_sequence
            where pe.stop_id = $1 and pe.horizon_minutes = $2
            group by pe.trip_id, pe.route_id, pe.stop_sequence, pe.arrived_at,
                     pe.horizon_minutes
            order by pe.arrived_at desc
            limit $3
            """,
            stop_id,
            horizon,
            limit,
            compare,
        )

        recent = []
        for row in rows:
            scheduled = None
            if row["scheduled_seconds"] is not None and row["start_date"] is not None:
                midnight = datetime.combine(
                    row["start_date"], datetime.min.time(), tzinfo=SERVICE_TZ
                )
                scheduled = midnight + timedelta(seconds=row["scheduled_seconds"])
            recent.append(
                StopArrival(
                    trip_id=row["trip_id"],
                    route_id=row["route_id"],
                    stop_sequence=row["stop_sequence"],
                    arrived_at=row["arrived_at"],
                    scheduled_at=scheduled,
                    horizon_minutes=row["horizon_minutes"],
                    mts_predicted=row["mts_predicted"],
                    model_predicted=row["model_predicted"],
                    mts_error_seconds=row["mts_error_seconds"],
                    model_error_seconds=row["model_error_seconds"],
                )
            )
        return StopDetail(stop_id=stop_id, stop_name=name, recent=recent)

    @app.get("/api/stops/{stop_id}/upcoming", response_model=StopUpcoming)
    async def stop_upcoming(
        stop_id: str,
        limit: int = Query(8, le=30, description="How many services to return"),
        horizon: int = Query(
            BIAS_HORIZON_MINUTES,
            description="Which horizon's measured bias drives the correction",
        ),
        min_sample: int = Query(
            MIN_STOP_ROUTE_SAMPLE,
            ge=1,
            le=5000,
            description="Arrivals required before a stop-and-route bias is used",
        ),
    ) -> StopUpcoming:
        """Live arrivals at a stop, with MTS's estimate and our corrected one.

        The correction is the bias measured for this stop and route in Phase 4.
        It is a statistical correction, not live model inference, and the response
        carries the basis and sample size so the client can say which.
        """
        bias = await cache.get(
            f"bias:{horizon}:{TIGHT_LABEL_SECONDS}",
            lambda: _load_bias(horizon, TIGHT_LABEL_SECONDS),
        )
        stop_bias: dict[tuple[str, str], tuple[int, float]] = bias["stop_route"]
        route_bias: dict[str, tuple[int, float]] = bias["route"]

        info = await db().fetchrow(
            "select max(stop_name) as stop_name, max(stop_lat)::float8 as lat, "
            "max(stop_lon)::float8 as lon from stops where stop_id = $1",
            stop_id,
        )

        rows = await db().fetch(
            """
            select distinct on (p.trip_id, p.stop_sequence)
                   p.trip_id, p.stop_sequence, p.route_id,
                   p.arrival_time, p.observed_at,
                   r.route_short_name, r.route_type, t.trip_headsign
            from predictions p
            left join (
                select distinct on (trip_id) trip_id, trip_headsign, route_id
                from trips order by trip_id, feed_version
            ) t on t.trip_id = p.trip_id
            left join (
                select distinct on (route_id) route_id, route_short_name, route_type
                from routes order by route_id, feed_version
            ) r on r.route_id = coalesce(p.route_id, t.route_id)
            where p.stop_id = $1
              and p.arrival_time is not null
              and p.arrival_time > now()
            order by p.trip_id, p.stop_sequence, p.observed_at desc
            """,
            stop_id,
        )

        arrivals: list[UpcomingArrival] = []
        for row in rows:
            route_id = row["route_id"]
            pair = stop_bias.get((stop_id, route_id)) if route_id else None
            single = route_bias.get(route_id) if route_id else None

            chosen, basis, sample = choose_bias(
                pair[0] if pair else None,
                pair[1] if pair else None,
                single[0] if single else None,
                single[1] if single else None,
                min_stop_route=min_sample,
                # The route level threshold scales with the stop level one, so a
                # viewer loosening the evidence requirement loosens both.
                min_route=max(min_sample * 3, 1),
            )

            corrected = (
                row["arrival_time"] - timedelta(seconds=chosen) if chosen is not None else None
            )
            arrivals.append(
                UpcomingArrival(
                    trip_id=row["trip_id"],
                    route_id=route_id,
                    route_short_name=row["route_short_name"],
                    route_type=row["route_type"],
                    headsign=row["trip_headsign"],
                    stop_sequence=row["stop_sequence"],
                    mts_arrival=row["arrival_time"],
                    predicted_at=row["observed_at"],
                    corrected_arrival=corrected,
                    correction_seconds=-chosen if chosen is not None else None,
                    correction_basis=basis,
                    correction_sample=sample,
                )
            )

        arrivals.sort(key=lambda a: a.mts_arrival)
        return StopUpcoming(
            stop_id=stop_id,
            stop_name=info["stop_name"] if info else None,
            lat=info["lat"] if info else None,
            lon=info["lon"] if info else None,
            arrivals=arrivals[:limit],
            as_of=datetime.now(tz=UTC),
        )

    @app.get("/api/vehicles", response_model=list[Vehicle])
    async def vehicles(
        max_age_minutes: int = Query(15, description="Ignore stale positions"),
    ) -> list[Vehicle]:
        rows = await db().fetch(
            """
            select v.vehicle_id, v.route_id, v.trip_id,
                   v.lat::float8 as lat, v.lon::float8 as lon, v.ts,
                   r.route_short_name,
                   r.route_type
            from (
                select distinct on (vehicle_id)
                       vehicle_id, route_id, trip_id, feed_version_hint, lat, lon, ts
                from (
                    select vehicle_id, route_id, trip_id, null::text as feed_version_hint,
                           lat, lon, ts
                    from vehicle_positions
                    where ts > now() - ($1 * interval '1 minute')
                      and lat is not null and lon is not null
                ) recent
                order by vehicle_id, ts desc
            ) v
            left join (
                select distinct on (route_id) route_id, route_short_name, route_type
                from routes order by route_id, feed_version
            ) r on r.route_id = v.route_id
            """,
            max_age_minutes,
        )
        return [Vehicle(**dict(row)) for row in rows]

    @app.get("/api/routes/{route_id}/shape", response_model=RouteShape)
    async def route_shape(route_id: str) -> RouteShape:
        """One route's geometry only. All shapes together is 192k points."""
        rows = await db().fetch(
            """
            with chosen as (
                select t.shape_id
                from trips t
                where t.route_id = $1 and t.shape_id is not null
                group by t.shape_id
                order by count(*) desc
                limit 1
            )
            select s.shape_pt_lat::float8 as lat, s.shape_pt_lon::float8 as lon
            from shapes s
            join chosen on chosen.shape_id = s.shape_id
            order by s.shape_pt_sequence
            """,
            route_id,
        )
        if not rows:
            raise HTTPException(404, f"no shape for route {route_id}")
        return RouteShape(route_id=route_id, points=[ShapePoint(**dict(row)) for row in rows])

    return app


def main() -> None:
    """Entry point for `make api`."""
    import uvicorn

    from ontime_sd.logging_setup import configure_logging

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    uvicorn.run(create_app(settings), host="127.0.0.1", port=8000, log_config=None)
