"""Phase 5: three predictors, scored against MTS on identical rows.

  persist_delay  the bus is N seconds late now, assume it stays N seconds late
  segment_mean   anchor time plus historical travel time for each segment ahead
  lgbm           gradient boosting on features known at the cutoff

All three forecast from the same anchor and are written into `prediction_errors`
with a `source`, so the comparison is a single GROUP BY over one population scored
by one definition. See DESIGN.md ADR-0038.

persist_delay exists because it is the honest obvious thing. A gradient boosted
model that beats MTS but loses to "assume it stays as late as it is now" has
demonstrated nothing, and that result would be easy to hide behind a single
headline.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta

import asyncpg
import numpy as np

from ontime_sd.config import SERVICE_TZ
from ontime_sd.features import (
    FEATURE_NAMES,
    Context,
    build_matrix,
    load_evaluation_contexts,
    load_training_contexts,
    segment_sum,
)
from ontime_sd.segments import SegmentMeans, fit_segment_stats, load_segment_means

log = logging.getLogger(__name__)

SOURCE_PERSIST = "persist_delay"
SOURCE_SEGMENT = "segment_mean"
SOURCE_LGBM = "lgbm"

# Mean absolute error as the objective, matching the metric being reported. An
# L2 objective would chase the long tail of badly observed arrivals instead.
LGBM_PARAMS = {
    "objective": "l1",
    "metric": "l1",
    "num_leaves": 63,
    "learning_rate": 0.05,
    "min_data_in_leaf": 100,
    "feature_fraction": 0.9,
    "bagging_fraction": 0.8,
    "bagging_freq": 5,
    "verbose": -1,
    # Fixed so a rerun reproduces the number in the README.
    "seed": 20261002,
    "deterministic": True,
    "num_threads": 4,
}
LGBM_ROUNDS = 300

_INSERT_SQL = """
insert into prediction_errors (
    start_date, trip_id, stop_sequence, horizon_minutes, source,
    feed_version, stop_id, route_id,
    arrived_at, predicted_arrival, predicted_at,
    error_seconds, abs_error_seconds, ping_gap_seconds,
    service_minute, is_weekend, has_all_horizons
)
select
    base.start_date, base.trip_id, base.stop_sequence, base.horizon_minutes,
    $1::text,
    base.feed_version, base.stop_id, base.route_id,
    base.arrived_at,
    p.predicted_arrival,
    base.arrived_at - (base.horizon_minutes * interval '1 minute'),
    round(extract(epoch from p.predicted_arrival - base.arrived_at))::int,
    abs(round(extract(epoch from p.predicted_arrival - base.arrived_at))::int),
    base.ping_gap_seconds, base.service_minute, base.is_weekend,
    base.has_all_horizons
from prediction_errors base
join unnest($2::date[], $3::text[], $4::int[], $5::int[], $6::timestamptz[])
     as p(start_date, trip_id, stop_sequence, horizon_minutes, predicted_arrival)
  on p.start_date = base.start_date
 and p.trip_id = base.trip_id
 and p.stop_sequence = base.stop_sequence
 and p.horizon_minutes = base.horizon_minutes
where base.source = 'mts'
on conflict do nothing
"""


@dataclass(frozen=True, slots=True)
class Prediction:
    context: Context
    predicted_arrival: datetime


def _scheduled_instant(start_date: date, scheduled_seconds: int) -> datetime:
    """Turn a service-day offset into an absolute instant.

    Built by adding the offset to the service day's local midnight, so a scheduled
    time past 24:00:00 lands on the following calendar day as it should.
    """
    midnight = datetime.combine(start_date, datetime.min.time(), tzinfo=SERVICE_TZ)
    return midnight + timedelta(seconds=scheduled_seconds)


def predict_persist_delay(context: Context) -> datetime:
    """Scheduled arrival plus however late the vehicle already is."""
    scheduled_target = _scheduled_instant(context.start_date, context.target_scheduled_seconds)
    return scheduled_target + timedelta(seconds=context.anchor_delay_seconds)


def predict_segment_mean(
    context: Context,
    trip_stops: list[tuple[int, str]],
    means: SegmentMeans,
) -> datetime:
    """Anchor arrival plus the historical travel time of each segment ahead."""
    total, _ = segment_sum(context, trip_stops, means)
    return context.anchor_arrived_at + timedelta(seconds=total)


async def load_trip_stops(
    conn: asyncpg.Connection, first_day: date, last_day: date
) -> dict[tuple[date, str], list[tuple[int, str]]]:
    """Every observed trip's stop list, for walking segments between two stops."""
    rows = await conn.fetch(
        """
        select distinct a.start_date, a.trip_id, s.stop_sequence, s.stop_id
        from arrivals a
        join stop_times s
          on s.feed_version = a.feed_version and s.trip_id = a.trip_id
        where a.start_date between $1 and $2
        order by a.start_date, a.trip_id, s.stop_sequence
        """,
        first_day,
        last_day,
    )
    stops: dict[tuple[date, str], list[tuple[int, str]]] = {}
    for row in rows:
        stops.setdefault((row["start_date"], row["trip_id"]), []).append(
            (row["stop_sequence"], row["stop_id"])
        )
    return stops


async def store_predictions(
    conn: asyncpg.Connection, source: str, predictions: list[Prediction]
) -> int:
    """Write predictions as scored rows, reusing the arrival they were made for."""
    if not predictions:
        return 0

    status = await conn.execute(
        _INSERT_SQL,
        source,
        [p.context.start_date for p in predictions],
        [p.context.trip_id for p in predictions],
        [p.context.target_sequence for p in predictions],
        [p.context.horizon_minutes for p in predictions],
        [p.predicted_arrival for p in predictions],
    )
    try:
        return int(status.split()[-1])
    except (ValueError, IndexError):
        return 0


async def run_phase5(
    pool: asyncpg.Pool,
    train_from: date,
    train_to: date,
    test_from: date,
    test_to: date,
) -> dict[str, int]:
    """Fit, predict and score all three predictors on the test window."""
    import lightgbm as lgb

    if train_to >= test_from:
        raise ValueError(
            f"training window must end before the test window: {train_to} is not before {test_from}"
        )

    written: dict[str, int] = {}

    async with pool.acquire() as conn:
        feed_version = await conn.fetchval(
            "select feed_version from feed_versions where loaded_at is not null "
            "order by loaded_at desc limit 1"
        )
        if feed_version is None:
            raise RuntimeError("no loaded GTFS schedule")

        # Fit segment statistics on training days ONLY. This is the leak that
        # would be easiest to miss: a mean computed over all days has seen the
        # test period. fit_through_date records the boundary in the data.
        await fit_segment_stats(conn, feed_version, train_to)
        means = await load_segment_means(conn, feed_version, train_to)
        if means.fit_through >= test_from:
            raise ValueError(
                f"segment stats fit through {means.fit_through}, which is not "
                f"before the test window starting {test_from}"
            )

        log.info(
            "loading contexts",
            extra={
                "train": f"{train_from}..{train_to}",
                "test": f"{test_from}..{test_to}",
            },
        )
        train_contexts = await load_training_contexts(conn, train_from, train_to)
        test_contexts = await load_evaluation_contexts(conn, test_from, test_to)
        train_stops = await load_trip_stops(conn, train_from, train_to)
        test_stops = await load_trip_stops(conn, test_from, test_to)

    log.info(
        "contexts loaded",
        extra={"train_rows": len(train_contexts), "test_rows": len(test_contexts)},
    )

    # --- the two baselines ---

    persist = [Prediction(context, predict_persist_delay(context)) for context in test_contexts]
    segment = [
        Prediction(
            context,
            predict_segment_mean(
                context, test_stops.get((context.start_date, context.trip_id), []), means
            ),
        )
        for context in test_contexts
    ]

    # --- the model ---

    x_train, y_train = build_matrix(train_contexts, train_stops, means)
    usable = ~np.isnan(y_train)
    x_train, y_train = x_train[usable], y_train[usable]

    booster = lgb.train(
        LGBM_PARAMS,
        lgb.Dataset(x_train, label=y_train, feature_name=list(FEATURE_NAMES)),
        num_boost_round=LGBM_ROUNDS,
    )

    x_test, _ = build_matrix(test_contexts, test_stops, means)
    remaining = booster.predict(x_test)
    lgbm = [
        Prediction(
            context,
            context.anchor_arrived_at + timedelta(seconds=max(float(seconds), 0.0)),
        )
        for context, seconds in zip(test_contexts, remaining, strict=True)
    ]

    async with pool.acquire() as conn:
        for source, predictions in (
            (SOURCE_PERSIST, persist),
            (SOURCE_SEGMENT, segment),
            (SOURCE_LGBM, lgbm),
        ):
            written[source] = await store_predictions(conn, source, predictions)
            log.info("stored predictions", extra={"source": source, "rows": written[source]})

        mae = await conn.fetch(
            """
            select source, horizon_minutes,
                   round(avg(abs_error_seconds))::int as mae_seconds
            from prediction_errors
            where start_date between $1 and $2
              and has_all_horizons and ping_gap_seconds <= 180
            group by source, horizon_minutes
            order by source, horizon_minutes
            """,
            test_from,
            test_to,
        )
        summary: dict[str, dict[str, int]] = {}
        for row in mae:
            summary.setdefault(row["source"], {})[str(row["horizon_minutes"])] = row["mae_seconds"]

        await conn.execute(
            """
            insert into model_runs (source, train_from, train_to, test_from, test_to,
                                    train_rows, test_rows, features, params,
                                    mae_by_horizon, notes)
            values ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9::jsonb,$10::jsonb,$11)
            """,
            SOURCE_LGBM,
            train_from,
            train_to,
            test_from,
            test_to,
            len(y_train),
            len(test_contexts),
            json.dumps(list(FEATURE_NAMES)),
            json.dumps({**LGBM_PARAMS, "num_boost_round": LGBM_ROUNDS}),
            json.dumps(summary),
            f"segment stats fit through {means.fit_through}",
        )

    written["train_rows"] = len(y_train)
    written["test_rows"] = len(test_contexts)
    return written


def main() -> None:
    """Entry point for `make model`."""
    import argparse
    import asyncio

    from ontime_sd.config import Settings
    from ontime_sd.db import create_pool
    from ontime_sd.logging_setup import configure_logging

    parser = argparse.ArgumentParser(description="Fit and score the Phase 5 predictors")
    parser.add_argument("--train-from", required=True)
    parser.add_argument("--train-to", required=True)
    parser.add_argument("--test-from", required=True)
    parser.add_argument("--test-to", required=True)
    args = parser.parse_args()

    settings = Settings.from_env()
    configure_logging(settings.log_level)

    async def run() -> dict[str, int]:
        pool = await create_pool(settings)
        try:
            return await run_phase5(
                pool,
                date.fromisoformat(args.train_from),
                date.fromisoformat(args.train_to),
                date.fromisoformat(args.test_from),
                date.fromisoformat(args.test_to),
            )
        finally:
            await pool.close()

    written = asyncio.run(run())
    print("Phase 5 complete")
    for key, value in written.items():
        print(f"  {key:<16} {value:>10,}")
    print()
    print("Run `make compare` for the head-to-head against MTS.")
