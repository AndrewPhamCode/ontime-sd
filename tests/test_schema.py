"""Schema behavior against a real Postgres.

These tests exist to pin the claims DESIGN.md makes about the schema: that
ingest is idempotent, that the prediction table stores a time series the Phase 4
lookup can query efficiently, and that poll_log refuses statuses the dashboard
would not understand.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import asyncpg
import pytest

from ontime_sd.db import pending_migrations, run_migrations

SERVICE_DAY = date(2026, 9, 27)
NOON = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)


async def test_migrations_are_idempotent(conn: asyncpg.Connection) -> None:
    """Re-running migrations on a migrated database is a no-op."""
    assert await run_migrations(conn) == []


async def test_all_expected_tables_exist(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "select table_name from information_schema.tables where table_schema = 'public'"
    )
    names = {row["table_name"] for row in rows}
    assert {"vehicle_positions", "predictions", "poll_log", "schema_migrations"} <= names


def test_migration_files_sort_numerically() -> None:
    """Zero padded filenames mean lexical order is apply order."""
    pending = pending_migrations(applied=set())
    assert [p.stem for p in pending] == sorted(p.stem for p in pending)
    assert pending, "expected at least one migration file"


# --- vehicle_positions: ADR-0004 ---


async def _insert_position(conn: asyncpg.Connection, vehicle_id: str, ts: datetime, lat: float):
    return await conn.execute(
        """
        insert into vehicle_positions (vehicle_id, ts, trip_id, start_date, lat, lon)
        values ($1, $2, 'trip-1', $3, $4, -117.16)
        on conflict do nothing
        """,
        vehicle_id,
        ts,
        SERVICE_DAY,
        lat,
    )


async def test_repeated_position_is_ignored_not_duplicated(conn: asyncpg.Connection) -> None:
    """Feeds republish the same record across polls. That must be free."""
    await _insert_position(conn, "bus-1", NOON, 32.71)
    await _insert_position(conn, "bus-1", NOON, 32.71)

    count = await conn.fetchval("select count(*) from vehicle_positions")
    assert count == 1


async def test_conflicting_position_keeps_the_first_write(conn: asyncpg.Connection) -> None:
    """Documented accepted loss: same key, different payload, first one wins."""
    await _insert_position(conn, "bus-1", NOON, 32.71)
    await _insert_position(conn, "bus-1", NOON, 33.99)

    lat = await conn.fetchval("select lat from vehicle_positions")
    assert lat == pytest.approx(32.71)


async def test_same_vehicle_at_different_times_is_two_rows(conn: asyncpg.Connection) -> None:
    await _insert_position(conn, "bus-1", NOON, 32.71)
    await _insert_position(conn, "bus-1", NOON + timedelta(seconds=30), 32.72)

    count = await conn.fetchval("select count(*) from vehicle_positions")
    assert count == 2


async def test_timestamptz_preserves_the_instant(conn: asyncpg.Connection) -> None:
    """Storage must not reinterpret the instant in a local timezone."""
    await _insert_position(conn, "bus-1", NOON, 32.71)
    await conn.execute("set local timezone to 'America/Los_Angeles'")

    stored = await conn.fetchval("select ts from vehicle_positions")
    assert stored == NOON


# --- predictions: ADR-0005, and the Phase 4 lookup ---


async def _insert_prediction(
    conn: asyncpg.Connection, observed_at: datetime, arrival_time: datetime
) -> None:
    await conn.execute(
        """
        insert into predictions
            (start_date, trip_id, stop_sequence, observed_at, stop_id, arrival_time)
        values ($1, 'trip-1', 7, $2, 'stop-42', $3)
        on conflict do nothing
        """,
        SERVICE_DAY,
        observed_at,
        arrival_time,
    )


async def test_prediction_key_stores_a_time_series(conn: asyncpg.Connection) -> None:
    """The same stop observed at two times is two rows, not an overwrite."""
    await _insert_prediction(conn, NOON, NOON + timedelta(minutes=10))
    await _insert_prediction(conn, NOON + timedelta(seconds=30), NOON + timedelta(minutes=12))

    count = await conn.fetchval("select count(*) from predictions")
    assert count == 2


async def test_duplicate_observation_is_ignored(conn: asyncpg.Connection) -> None:
    """Replaying a poll cannot create duplicates."""
    await _insert_prediction(conn, NOON, NOON + timedelta(minutes=10))
    await _insert_prediction(conn, NOON, NOON + timedelta(minutes=10))

    count = await conn.fetchval("select count(*) from predictions")
    assert count == 1


# The query Phase 4 will run: what did MTS predict for this stop, as of the
# most recent observation at or before the horizon cutoff.
_HORIZON_LOOKUP = """
select arrival_time
from predictions
where start_date = $1
  and trip_id = $2
  and stop_sequence = $3
  and observed_at <= $4
order by observed_at desc
limit 1
"""


async def test_horizon_lookup_returns_the_prediction_current_at_that_time(
    conn: asyncpg.Connection,
) -> None:
    actual_arrival = NOON + timedelta(minutes=20)

    # MTS revised its estimate three times on the way in.
    await _insert_prediction(conn, actual_arrival - timedelta(minutes=20), actual_arrival)
    await _insert_prediction(
        conn, actual_arrival - timedelta(minutes=10), actual_arrival + timedelta(minutes=3)
    )
    await _insert_prediction(
        conn, actual_arrival - timedelta(minutes=4), actual_arrival + timedelta(minutes=1)
    )

    # At the 5 minute horizon the newest observation is the 10 minute one,
    # because the 4 minute revision had not happened yet.
    got = await conn.fetchval(
        _HORIZON_LOOKUP,
        SERVICE_DAY,
        "trip-1",
        7,
        actual_arrival - timedelta(minutes=5),
    )
    assert got == actual_arrival + timedelta(minutes=3)

    # At the 1 minute horizon the latest revision is visible.
    got = await conn.fetchval(
        _HORIZON_LOOKUP,
        SERVICE_DAY,
        "trip-1",
        7,
        actual_arrival - timedelta(minutes=1),
    )
    assert got == actual_arrival + timedelta(minutes=1)


async def test_horizon_lookup_with_no_prior_prediction_returns_nothing(
    conn: asyncpg.Connection,
) -> None:
    """A stop MTS never predicted must not silently borrow another row."""
    await _insert_prediction(conn, NOON, NOON + timedelta(minutes=10))

    got = await conn.fetchval(
        _HORIZON_LOOKUP, SERVICE_DAY, "trip-1", 7, NOON - timedelta(minutes=5)
    )
    assert got is None


async def test_horizon_lookup_is_served_by_the_primary_key_index(
    conn: asyncpg.Connection,
) -> None:
    """ADR-0005 claims the primary key serves this query via a backward scan.

    Sequential scan is disabled so the planner must reveal whether an index can
    answer the query at all, rather than picking a seq scan because the test
    table is tiny.
    """
    await _insert_prediction(conn, NOON, NOON + timedelta(minutes=10))
    await conn.execute("set local enable_seqscan to off")

    # EXPLAIN returns one row per plan line, so the whole plan has to be joined.
    rows = await conn.fetch(
        f"explain (format text) {_HORIZON_LOOKUP}",
        SERVICE_DAY,
        "trip-1",
        7,
        NOON,
    )
    plan = "\n".join(row[0] for row in rows)
    assert "predictions_pkey" in plan, plan
    assert "Index" in plan, plan
    # Descending order comes from walking the primary key index backwards,
    # which is why no separate descending index exists.
    assert "Backward" in plan, plan


# --- poll_log: ADR-0018 ---


async def _insert_poll(conn: asyncpg.Connection, feed: str, status: str) -> None:
    await conn.execute(
        "insert into poll_log (feed, started_at, status) values ($1, $2, $3)",
        feed,
        NOON,
        status,
    )


@pytest.mark.parametrize(
    "status", ["ok", "skipped_unchanged", "http_error", "parse_error", "db_error"]
)
async def test_every_documented_status_is_accepted(conn: asyncpg.Connection, status: str) -> None:
    await _insert_poll(conn, "vehicle_positions", status)
    assert await conn.fetchval("select count(*) from poll_log") == 1


async def test_unknown_status_is_rejected(conn: asyncpg.Connection) -> None:
    """A typo must fail at write time, not quietly skew the dashboard."""
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_poll(conn, "vehicle_positions", "sort_of_ok")


async def test_unknown_feed_is_rejected(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await _insert_poll(conn, "bus_positions", "ok")
