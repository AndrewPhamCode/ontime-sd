"""Insert behavior.

poll_log records rows_written, and the gap between rows offered and rows written
is how deduplication is observed. So the count has to be the number actually
inserted, not the number attempted. See ADR-0006.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import asyncpg

from ontime_sd.feeds import PredictionRow, VehiclePositionRow
from ontime_sd.sinks import write_positions, write_predictions

SERVICE_DAY = date(2026, 9, 27)
NOW = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)


def _position(vehicle_id: str = "bus-1", ts: datetime = NOW) -> VehiclePositionRow:
    return VehiclePositionRow(
        vehicle_id=vehicle_id,
        ts=ts,
        trip_id="trip-1",
        route_id="route-1",
        start_date=SERVICE_DAY,
        lat=32.71,
        lon=-117.16,
        bearing=180.0,
        speed=9.5,
        current_stop_sequence=3,
        current_status=2,
        occupancy_status=1,
    )


def _prediction(stop_sequence: int = 1, observed_at: datetime = NOW) -> PredictionRow:
    return PredictionRow(
        start_date=SERVICE_DAY,
        trip_id="trip-1",
        stop_sequence=stop_sequence,
        observed_at=observed_at,
        stop_id="stop-1",
        route_id="route-1",
        arrival_time=NOW + timedelta(minutes=10),
        departure_time=NOW + timedelta(minutes=10, seconds=20),
        delay_seconds=65,
        schedule_relationship=0,
        vehicle_id="bus-1",
    )


async def test_empty_batch_writes_nothing(conn: asyncpg.Connection) -> None:
    assert await write_positions(conn, []) == 0
    assert await write_predictions(conn, []) == 0


async def test_position_count_is_rows_actually_inserted(conn: asyncpg.Connection) -> None:
    rows = [_position("bus-1"), _position("bus-2"), _position("bus-3")]
    assert await write_positions(conn, rows) == 3

    # Same records republished on the next poll: offered three, inserted none.
    assert await write_positions(conn, rows) == 0
    assert await conn.fetchval("select count(*) from vehicle_positions") == 3


async def test_partially_new_batch_counts_only_the_new_rows(
    conn: asyncpg.Connection,
) -> None:
    await write_positions(conn, [_position("bus-1")])

    written = await write_positions(
        conn, [_position("bus-1"), _position("bus-2"), _position("bus-3")]
    )
    assert written == 2


async def test_duplicates_inside_one_batch_are_collapsed(
    conn: asyncpg.Connection,
) -> None:
    """A feed repeating a vehicle within one response must not break the insert."""
    rows = [_position("bus-1"), _position("bus-1"), _position("bus-2")]
    assert await write_positions(conn, rows) == 2


async def test_all_position_columns_round_trip(conn: asyncpg.Connection) -> None:
    await write_positions(conn, [_position()])
    stored = await conn.fetchrow("select * from vehicle_positions")

    assert stored["vehicle_id"] == "bus-1"
    assert stored["ts"] == NOW
    assert stored["trip_id"] == "trip-1"
    assert stored["start_date"] == SERVICE_DAY
    assert stored["lat"] == 32.71
    assert stored["current_stop_sequence"] == 3
    assert stored["current_status"] == 2
    assert stored["occupancy_status"] == 1
    assert stored["ingested_at"] is not None


async def test_nullable_position_fields_are_accepted(conn: asyncpg.Connection) -> None:
    """A sparse feed must still produce a row, not an error."""
    sparse = VehiclePositionRow(
        vehicle_id="bus-9",
        ts=NOW,
        trip_id=None,
        route_id=None,
        start_date=None,
        lat=None,
        lon=None,
        bearing=None,
        speed=None,
        current_stop_sequence=None,
        current_status=None,
        occupancy_status=None,
    )
    assert await write_positions(conn, [sparse]) == 1


async def test_prediction_count_is_rows_actually_inserted(
    conn: asyncpg.Connection,
) -> None:
    rows = [_prediction(1), _prediction(2)]
    assert await write_predictions(conn, rows) == 2
    assert await write_predictions(conn, rows) == 0


async def test_same_stop_observed_later_is_a_new_row(conn: asyncpg.Connection) -> None:
    """Predictions are a time series, so a later observation is not a conflict."""
    await write_predictions(conn, [_prediction(1)])
    written = await write_predictions(
        conn, [_prediction(1, observed_at=NOW + timedelta(seconds=30))]
    )

    assert written == 1
    assert await conn.fetchval("select count(*) from predictions") == 2


async def test_delay_only_prediction_is_stored(conn: asyncpg.Connection) -> None:
    """The single_delay feed shape: a delay and no absolute time. See ADR-0019."""
    row = PredictionRow(
        start_date=SERVICE_DAY,
        trip_id="trip-1",
        stop_sequence=4,
        observed_at=NOW,
        stop_id="stop-4",
        route_id="route-1",
        arrival_time=None,
        departure_time=None,
        delay_seconds=-120,
        schedule_relationship=None,
        vehicle_id="bus-1",
    )
    assert await write_predictions(conn, [row]) == 1

    stored = await conn.fetchrow("select arrival_time, delay_seconds from predictions")
    assert stored["arrival_time"] is None
    assert stored["delay_seconds"] == -120
