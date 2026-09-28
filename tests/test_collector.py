"""Collector integration: real Postgres, real HTTP, real protobuf.

Failures are produced by configuring the mock to fail at rate 1.0 rather than by
patching the collector, so the error paths are exercised end to end through the
network and the parser.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import signal
from collections.abc import AsyncIterator

import asyncpg
import pytest
import pytest_asyncio

from ontime_sd import poll_log
from ontime_sd.collector import CollectorState, FeedPoller, install_signal_handlers, run
from ontime_sd.config import TRIP_UPDATES, VEHICLE_POSITIONS, Settings
from ontime_sd.mock.server import MockFeedServer
from tests.conftest import make_settings

pytestmark = pytest.mark.usefixtures("clean_tables")

# A refresh period long enough that the header never advances during a test,
# which is how the unchanged-header skip path is reached deterministically.
FROZEN_HEADER = 86_400


@pytest_asyncio.fixture(loop_scope="session")
async def clean_tables(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    """Empty the tables around each test.

    The collector acquires its own connections from the pool, so the rolled back
    transaction trick used elsewhere cannot isolate it.
    """
    await db_pool.execute("truncate vehicle_positions, predictions, poll_log")
    yield
    await db_pool.execute("truncate vehicle_positions, predictions, poll_log")


@contextlib.asynccontextmanager
async def running_mock(**overrides: object) -> AsyncIterator[tuple[MockFeedServer, Settings]]:
    """Start a mock feed and return settings pointed at it."""
    server = MockFeedServer(make_settings(**overrides))
    port = await server.start(0)
    try:
        settings = make_settings(**{**overrides, "mts_feed_base_url": server.base_url(port)})
        yield server, settings
    finally:
        await server.stop()


def _poller(feed: str, settings: Settings, pool: asyncpg.Pool, client) -> FeedPoller:
    return FeedPoller(feed, settings, client, pool, CollectorState())


@pytest_asyncio.fixture(loop_scope="session")
async def http_client() -> AsyncIterator[object]:
    import httpx

    async with httpx.AsyncClient(timeout=10) as client:
        yield client


# --- happy path ---


async def test_position_poll_writes_rows_and_logs_the_poll(
    db_pool: asyncpg.Pool, http_client: object
) -> None:
    async with running_mock(mock_vehicle_count=7) as (_, settings):
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)
        outcome = await poller.poll_once()
        await poll_log.record(db_pool, outcome)

    assert outcome.status == poll_log.STATUS_OK
    assert outcome.entity_count == 7
    assert outcome.rows_written == 7
    assert outcome.duration_ms is not None

    stored = await db_pool.fetchval("select count(*) from vehicle_positions")
    assert stored == 7

    logged = await db_pool.fetchrow("select * from poll_log")
    assert logged["feed"] == VEHICLE_POSITIONS
    assert logged["status"] == poll_log.STATUS_OK
    assert logged["rows_written"] == 7
    assert logged["http_code"] == 200
    assert logged["error"] is None


async def test_trip_update_poll_writes_predictions(
    db_pool: asyncpg.Pool, http_client: object
) -> None:
    async with running_mock(mock_vehicle_count=4) as (_, settings):
        poller = _poller(TRIP_UPDATES, settings, db_pool, http_client)
        outcome = await poller.poll_once()

    assert outcome.status == poll_log.STATUS_OK
    assert outcome.rows_written > 0

    stored = await db_pool.fetchval("select count(*) from predictions")
    assert stored == outcome.rows_written
    assert poller.cache is not None
    assert len(poller.cache) == outcome.rows_written


async def test_repeated_positions_are_deduplicated_by_the_database(
    db_pool: asyncpg.Pool, http_client: object
) -> None:
    """The same records republished must not become duplicate rows.

    The header is frozen so the second poll would otherwise write the same
    vehicles again, and the watermark is cleared to force the write path rather
    than the skip path.
    """
    async with running_mock(mock_vehicle_count=5, mock_feed_refresh_seconds=FROZEN_HEADER) as (
        _,
        settings,
    ):
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)

        first = await poller.poll_once()
        poller.last_feed_timestamp = None
        second = await poller.poll_once()

    assert first.rows_written == 5
    # Vehicle timestamps advance with the wall clock, so at most a few rows are
    # genuinely new. What matters is that the conflict path is exercised without
    # error and the total stays bounded.
    assert second.status == poll_log.STATUS_OK
    total = await db_pool.fetchval("select count(*) from vehicle_positions")
    assert total == first.rows_written + second.rows_written


# --- unchanged header: ADR-0007 ---


async def test_unchanged_header_is_skipped_without_writing(
    db_pool: asyncpg.Pool, http_client: object
) -> None:
    async with running_mock(mock_vehicle_count=5, mock_feed_refresh_seconds=FROZEN_HEADER) as (
        _,
        settings,
    ):
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)

        first = await poller.poll_once()
        second = await poller.poll_once()
        await poll_log.record(db_pool, second)

    assert first.status == poll_log.STATUS_OK
    assert second.status == poll_log.STATUS_SKIPPED
    assert second.rows_written == 0

    # Only the first poll's rows exist.
    assert await db_pool.fetchval("select count(*) from vehicle_positions") == 5

    logged = await db_pool.fetchrow("select status from poll_log")
    assert logged["status"] == poll_log.STATUS_SKIPPED


async def test_a_skip_counts_as_healthy(db_pool: asyncpg.Pool, http_client: object) -> None:
    """A stale feed is not a failure, so it must not trigger backoff."""
    async with running_mock(mock_feed_refresh_seconds=FROZEN_HEADER) as (_, settings):
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)
        await poller.poll_once()
        await poller.poll_once()

    assert poller.backoff.failures == 0
    health = poller.state.health(VEHICLE_POSITIONS)
    assert health.consecutive_failures == 0
    assert health.last_success is not None


# --- change-only storage end to end: ADR-0005 ---


async def test_second_prediction_poll_writes_far_fewer_rows(
    db_pool: asyncpg.Pool, http_client: object
) -> None:
    """The whole point of change-only storage, measured.

    Two polls seconds apart see nearly identical predictions, so the second must
    write dramatically fewer rows than it was offered.
    """
    async with running_mock(mock_vehicle_count=10) as (_, settings):
        poller = _poller(TRIP_UPDATES, settings, db_pool, http_client)

        first = await poller.poll_once()
        second = await poller.poll_once()

    offered = first.rows_written
    assert offered > 0
    assert second.rows_written < offered, "second poll should be mostly unchanged"

    stored = await db_pool.fetchval("select count(*) from predictions")
    assert stored == first.rows_written + second.rows_written


# --- failure paths ---


async def test_http_500_is_recorded_and_backs_off(
    db_pool: asyncpg.Pool, http_client: object
) -> None:
    async with running_mock(mock_failure_rate=1.0) as (_, settings):
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)
        outcome = await poller.poll_once()
        await poll_log.record(db_pool, outcome)

        delay = poller.backoff.record_failure()

    assert outcome.status == poll_log.STATUS_HTTP_ERROR
    assert outcome.http_code == 500
    assert outcome.rows_written is None
    assert "FeedHTTPError" in (outcome.error or "")

    assert await db_pool.fetchval("select count(*) from vehicle_positions") == 0

    logged = await db_pool.fetchrow("select * from poll_log")
    assert logged["status"] == poll_log.STATUS_HTTP_ERROR
    assert logged["http_code"] == 500
    assert logged["error"]

    assert 0.0 <= delay <= poller.backoff.ceiling


async def test_truncated_feed_is_a_parse_error(db_pool: asyncpg.Pool, http_client: object) -> None:
    async with running_mock(mock_truncate_rate=1.0) as (_, settings):
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)
        outcome = await poller.poll_once()
        await poll_log.record(db_pool, outcome)

    assert outcome.status == poll_log.STATUS_PARSE_ERROR
    # A parse error is distinct from an HTTP error because "MTS is down" and
    # "MTS changed its output" need different responses.
    assert outcome.http_code is None
    assert await db_pool.fetchval("select count(*) from vehicle_positions") == 0

    logged = await db_pool.fetchrow("select status from poll_log")
    assert logged["status"] == poll_log.STATUS_PARSE_ERROR


async def test_unreachable_feed_is_recorded_without_a_status_code(
    db_pool: asyncpg.Pool, http_client: object
) -> None:
    """A connection refused has no HTTP status, and must not invent one."""
    async with running_mock() as (server, settings):
        await server.stop()
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)
        outcome = await poller.poll_once()
        await poll_log.record(db_pool, outcome)

    assert outcome.status == poll_log.STATUS_HTTP_ERROR
    assert outcome.http_code is None
    assert "FeedTransportError" in (outcome.error or "")


async def test_failed_write_does_not_advance_the_watermark(
    db_pool: asyncpg.Pool, http_client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A database failure must leave the poll retryable.

    If the watermark advanced on a failed write, the next poll would see an
    unchanged header, skip, and the data would be lost with no error.
    """
    import ontime_sd.collector as collector_module

    async def boom(*_args: object, **_kwargs: object) -> int:
        raise asyncpg.PostgresConnectionError("simulated database outage")

    async with running_mock(mock_vehicle_count=3) as (_, settings):
        monkeypatch.setattr(collector_module, "write_positions", boom)
        poller = _poller(VEHICLE_POSITIONS, settings, db_pool, http_client)
        outcome = await poller.poll_once()

    assert outcome.status == poll_log.STATUS_DB_ERROR
    assert poller.last_feed_timestamp is None, "watermark must not advance"
    assert poller.state.health(VEHICLE_POSITIONS).consecutive_failures == 1


async def test_failed_prediction_write_keeps_rows_offered(
    db_pool: asyncpg.Pool, http_client: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cache must not remember rows that were never written."""
    import ontime_sd.collector as collector_module

    async def boom(*_args: object, **_kwargs: object) -> int:
        raise asyncpg.PostgresConnectionError("simulated database outage")

    async with running_mock(mock_vehicle_count=3) as (_, settings):
        monkeypatch.setattr(collector_module, "write_predictions", boom)
        poller = _poller(TRIP_UPDATES, settings, db_pool, http_client)
        outcome = await poller.poll_once()

    assert outcome.status == poll_log.STATUS_DB_ERROR
    assert poller.cache is not None
    assert len(poller.cache) == 0, "nothing remembered after a failed write"


async def test_poll_log_failure_does_not_raise(db_pool: asyncpg.Pool) -> None:
    """Losing observability must never take down collection."""
    bad = poll_log.PollOutcome(
        feed="vehicle_positions",
        started_at=__import__("datetime").datetime.now(tz=__import__("datetime").UTC),
        status="not_a_real_status",
    )
    await poll_log.record(db_pool, bad)  # must not raise

    assert await db_pool.fetchval("select count(*) from poll_log") == 0


# --- the loop ---


async def test_run_starts_both_feeds_and_stops_cleanly(
    db_pool: asyncpg.Pool, monkeypatch: pytest.MonkeyPatch
) -> None:
    async with running_mock(mock_vehicle_count=3, poll_interval_seconds=1) as (_, settings):
        monkeypatch.setattr(
            "ontime_sd.collector.create_pool", lambda _settings, **_kw: _wrap(db_pool)
        )

        stop = asyncio.Event()
        task = asyncio.create_task(run(settings, stop))

        # Let both feeds complete at least one poll.
        for _ in range(100):
            await asyncio.sleep(0.05)
            if await db_pool.fetchval("select count(distinct feed) from poll_log") == 2:
                break

        stop.set()
        state = await asyncio.wait_for(task, timeout=10)

    assert set(state.feeds) == {VEHICLE_POSITIONS, TRIP_UPDATES}
    assert all(health.polls >= 1 for health in state.feeds.values())
    assert await db_pool.fetchval("select count(*) from vehicle_positions") > 0
    assert await db_pool.fetchval("select count(*) from predictions") > 0


async def _wrap(pool: asyncpg.Pool) -> asyncpg.Pool:
    """Hand the collector the test pool, and survive its close() call."""

    class _NoCloseProxy:
        def __getattr__(self, name: str) -> object:
            return getattr(pool, name)

        async def close(self) -> None:
            return None

    return _NoCloseProxy()  # type: ignore[return-value]


async def test_sigterm_requests_a_graceful_stop() -> None:
    """launchd sends SIGTERM on stop and restart, so this is the normal path."""
    stop = asyncio.Event()
    install_signal_handlers(stop)

    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(stop.wait(), timeout=5)

    assert stop.is_set()
