"""Phase 5: segment statistics, the predictors, and the leakage guards.

The guard tests are the important ones. A leaked feature produces a result that
looks like success, which is why the loose version of the anchor condition briefly
appeared to beat MTS at every horizon. These pin the conditions that stopped it.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from ontime_sd.features import Context, load_evaluation_contexts, segment_sum
from ontime_sd.model import (
    predict_persist_delay,
    predict_segment_mean,
    run_phase5,
)
from ontime_sd.segments import (
    LEVEL_EXACT,
    LEVEL_GLOBAL,
    LEVEL_SEGMENT,
    SegmentMeans,
    fit_segment_stats,
    load_segment_means,
)

VERSION = "c" * 64
TRIP = "trip-1"
TRAIN_DAY = date(2026, 10, 1)
TEST_DAY = date(2026, 10, 2)
# 12:00 local on the service day.
NOON = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)


async def _schedule(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "insert into feed_versions (feed_version, source_url, loaded_at) "
        "values ($1, 'https://example.test/f.zip', now())",
        VERSION,
    )
    await conn.execute(
        "insert into trips (feed_version, trip_id, route_id, service_id) "
        "values ($1, $2, 'route-1', 'svc')",
        VERSION,
        TRIP,
    )
    # Stops 1, 2, 3 scheduled at 12:00, 12:02, 12:05.
    for sequence, seconds in ((1, 43200), (2, 43320), (3, 43500)):
        await conn.execute(
            """
            insert into stop_times (feed_version, trip_id, stop_sequence, stop_id,
                                    arrival_seconds, departure_seconds)
            values ($1, $2, $3, $4, $5, $5)
            """,
            VERSION,
            TRIP,
            sequence,
            f"stop-{sequence}",
            seconds,
        )


async def _arrival(
    conn: asyncpg.Connection,
    stop_sequence: int,
    arrived_at: datetime,
    *,
    day: date = TRAIN_DAY,
    ping_gap_seconds: int = 30,
) -> None:
    await conn.execute(
        """
        insert into arrivals (start_date, trip_id, stop_sequence, feed_version,
                              stop_id, vehicle_id, arrived_at, method,
                              ping_gap_seconds)
        values ($1, $2, $3, $4, $5, 'bus-1', $6, 'interpolated', $7)
        """,
        day,
        TRIP,
        stop_sequence,
        VERSION,
        f"stop-{stop_sequence}",
        arrived_at,
        ping_gap_seconds,
    )


def _context(
    anchor_sequence: int = 1,
    target_sequence: int = 3,
    anchor_arrived_at: datetime = NOON,
    anchor_scheduled: int = 43200,
    target_scheduled: int = 43500,
) -> Context:
    return Context(
        start_date=TRAIN_DAY,
        trip_id=TRIP,
        target_sequence=target_sequence,
        target_stop_id=f"stop-{target_sequence}",
        anchor_sequence=anchor_sequence,
        anchor_stop_id=f"stop-{anchor_sequence}",
        anchor_arrived_at=anchor_arrived_at,
        anchor_scheduled_seconds=anchor_scheduled,
        target_scheduled_seconds=target_scheduled,
        hour_bin=12,
        is_weekend=False,
    )


# --- persist delay -----------------------------------------------------------


def test_an_on_time_vehicle_is_predicted_on_time() -> None:
    """Anchor exactly on schedule, so the target prediction is its schedule."""
    context = _context(anchor_arrived_at=NOON)
    predicted = predict_persist_delay(context)

    # Stop 3 is scheduled 5 minutes after midday.
    assert predicted == NOON + timedelta(seconds=300)


def test_a_late_vehicle_stays_late() -> None:
    """Four minutes down at the anchor means four minutes down at the target."""
    context = _context(anchor_arrived_at=NOON + timedelta(minutes=4))
    predicted = predict_persist_delay(context)

    assert predicted == NOON + timedelta(seconds=300) + timedelta(minutes=4)


def test_an_early_vehicle_stays_early() -> None:
    context = _context(anchor_arrived_at=NOON - timedelta(minutes=2))
    assert predict_persist_delay(context) == NOON + timedelta(seconds=300) - timedelta(minutes=2)


def test_anchor_delay_is_computed_against_the_service_day() -> None:
    context = _context(anchor_arrived_at=NOON + timedelta(seconds=75))
    assert context.anchor_delay_seconds == pytest.approx(75.0)


# --- segment means -----------------------------------------------------------


def _means(**exact: float) -> SegmentMeans:
    means = SegmentMeans(fit_through=TRAIN_DAY, global_mean=100.0)
    for key, value in exact.items():
        from_stop, to_stop = key.split("__")
        means.exact[(from_stop, to_stop, 12, False)] = value
    means.by_segment = {
        tuple(key.split("__")): value
        for key, value in exact.items()  # type: ignore[misc]
    }
    return means


def test_segment_prediction_sums_the_means_ahead() -> None:
    """Two segments of 90 and 150 seconds puts the target 240 seconds out."""
    means = _means(**{"stop-1__stop-2": 90.0, "stop-2__stop-3": 150.0})
    trip_stops = [(1, "stop-1"), (2, "stop-2"), (3, "stop-3")]

    predicted = predict_segment_mean(_context(), trip_stops, means)
    assert predicted == NOON + timedelta(seconds=240)


def test_only_segments_between_anchor_and_target_are_summed() -> None:
    means = _means(**{"stop-1__stop-2": 90.0, "stop-2__stop-3": 150.0})
    trip_stops = [(1, "stop-1"), (2, "stop-2"), (3, "stop-3")]

    predicted = predict_segment_mean(
        _context(anchor_sequence=2, target_sequence=3), trip_stops, means
    )
    assert predicted == NOON + timedelta(seconds=150)


def test_the_fallback_chain_fires_in_order() -> None:
    means = SegmentMeans(fit_through=TRAIN_DAY, global_mean=100.0)
    means.exact[("a", "b", 12, False)] = 42.0
    means.by_segment[("a", "b")] = 55.0
    means.by_segment[("c", "d")] = 70.0

    assert means.lookup("a", "b", 12, False) == (42.0, LEVEL_EXACT)
    # Same segment, different hour, so it falls back to the segment average.
    assert means.lookup("a", "b", 3, False) == (55.0, LEVEL_SEGMENT)
    # Unknown segment entirely.
    assert means.lookup("x", "y", 12, False) == (100.0, LEVEL_GLOBAL)


def test_coarse_share_reports_how_much_rested_on_a_fallback() -> None:
    """So a prediction built mostly from the global mean is identifiable."""
    means = SegmentMeans(fit_through=TRAIN_DAY, global_mean=100.0)
    means.exact[("stop-1", "stop-2", 12, False)] = 90.0
    trip_stops = [(1, "stop-1"), (2, "stop-2"), (3, "stop-3")]

    total, coarse = segment_sum(_context(), trip_stops, means)
    assert total == pytest.approx(190.0)
    assert coarse == pytest.approx(0.5), "one of two segments fell back"


def test_a_single_stop_span_yields_nothing_to_sum() -> None:
    means = SegmentMeans(fit_through=TRAIN_DAY)
    total, coarse = segment_sum(
        _context(anchor_sequence=3, target_sequence=3), [(3, "stop-3")], means
    )
    assert total == 0.0
    assert coarse == 1.0


# --- fitting respects the training boundary ---------------------------------


async def test_segment_stats_ignore_days_after_the_fit_boundary(
    conn: asyncpg.Connection,
) -> None:
    """The leak that is easiest to miss: a historical mean that saw the test days.

    The training day segment takes 120 seconds and the test day segment takes 600.
    A fit through the training day must not know about the 600.
    """
    await _schedule(conn)
    await _arrival(conn, 1, NOON, day=TRAIN_DAY)
    await _arrival(conn, 2, NOON + timedelta(seconds=120), day=TRAIN_DAY)
    await _arrival(conn, 1, NOON + timedelta(days=1), day=TEST_DAY)
    await _arrival(conn, 2, NOON + timedelta(days=1, seconds=600), day=TEST_DAY)

    await fit_segment_stats(conn, VERSION, TRAIN_DAY)
    means = await load_segment_means(conn, VERSION, TRAIN_DAY)

    seconds, level = means.lookup("stop-1", "stop-2", 12, False)
    assert level == LEVEL_EXACT
    assert seconds == pytest.approx(120.0), "the test day must not influence this"


async def test_fit_through_date_is_recorded_with_the_stats(
    conn: asyncpg.Connection,
) -> None:
    """Stored rather than implied, so a leak is visible in the data."""
    await _schedule(conn)
    await _arrival(conn, 1, NOON)
    await _arrival(conn, 2, NOON + timedelta(seconds=120))
    await fit_segment_stats(conn, VERSION, TRAIN_DAY)

    recorded = await conn.fetchval("select distinct fit_through_date from segment_stats")
    assert recorded == TRAIN_DAY


async def test_endpoints_with_poor_labels_are_excluded(
    conn: asyncpg.Connection,
) -> None:
    """A segment bounded by two interpolated guesses is not a measurement."""
    await _schedule(conn)
    await _arrival(conn, 1, NOON, ping_gap_seconds=900)
    await _arrival(conn, 2, NOON + timedelta(seconds=120), ping_gap_seconds=900)

    await fit_segment_stats(conn, VERSION, TRAIN_DAY)
    assert await conn.fetchval("select count(*) from segment_stats") == 0


# --- the anchor leakage guard ------------------------------------------------


async def _evaluation_row(conn: asyncpg.Connection, horizon: int, target_arrived: datetime) -> None:
    await conn.execute(
        """
        insert into prediction_errors (
            start_date, trip_id, stop_sequence, horizon_minutes, source,
            feed_version, stop_id, route_id, arrived_at, predicted_arrival,
            predicted_at, error_seconds, abs_error_seconds, service_minute,
            is_weekend, has_all_horizons, ping_gap_seconds)
        values ($1,$2,3,$3::int,'mts',$4,'stop-3','route-1',$5::timestamptz,$5::timestamptz,
                $5::timestamptz - ($3::int * interval '1 minute'),0,0,720,false,true,30)
        """,
        TRAIN_DAY,
        TRIP,
        horizon,
        VERSION,
        target_arrived,
    )


async def test_an_anchor_knowable_before_the_cutoff_is_used(
    conn: asyncpg.Connection,
) -> None:
    await _schedule(conn)
    target = NOON + timedelta(minutes=10)
    # Anchor at NOON with a 30 second gap, so knowable at NOON+30s, well before
    # the 5 minute cutoff at target-5min.
    await _arrival(conn, 1, NOON, ping_gap_seconds=30)
    await _arrival(conn, 3, target)
    await _evaluation_row(conn, 5, target)

    contexts = await load_evaluation_contexts(conn, TRAIN_DAY, TRAIN_DAY)
    assert len(contexts) == 1
    assert contexts[0].anchor_sequence == 1


async def test_an_anchor_whose_interpolation_window_crosses_the_cutoff_is_refused(
    conn: asyncpg.Connection,
) -> None:
    """The leak that made the model look like it beat MTS.

    The anchor arrival is before the cutoff, but it was interpolated across a gap
    that closes after the cutoff, so its timestamp was computed from GPS received
    after the moment the prediction is supposed to be made.
    """
    await _schedule(conn)
    target = NOON + timedelta(minutes=10)
    # Cutoff for the 5 minute horizon is target - 5min = NOON + 5min.
    # Anchor at NOON + 4min with a 600 second gap closes at NOON + 14min.
    await _arrival(conn, 1, NOON + timedelta(minutes=4), ping_gap_seconds=600)
    await _arrival(conn, 3, target)
    await _evaluation_row(conn, 5, target)

    contexts = await load_evaluation_contexts(conn, TRAIN_DAY, TRAIN_DAY)
    assert contexts == [], "an anchor that is not yet knowable must not be used"


async def test_an_arrival_after_the_cutoff_is_never_an_anchor(
    conn: asyncpg.Connection,
) -> None:
    await _schedule(conn)
    target = NOON + timedelta(minutes=10)
    # Only arrival before the target is itself after the 5 minute cutoff.
    await _arrival(conn, 1, NOON + timedelta(minutes=7), ping_gap_seconds=10)
    await _arrival(conn, 3, target)
    await _evaluation_row(conn, 5, target)

    assert await load_evaluation_contexts(conn, TRAIN_DAY, TRAIN_DAY) == []


async def test_the_target_stop_is_never_its_own_anchor(
    conn: asyncpg.Connection,
) -> None:
    """Using the label as a feature would be total leakage."""
    await _schedule(conn)
    target = NOON + timedelta(minutes=10)
    await _arrival(conn, 3, target)
    await _evaluation_row(conn, 5, target)

    assert await load_evaluation_contexts(conn, TRAIN_DAY, TRAIN_DAY) == []


# --- window validation -------------------------------------------------------


async def test_overlapping_train_and_test_windows_are_refused(
    db_pool: asyncpg.Pool,
) -> None:
    """A time-based split is the whole point; overlap would invalidate the result."""
    with pytest.raises(ValueError, match="must end before"):
        await run_phase5(
            db_pool,
            train_from=date(2026, 10, 1),
            train_to=date(2026, 10, 3),
            test_from=date(2026, 10, 2),
            test_to=date(2026, 10, 4),
        )
