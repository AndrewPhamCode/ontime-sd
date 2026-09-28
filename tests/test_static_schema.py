"""Static GTFS schema behavior against a real Postgres.

The load bearing claim here is that two feed versions can hold the same trip_id
with different stop times, because that is what lets Phase 4 compare a prediction
against the schedule that was in effect when it was made. See ADR-0024.
"""

from __future__ import annotations

from datetime import date

import asyncpg
import pytest

V1 = "a" * 64
V2 = "b" * 64

STATIC_TABLES = (
    "feed_versions",
    "agencies",
    "routes",
    "stops",
    "trips",
    "stop_times",
    "shapes",
    "calendar",
    "calendar_dates",
    "service_dates",
    "gtfs_load_log",
)


async def _version(conn: asyncpg.Connection, feed_version: str = V1) -> str:
    await conn.execute(
        """
        insert into feed_versions (feed_version, source_url, mts_feed_version,
                                   feed_start_date, feed_end_date)
        values ($1, 'https://example.test/google_transit.zip', 'Generated on 20260521',
                $2, $3)
        """,
        feed_version,
        date(2026, 6, 7),
        date(2027, 1, 30),
    )
    return feed_version


async def _stop_time(
    conn: asyncpg.Connection,
    feed_version: str,
    *,
    trip_id: str = "19383851",
    stop_sequence: int = 1,
    arrival_seconds: int | None = 18300,
    dist_m: float | None = 0.0,
) -> None:
    await conn.execute(
        """
        insert into stop_times (feed_version, trip_id, stop_sequence, stop_id,
                                arrival_seconds, departure_seconds,
                                shape_dist_traveled_m)
        values ($1, $2, $3, '94048', $4, $4, $5)
        """,
        feed_version,
        trip_id,
        stop_sequence,
        arrival_seconds,
        dist_m,
    )


async def test_all_static_tables_exist(conn: asyncpg.Connection) -> None:
    rows = await conn.fetch(
        "select table_name from information_schema.tables where table_schema='public'"
    )
    names = {row["table_name"] for row in rows}
    assert set(STATIC_TABLES) <= names


async def test_realtime_tables_are_untouched(conn: asyncpg.Connection) -> None:
    """Migration 002 must not disturb Phase 1. The collector is running."""
    rows = await conn.fetch(
        "select table_name from information_schema.tables where table_schema='public'"
    )
    names = {row["table_name"] for row in rows}
    assert {"vehicle_positions", "predictions", "poll_log"} <= names


# --- versioning: the reason every table carries feed_version ---


async def test_same_trip_can_differ_between_feed_versions(
    conn: asyncpg.Connection,
) -> None:
    """MTS reschedules a trip. Both schedules must coexist."""
    await _version(conn, V1)
    await _version(conn, V2)

    await _stop_time(conn, V1, arrival_seconds=18300)  # 05:05:00
    await _stop_time(conn, V2, arrival_seconds=18600)  # 05:10:00

    rows = await conn.fetch(
        "select feed_version, arrival_seconds from stop_times order by feed_version"
    )
    assert [(r["feed_version"], r["arrival_seconds"]) for r in rows] == [
        (V1, 18300),
        (V2, 18600),
    ]


async def test_duplicate_stop_time_within_one_version_is_rejected(
    conn: asyncpg.Connection,
) -> None:
    await _version(conn, V1)
    await _stop_time(conn, V1)

    with pytest.raises(asyncpg.UniqueViolationError):
        await _stop_time(conn, V1)


async def test_deleting_a_version_cascades_to_its_rows(
    conn: asyncpg.Connection,
) -> None:
    """Pruning a version must not leave orphaned stop times behind."""
    await _version(conn, V1)
    await _version(conn, V2)
    await _stop_time(conn, V1)
    await _stop_time(conn, V2)

    await conn.execute("delete from feed_versions where feed_version = $1", V1)

    remaining = await conn.fetch("select feed_version from stop_times")
    assert [r["feed_version"] for r in remaining] == [V2]


async def test_rows_cannot_reference_an_unknown_version(
    conn: asyncpg.Connection,
) -> None:
    """A typo in feed_version must fail at write time, not create a ghost feed."""
    with pytest.raises(asyncpg.ForeignKeyViolationError):
        await _stop_time(conn, "c" * 64)


async def test_loaded_at_starts_null(conn: asyncpg.Connection) -> None:
    """A half finished load is detectable because loaded_at is set last."""
    await _version(conn, V1)
    assert await conn.fetchval("select loaded_at from feed_versions") is None


# --- times past midnight: ADR-0025 ---


@pytest.mark.parametrize(
    ("label", "seconds"),
    [
        ("05:05:00", 18300),
        ("23:59:59", 86399),
        ("24:00:00", 86400),
        ("27:15:00", 98100),
    ],
)
async def test_times_past_midnight_are_storable(
    conn: asyncpg.Connection, label: str, seconds: int
) -> None:
    """The real feed has 11,432 rows at hour 24 or later, max hour 27.

    A time column could not hold any of the last two cases.
    """
    await _version(conn, V1)
    await _stop_time(conn, V1, arrival_seconds=seconds)

    assert await conn.fetchval("select arrival_seconds from stop_times") == seconds


async def test_missing_times_are_allowed(conn: asyncpg.Connection) -> None:
    """GTFS permits a stop_time with no times at a non timepoint stop."""
    await _version(conn, V1)
    await _stop_time(conn, V1, arrival_seconds=None)

    assert await conn.fetchval("select arrival_seconds from stop_times") is None


async def test_negative_time_is_rejected(conn: asyncpg.Connection) -> None:
    """Seconds past midnight cannot be negative, so a parse bug fails loudly."""
    await _version(conn, V1)
    with pytest.raises(asyncpg.CheckViolationError):
        await _stop_time(conn, V1, arrival_seconds=-60)


async def test_negative_distance_is_rejected(conn: asyncpg.Connection) -> None:
    await _version(conn, V1)
    with pytest.raises(asyncpg.CheckViolationError):
        await _stop_time(conn, V1, dist_m=-1.0)


# --- shapes ---


async def test_shape_points_are_ordered_within_a_shape(
    conn: asyncpg.Connection,
) -> None:
    await _version(conn, V1)
    for seq, dist in ((10001, 0.0), (10002, 120.5), (10003, 350.9)):
        await conn.execute(
            """
            insert into shapes (feed_version, shape_id, shape_pt_sequence,
                                shape_pt_lat, shape_pt_lon, shape_dist_traveled_m)
            values ($1, '1_2_161', $2, 32.75, -117.19, $3)
            """,
            V1,
            seq,
            dist,
        )

    rows = await conn.fetch("select shape_dist_traveled_m from shapes order by shape_pt_sequence")
    distances = [r["shape_dist_traveled_m"] for r in rows]
    assert distances == sorted(distances)


# --- calendar ---


async def test_calendar_dates_exception_type_is_constrained(
    conn: asyncpg.Connection,
) -> None:
    """Only 1 (added) and 2 (removed) exist in GTFS."""
    await _version(conn, V1)

    for exception_type in (1, 2):
        await conn.execute(
            """
            insert into calendar_dates (feed_version, service_id, service_date,
                                        exception_type)
            values ($1, $2, $3, $4)
            """,
            V1,
            f"svc-{exception_type}",
            date(2026, 7, 3),
            exception_type,
        )

    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            """
            insert into calendar_dates (feed_version, service_id, service_date,
                                        exception_type)
            values ($1, 'svc-bad', $2, 3)
            """,
            V1,
            date(2026, 7, 4),
        )


async def test_service_dates_answers_what_runs_on_a_date(
    conn: asyncpg.Connection,
) -> None:
    """The query shape Phase 3 and 4 will use constantly."""
    await _version(conn, V1)
    await conn.executemany(
        "insert into service_dates (feed_version, service_date, service_id) values ($1,$2,$3)",
        [
            (V1, date(2026, 9, 27), "sunday-only"),
            (V1, date(2026, 9, 28), "weekday"),
            (V1, date(2026, 9, 29), "weekday"),
        ],
    )

    running = await conn.fetch(
        "select service_id from service_dates where feed_version=$1 and service_date=$2",
        V1,
        date(2026, 9, 28),
    )
    assert [r["service_id"] for r in running] == ["weekday"]


# --- load log ---


@pytest.mark.parametrize(
    "status",
    ["ok", "skipped_unchanged", "skipped_already_loaded", "http_error", "parse_error", "db_error"],
)
async def test_every_documented_load_status_is_accepted(
    conn: asyncpg.Connection, status: str
) -> None:
    await conn.execute("insert into gtfs_load_log (started_at, status) values (now(), $1)", status)
    assert await conn.fetchval("select count(*) from gtfs_load_log") == 1


async def test_unknown_load_status_is_rejected(conn: asyncpg.Connection) -> None:
    with pytest.raises(asyncpg.CheckViolationError):
        await conn.execute(
            "insert into gtfs_load_log (started_at, status) values (now(), 'probably_fine')"
        )


async def test_row_counts_are_queryable_as_json(conn: asyncpg.Connection) -> None:
    """row_counts is how a load is verified after the fact."""
    await conn.execute(
        """
        insert into gtfs_load_log (started_at, status, rows_loaded)
        values (now(), 'ok', $1::jsonb)
        """,
        '{"stop_times": 1376040, "shapes": 192227}',
    )
    got = await conn.fetchval("select (rows_loaded->>'stop_times')::int from gtfs_load_log")
    assert got == 1376040
