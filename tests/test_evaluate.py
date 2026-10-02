"""Phase 4: scoring MTS predictions against inferred arrivals.

Every case is hand built with an answer computed by hand, because this is the
definition that produces the project's headline number and a quiet error in it
would be invisible: the result would still look like a plausible MAE.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import asyncpg

from ontime_sd.evaluate import HORIZONS_MINUTES, evaluate_on_connection

DAY = date(2026, 10, 1)
# 12:00 local on that service day, expressed in UTC.
ARRIVED = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)
VERSION = "d" * 64
TRIP = "trip-1"


async def _setup(conn: asyncpg.Connection) -> None:
    await conn.execute(
        "insert into feed_versions (feed_version, source_url, loaded_at) "
        "values ($1, 'https://example.test/f.zip', now())",
        VERSION,
    )
    await conn.execute(
        "insert into trips (feed_version, trip_id, route_id, service_id) "
        "values ($1, $2, 'route-9', 'svc-1')",
        VERSION,
        TRIP,
    )


async def _arrival(
    conn: asyncpg.Connection,
    stop_sequence: int = 5,
    arrived_at: datetime = ARRIVED,
    ping_gap_seconds: int = 30,
) -> None:
    await conn.execute(
        """
        insert into arrivals (start_date, trip_id, stop_sequence, feed_version,
                              stop_id, vehicle_id, arrived_at, method,
                              ping_gap_seconds)
        values ($1, $2, $3, $4, 'stop-A', 'bus-1', $5, 'interpolated', $6)
        """,
        DAY,
        TRIP,
        stop_sequence,
        VERSION,
        arrived_at,
        ping_gap_seconds,
    )


async def _prediction(
    conn: asyncpg.Connection,
    observed_at: datetime,
    predicted_arrival: datetime,
    stop_sequence: int = 5,
) -> None:
    await conn.execute(
        """
        insert into predictions (start_date, trip_id, stop_sequence, observed_at,
                                 stop_id, arrival_time)
        values ($1, $2, $3, $4, 'stop-A', $5)
        """,
        DAY,
        TRIP,
        stop_sequence,
        observed_at,
        predicted_arrival,
    )


async def _errors(conn: asyncpg.Connection) -> dict[int, asyncpg.Record]:
    rows = await conn.fetch("select * from prediction_errors order by horizon_minutes")
    return {row["horizon_minutes"]: row for row in rows}


# --- the definition ----------------------------------------------------------


async def test_error_is_predicted_minus_actual(conn: asyncpg.Connection) -> None:
    """A prediction 90 seconds late at every horizon gives +90 error."""
    await _setup(conn)
    await _arrival(conn)
    for horizon in HORIZONS_MINUTES:
        await _prediction(
            conn,
            observed_at=ARRIVED - timedelta(minutes=horizon),
            predicted_arrival=ARRIVED + timedelta(seconds=90),
        )

    await evaluate_on_connection(conn, [DAY])
    rows = await _errors(conn)

    assert set(rows) == set(HORIZONS_MINUTES)
    for horizon in HORIZONS_MINUTES:
        assert rows[horizon]["error_seconds"] == 90
        assert rows[horizon]["abs_error_seconds"] == 90


async def test_a_prediction_earlier_than_actual_is_negative(
    conn: asyncpg.Connection,
) -> None:
    """Sign matters: a systematic bias is the easiest thing for a model to beat."""
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=1),
        predicted_arrival=ARRIVED - timedelta(seconds=120),
    )

    await evaluate_on_connection(conn, [DAY])
    row = (await _errors(conn))[1]
    assert row["error_seconds"] == -120
    assert row["abs_error_seconds"] == 120


async def test_the_latest_prediction_before_the_cutoff_wins(
    conn: asyncpg.Connection,
) -> None:
    """MTS revises constantly; the one in force at the cutoff is the one scored."""
    await _setup(conn)
    await _arrival(conn)
    # Scored at the 5 minute horizon, so the cutoff is ARRIVED - 5 min.
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=20),
        predicted_arrival=ARRIVED + timedelta(seconds=600),
    )
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=6),
        predicted_arrival=ARRIVED + timedelta(seconds=60),
    )
    # After the 5 minute cutoff, so it must be ignored at that horizon.
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=2),
        predicted_arrival=ARRIVED,
    )

    await evaluate_on_connection(conn, [DAY])
    rows = await _errors(conn)
    assert rows[5]["error_seconds"] == 60
    assert rows[20]["error_seconds"] == 600


async def test_a_prediction_from_long_before_is_still_the_one_in_force(
    conn: asyncpg.Connection,
) -> None:
    """The change-only storage case, and why ADR-0005 works for Phase 4.

    No row between then and the cutoff means MTS did not change its mind, which is
    information rather than a gap.
    """
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=45),
        predicted_arrival=ARRIVED + timedelta(seconds=30),
    )

    await evaluate_on_connection(conn, [DAY])
    rows = await _errors(conn)
    assert len(rows) == len(HORIZONS_MINUTES)
    assert all(row["error_seconds"] == 30 for row in rows.values())


async def test_a_prediction_exactly_at_the_cutoff_is_included(
    conn: asyncpg.Connection,
) -> None:
    """The boundary is inclusive, so an exact hit is not silently dropped."""
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=10),
        predicted_arrival=ARRIVED + timedelta(seconds=45),
    )

    await evaluate_on_connection(conn, [DAY])
    rows = await _errors(conn)
    assert 10 in rows
    assert rows[10]["error_seconds"] == 45


async def test_a_prediction_made_after_the_arrival_is_never_used(
    conn: asyncpg.Connection,
) -> None:
    """A prediction cannot use information from after the event it predicts."""
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED + timedelta(minutes=5),
        predicted_arrival=ARRIVED,
    )

    await evaluate_on_connection(conn, [DAY])
    assert await _errors(conn) == {}


async def test_a_missing_prediction_produces_no_row_not_a_zero(
    conn: asyncpg.Connection,
) -> None:
    """A zero would drag the mean down and make MTS look better on thin data."""
    await _setup(conn)
    await _arrival(conn)
    # Only 3 minutes ahead, so the 5, 10 and 20 minute horizons have nothing.
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=3),
        predicted_arrival=ARRIVED + timedelta(seconds=10),
    )

    await evaluate_on_connection(conn, [DAY])
    rows = await _errors(conn)
    assert set(rows) == {1}


async def test_a_stale_prediction_is_rejected(conn: asyncpg.Connection) -> None:
    """A prediction 24 hours old is a leftover from another run of the same trip
    id, not the prediction in force. Without this guard such pairs produced errors
    of exactly 24 hours, which destroyed the mean while leaving the median intact.
    """
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(hours=24),
        predicted_arrival=ARRIVED - timedelta(hours=24) + timedelta(minutes=2),
    )

    await evaluate_on_connection(conn, [DAY])
    assert await _errors(conn) == {}


async def test_the_staleness_limit_is_configurable(conn: asyncpg.Connection) -> None:
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(hours=3),
        predicted_arrival=ARRIVED + timedelta(seconds=15),
    )

    await evaluate_on_connection(conn, [DAY], max_prediction_age_seconds=4 * 3600)
    assert (await _errors(conn))[1]["error_seconds"] == 15


# --- comparability and quality ----------------------------------------------


async def test_has_all_horizons_is_true_only_when_all_four_are_present(
    conn: asyncpg.Connection,
) -> None:
    """The flag the headline table filters on, so horizons describe one population."""
    await _setup(conn)
    await _arrival(conn, stop_sequence=1)
    await _arrival(conn, stop_sequence=2)

    # Stop 1 gets all four horizons.
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=30),
        predicted_arrival=ARRIVED,
        stop_sequence=1,
    )
    # Stop 2 only gets the 1 minute horizon.
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=2),
        predicted_arrival=ARRIVED,
        stop_sequence=2,
    )

    await evaluate_on_connection(conn, [DAY])
    rows = await conn.fetch(
        "select stop_sequence, horizon_minutes, has_all_horizons "
        "from prediction_errors order by stop_sequence, horizon_minutes"
    )
    complete = {r["stop_sequence"] for r in rows if r["has_all_horizons"]}
    assert complete == {1}


async def test_ping_gap_is_carried_from_the_arrival(
    conn: asyncpg.Connection,
) -> None:
    """So label quality can be filtered without a join back to arrivals."""
    await _setup(conn)
    await _arrival(conn, ping_gap_seconds=847)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=1),
        predicted_arrival=ARRIVED,
    )

    await evaluate_on_connection(conn, [DAY])
    assert (await _errors(conn))[1]["ping_gap_seconds"] == 847


async def test_route_is_joined_from_the_schedule(conn: asyncpg.Connection) -> None:
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=1),
        predicted_arrival=ARRIVED,
    )

    await evaluate_on_connection(conn, [DAY])
    assert (await _errors(conn))[1]["route_id"] == "route-9"


# --- service time derivation -------------------------------------------------


async def test_service_minute_is_counted_from_the_service_day(
    conn: asyncpg.Connection,
) -> None:
    """12:00 local on the service day is minute 720."""
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=1),
        predicted_arrival=ARRIVED,
    )

    await evaluate_on_connection(conn, [DAY])
    assert (await _errors(conn))[1]["service_minute"] == 720


async def test_an_after_midnight_arrival_exceeds_1440_minutes(
    conn: asyncpg.Connection,
) -> None:
    """A trip that began the previous evening belongs to the earlier service day,
    so its service minute runs past the end of the calendar day.
    """
    after_midnight = datetime(2026, 10, 2, 7, 30, tzinfo=UTC)  # 00:30 local
    await _setup(conn)
    await _arrival(conn, arrived_at=after_midnight)
    await _prediction(
        conn,
        observed_at=after_midnight - timedelta(minutes=1),
        predicted_arrival=after_midnight,
    )

    await evaluate_on_connection(conn, [DAY])
    assert (await _errors(conn))[1]["service_minute"] == 24 * 60 + 30


async def test_weekend_is_derived_from_the_service_day(
    conn: asyncpg.Connection,
) -> None:
    """2026-10-01 is a Thursday."""
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=1),
        predicted_arrival=ARRIVED,
    )

    await evaluate_on_connection(conn, [DAY])
    assert (await _errors(conn))[1]["is_weekend"] is False


# --- idempotency -------------------------------------------------------------


async def test_rerunning_adds_nothing(conn: asyncpg.Connection) -> None:
    """Safe to re-run after a Phase 3 re-run."""
    await _setup(conn)
    await _arrival(conn)
    await _prediction(
        conn,
        observed_at=ARRIVED - timedelta(minutes=30),
        predicted_arrival=ARRIVED,
    )

    first = await evaluate_on_connection(conn, [DAY])
    second = await evaluate_on_connection(conn, [DAY])

    assert first == len(HORIZONS_MINUTES)
    assert second == 0


async def test_no_arrivals_scores_nothing(conn: asyncpg.Connection) -> None:
    await _setup(conn)
    assert await evaluate_on_connection(conn, [DAY]) == 0
