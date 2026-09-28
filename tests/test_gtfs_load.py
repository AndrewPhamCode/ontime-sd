"""Loading a GTFS archive into Postgres.

Uses the 2.2 KB synthetic feed, whose contents are known exactly, so these tests
can assert on counts and converted values rather than on shapes of data.
"""

from __future__ import annotations

import email.utils
import zipfile
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio

from ontime_sd.gtfs_load import (
    STATUS_HTTP_ERROR,
    STATUS_OK,
    STATUS_PARSE_ERROR,
    STATUS_SKIPPED_ALREADY_LOADED,
    STATUS_SKIPPED_UNCHANGED,
    LoadResult,
    load_archive,
    load_feed,
    record_load,
)
from ontime_sd.gtfs_static import MILES_TO_METRES, GtfsArchive, GtfsParseError
from ontime_sd.tiny_http import Request, Response, bound_port, serve
from tests.conftest import make_settings
from tests.gtfs_fixtures import EXPECTED_ROWS, FILES, write_feed

VERSION = "f" * 64
SOURCE = "https://example.test/google_transit.zip"

# Derived by hand from the fixture calendar. 2026-09-07 is a Monday.
# Weekdays 7-30 September: 18 days, minus the 17th which is removed.
EXPECTED_WEEKDAY_DATES = 17
# Sundays 13, 20, 27 plus the 19th added by exception.
EXPECTED_SUNDAY_DATES = 4
EXPECTED_SERVICE_DATES = EXPECTED_WEEKDAY_DATES + EXPECTED_SUNDAY_DATES

GTFS_TABLES = (
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
)


@pytest.fixture
def feed(tmp_path: Path) -> Path:
    return write_feed(tmp_path / "google_transit.zip")


async def _load(conn: asyncpg.Connection, path: Path, **kwargs: object) -> dict[str, int]:
    with GtfsArchive(path) as archive:
        return await load_archive(
            conn, archive, kwargs.pop("feed_version", VERSION), source_url=SOURCE, **kwargs
        )


# --- row counts ---------------------------------------------------------------


async def test_every_table_gets_the_expected_row_count(
    conn: asyncpg.Connection, feed: Path
) -> None:
    counts = await _load(conn, feed)

    for table, expected in EXPECTED_ROWS.items():
        assert counts[table] == expected, table
        actual = await conn.fetchval(f"select count(*) from {table}")
        assert actual == expected, f"{table} in database"


async def test_service_dates_are_derived_and_stored(conn: asyncpg.Connection, feed: Path) -> None:
    counts = await _load(conn, feed)
    assert counts["service_dates"] == EXPECTED_SERVICE_DATES


async def test_counts_are_recorded_on_the_feed_version(
    conn: asyncpg.Connection, feed: Path
) -> None:
    """row_counts is how a load is verified after the fact."""
    await _load(conn, feed)

    row = await conn.fetchrow("select loaded_at, row_counts from feed_versions")
    assert row["loaded_at"] is not None
    assert row["row_counts"] is not None


async def test_publisher_metadata_is_kept(conn: asyncpg.Connection, feed: Path) -> None:
    await _load(conn, feed)

    row = await conn.fetchrow(
        "select mts_feed_version, feed_start_date, feed_end_date, source_url from feed_versions"
    )
    assert row["mts_feed_version"] == "Generated on 20260901 @ 1200000"
    assert row["feed_start_date"] == date(2026, 9, 7)
    assert row["feed_end_date"] == date(2026, 9, 30)
    assert row["source_url"] == SOURCE


async def test_unlisted_files_are_not_loaded(conn: asyncpg.Connection, feed: Path) -> None:
    """transfers.txt is in the archive and deliberately ignored. See ADR-0030."""
    await _load(conn, feed)

    tables = await conn.fetch(
        "select table_name from information_schema.tables where table_schema='public'"
    )
    assert "transfers" not in {row["table_name"] for row in tables}


# --- conversions, end to end -------------------------------------------------


async def test_times_past_midnight_survive_the_load(conn: asyncpg.Connection, feed: Path) -> None:
    """trip_owl runs 25:30 and 26:05, which no time column could hold."""
    await _load(conn, feed)

    rows = await conn.fetch(
        "select arrival_seconds from stop_times where trip_id = 'trip_owl' order by stop_sequence"
    )
    assert [r["arrival_seconds"] for r in rows] == [91800, 93900]


async def test_distances_are_converted_to_metres(conn: asyncpg.Connection, feed: Path) -> None:
    """The fixture declares 3.25 miles, which must land as ~5230 m."""
    await _load(conn, feed)

    metres = await conn.fetchval(
        "select shape_dist_traveled_m from stop_times "
        "where trip_id = 'trip_day' and stop_sequence = 3"
    )
    assert metres == pytest.approx(3.25 * MILES_TO_METRES)
    assert metres == pytest.approx(5230.368)


async def test_absent_times_load_as_null(conn: asyncpg.Connection, feed: Path) -> None:
    """trip_sun's second stop has no times, which GTFS permits."""
    await _load(conn, feed)

    row = await conn.fetchrow(
        "select arrival_seconds, departure_seconds from stop_times "
        "where trip_id = 'trip_sun' and stop_sequence = 2"
    )
    assert row["arrival_seconds"] is None
    assert row["departure_seconds"] is None


async def test_weekday_flags_become_booleans(conn: asyncpg.Connection, feed: Path) -> None:
    await _load(conn, feed)

    row = await conn.fetchrow(
        "select monday, saturday, sunday from calendar where service_id = 'service_weekday'"
    )
    assert row["monday"] is True
    assert row["saturday"] is False
    assert row["sunday"] is False


async def test_empty_optional_text_is_null_not_blank(conn: asyncpg.Connection, feed: Path) -> None:
    """So a join cannot accidentally match on an empty string."""
    await _load(conn, feed)

    parent = await conn.fetchval("select parent_station from stops where stop_id='stop_a'")
    assert parent is None


# --- the query Phase 3 will run ----------------------------------------------


async def test_a_trips_stops_come_back_in_order(conn: asyncpg.Connection, feed: Path) -> None:
    """The dominant Phase 3 access pattern, served by the primary key."""
    await _load(conn, feed)

    rows = await conn.fetch(
        """
        select stop_id, arrival_seconds, shape_dist_traveled_m
        from stop_times
        where feed_version = $1 and trip_id = 'trip_day'
        order by stop_sequence
        """,
        VERSION,
    )
    assert [r["stop_id"] for r in rows] == ["stop_a", "stop_b", "stop_c"]
    distances = [r["shape_dist_traveled_m"] for r in rows]
    assert distances == sorted(distances), "distance along the shape must increase"


async def test_what_runs_on_a_date_joins_through_service_dates(
    conn: asyncpg.Connection, feed: Path
) -> None:
    """2026-09-17 is a Thursday that the exception removes."""
    await _load(conn, feed)

    async def trips_on(day: date) -> list[str]:
        rows = await conn.fetch(
            """
            select t.trip_id
            from service_dates sd
            join trips t
              on t.feed_version = sd.feed_version and t.service_id = sd.service_id
            where sd.feed_version = $1 and sd.service_date = $2
            order by t.trip_id
            """,
            VERSION,
            day,
        )
        return [r["trip_id"] for r in rows]

    assert await trips_on(date(2026, 9, 16)) == ["trip_day", "trip_owl"]
    assert await trips_on(date(2026, 9, 17)) == [], "removed by exception_type 2"
    assert await trips_on(date(2026, 9, 19)) == ["trip_sun"], "added by exception_type 1"
    assert await trips_on(date(2026, 9, 20)) == ["trip_sun"], "a real Sunday"


# --- versioning --------------------------------------------------------------


async def test_two_versions_of_the_same_feed_coexist(conn: asyncpg.Connection, feed: Path) -> None:
    await _load(conn, feed, feed_version="a" * 64)
    await _load(conn, feed, feed_version="b" * 64)

    assert await conn.fetchval("select count(*) from feed_versions") == 2
    assert await conn.fetchval("select count(*) from stop_times") == EXPECTED_ROWS["stop_times"] * 2


async def test_reloading_the_same_version_without_replace_is_refused(
    conn: asyncpg.Connection, feed: Path
) -> None:
    await _load(conn, feed)
    with pytest.raises(asyncpg.UniqueViolationError):
        await _load(conn, feed)


async def test_replace_reloads_a_version_cleanly(conn: asyncpg.Connection, feed: Path) -> None:
    """What --force does. The old rows must go, not accumulate."""
    await _load(conn, feed)
    await _load(conn, feed, replace=True)

    assert await conn.fetchval("select count(*) from feed_versions") == 1
    assert await conn.fetchval("select count(*) from stop_times") == EXPECTED_ROWS["stop_times"]


# --- atomicity: ADR-0029 -----------------------------------------------------


async def test_a_malformed_feed_leaves_nothing_behind(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    """The load is one transaction, so a bad row anywhere means no rows at all.

    Without this, a feed that fails partway would leave trips with no stop times,
    which looks like a data quality problem rather than a loader failure.
    """
    broken = dict(FILES)
    broken["stop_times.txt"] = FILES["stop_times.txt"].replace("05:12:30", "05:XX:30")
    path = write_feed(tmp_path / "broken.zip", files=broken)

    with pytest.raises(GtfsParseError):
        await _load(conn, path)

    for table in GTFS_TABLES:
        assert await conn.fetchval(f"select count(*) from {table}") == 0, table


async def test_a_missing_required_file_fails_before_loading(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    path = write_feed(tmp_path / "partial.zip", omit=("shapes.txt",))

    with pytest.raises(GtfsParseError, match=r"shapes\.txt"):
        await _load(conn, path)

    assert await conn.fetchval("select count(*) from feed_versions") == 0


async def test_calendar_dates_is_optional(conn: asyncpg.Connection, tmp_path: Path) -> None:
    """A feed may express all service through calendar.txt alone."""
    path = write_feed(tmp_path / "no_exceptions.zip", omit=("calendar_dates.txt",))
    counts = await _load(conn, path)

    assert counts["calendar_dates"] == 0
    # Without the removal exception the weekday service keeps all 18 days, and
    # without the addition the Sunday service has only its 3 Sundays.
    assert counts["service_dates"] == 18 + 3


async def test_chunking_does_not_change_the_result(conn: asyncpg.Connection, feed: Path) -> None:
    """A chunk size below the row count exercises the multi COPY path."""
    counts = await _load(conn, feed, chunk_rows=2)

    assert counts["stop_times"] == EXPECTED_ROWS["stop_times"]
    assert counts["service_dates"] == EXPECTED_SERVICE_DATES


# --- load_feed, over HTTP ----------------------------------------------------

LAST_MODIFIED = email.utils.formatdate(1787329722, usegmt=True)


@pytest_asyncio.fixture(loop_scope="session")
async def clean_gtfs(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    await db_pool.execute("truncate feed_versions cascade")
    await db_pool.execute("truncate gtfs_load_log")
    yield
    await db_pool.execute("truncate feed_versions cascade")
    await db_pool.execute("truncate gtfs_load_log")


@pytest_asyncio.fixture(loop_scope="session")
async def served_feed(
    tmp_path_factory: pytest.TempPathFactory,
) -> AsyncIterator[str]:
    path = write_feed(tmp_path_factory.mktemp("served") / "google_transit.zip")
    payload = path.read_bytes()

    async def handle(request: Request) -> Response:
        if request.path == "/gone.zip":
            return Response(status=503, body=b"unavailable")
        if request.path == "/garbage.zip":
            body = b"" if request.method == "HEAD" else b"not a zip at all"
            return Response(
                body=body, content_type="application/zip", headers=(("Content-Length", "16"),)
            )

        headers = (("Last-Modified", LAST_MODIFIED), ("Content-Length", str(len(payload))))
        body = b"" if request.method == "HEAD" else payload
        return Response(body=body, content_type="application/zip", headers=headers)

    server = await serve(handle, 0)
    try:
        yield f"http://127.0.0.1:{bound_port(server)}"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.usefixtures("clean_gtfs")
async def test_full_load_then_skip_then_force(db_pool: asyncpg.Pool, served_feed: str) -> None:
    """The realistic sequence a weekly scheduled run produces."""
    settings = make_settings(gtfs_static_url=f"{served_feed}/google_transit.zip")

    first = await load_feed(settings, db_pool)
    assert first.status == STATUS_OK
    assert first.feed_version and len(first.feed_version) == 64
    assert first.rows_loaded["stop_times"] == EXPECTED_ROWS["stop_times"]
    assert first.bytes_downloaded and first.bytes_downloaded > 0

    # Headers unchanged, so nothing is downloaded at all.
    second = await load_feed(settings, db_pool)
    assert second.status == STATUS_SKIPPED_UNCHANGED
    assert second.skipped is True
    assert second.bytes_downloaded is None

    # Forcing reloads the same bytes in place rather than duplicating them.
    third = await load_feed(settings, db_pool, force=True)
    assert third.status == STATUS_OK
    assert third.feed_version == first.feed_version
    assert await db_pool.fetchval("select count(*) from feed_versions") == 1


@pytest.mark.usefixtures("clean_gtfs")
async def test_already_loaded_hash_is_skipped(db_pool: asyncpg.Pool, served_feed: str) -> None:
    """Different headers but identical bytes still must not load twice."""
    settings = make_settings(gtfs_static_url=f"{served_feed}/google_transit.zip")
    await load_feed(settings, db_pool)

    # Clearing the recorded headers forces a download, so the hash check is what
    # has to catch the duplicate.
    await db_pool.execute("update feed_versions set last_modified = null")

    result = await load_feed(settings, db_pool)
    assert result.status == STATUS_SKIPPED_ALREADY_LOADED
    assert result.bytes_downloaded and result.bytes_downloaded > 0
    assert await db_pool.fetchval("select count(*) from feed_versions") == 1


@pytest.mark.usefixtures("clean_gtfs")
async def test_http_failure_is_reported_with_its_status(
    db_pool: asyncpg.Pool, served_feed: str
) -> None:
    settings = make_settings(gtfs_static_url=f"{served_feed}/gone.zip")
    result = await load_feed(settings, db_pool)

    assert result.status == STATUS_HTTP_ERROR
    assert result.http_code == 503
    assert await db_pool.fetchval("select count(*) from feed_versions") == 0


@pytest.mark.usefixtures("clean_gtfs")
async def test_unparseable_download_is_a_parse_error(
    db_pool: asyncpg.Pool, served_feed: str
) -> None:
    settings = make_settings(gtfs_static_url=f"{served_feed}/garbage.zip")
    result = await load_feed(settings, db_pool)

    assert result.status == STATUS_PARSE_ERROR
    assert "zip" in (result.error or "").lower()
    assert await db_pool.fetchval("select count(*) from feed_versions") == 0


@pytest.mark.usefixtures("clean_gtfs")
async def test_unreachable_host_is_reported_without_a_status(
    db_pool: asyncpg.Pool,
) -> None:
    settings = make_settings(gtfs_static_url="http://127.0.0.1:1/google_transit.zip")
    result = await load_feed(settings, db_pool)

    assert result.status == STATUS_HTTP_ERROR
    assert result.http_code is None


# --- gtfs_load_log -----------------------------------------------------------


@pytest.mark.usefixtures("clean_gtfs")
async def test_load_outcome_is_logged(db_pool: asyncpg.Pool) -> None:
    await record_load(
        db_pool,
        LoadResult(
            status=STATUS_OK,
            feed_version=VERSION,
            rows_loaded={"stop_times": 1376040},
            duration_ms=41234,
            bytes_downloaded=8804780,
            http_code=200,
        ),
    )

    row = await db_pool.fetchrow("select * from gtfs_load_log")
    assert row["status"] == STATUS_OK
    assert row["duration_ms"] == 41234
    assert row["bytes"] == 8804780
    stop_times = await db_pool.fetchval(
        "select (rows_loaded->>'stop_times')::int from gtfs_load_log"
    )
    assert stop_times == 1376040


@pytest.mark.usefixtures("clean_gtfs")
async def test_logging_a_skip_is_recorded_too(db_pool: asyncpg.Pool) -> None:
    """Without the skips, a loader that stopped running looks like a stable feed."""
    await record_load(db_pool, LoadResult(status=STATUS_SKIPPED_UNCHANGED, skipped=True))

    assert await db_pool.fetchval("select status from gtfs_load_log") == (STATUS_SKIPPED_UNCHANGED)


@pytest.mark.usefixtures("clean_gtfs")
async def test_a_bad_log_write_does_not_raise(db_pool: asyncpg.Pool) -> None:
    """Losing observability must never fail a load."""
    await record_load(db_pool, LoadResult(status="not_a_real_status"))
    assert await db_pool.fetchval("select count(*) from gtfs_load_log") == 0


async def test_long_error_text_is_truncated(db_pool: asyncpg.Pool) -> None:
    await db_pool.execute("truncate gtfs_load_log")
    await record_load(db_pool, LoadResult(status=STATUS_PARSE_ERROR, error="x" * 5000))

    stored = await db_pool.fetchval("select error from gtfs_load_log")
    assert stored is not None and len(stored) < 1100
    assert stored.endswith("...")
    await db_pool.execute("truncate gtfs_load_log")


def test_fixture_is_small_enough_to_stay_offline(feed: Path) -> None:
    """If this grows, the suite stopped being a unit test suite."""
    assert feed.stat().st_size < 10_000


def test_fixture_is_a_valid_zip(feed: Path) -> None:
    assert zipfile.ZipFile(feed).testzip() is None
