"""Shared fixtures.

Tests run against a real Postgres, never a mock or SQLite. The behavior under
test is Postgres specific: ON CONFLICT DO NOTHING semantics, composite primary
key enforcement, check constraints, and timestamptz handling. A fake would only
assert that the code called the functions the test author expected. See
DESIGN.md ADR-0016.

Each test session creates a throwaway database, migrates it, and drops it.
Each test runs inside a transaction that is rolled back, so tests are isolated
without paying to recreate the schema every time.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit, urlunsplit

import asyncpg
import pytest
import pytest_asyncio

from ontime_sd.config import Settings
from ontime_sd.db import run_migrations

# In CI a database is guaranteed by the service container, so an unreachable
# database is a real failure and must not be silently skipped. Locally, Docker
# may simply not be running yet, and skipping with a clear message is more
# useful than a wall of connection errors.
_REQUIRE_DB = bool(os.environ.get("CI"))


def _maintenance_url(database_url: str) -> str:
    """Same server, but pointing at the postgres database.

    A connection cannot create the database it is connected to, so CREATE
    DATABASE has to be issued from somewhere else.
    """
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path="/postgres"))


def _with_database(database_url: str, name: str) -> str:
    parts = urlsplit(database_url)
    return urlunsplit(parts._replace(path=f"/{name}"))


def make_settings(**overrides: object) -> Settings:
    """Settings with every field populated, for tests that need no environment.

    Kept here so a new setting only has to be defaulted in one place.
    """
    base: dict[str, object] = {
        "database_url": "postgresql://u:p@localhost:5433/db",
        "mts_api_key": "",
        "mts_feed_base_url": "http://localhost:8081/api/api/gtfs_realtime",
        "gtfs_static_url": "https://example.test/google_transit.zip",
        "poll_interval_seconds": 30,
        "prediction_change_threshold_seconds": 30,
        "backoff_base_seconds": 1.0,
        "backoff_max_seconds": 300.0,
        # Ephemeral, so concurrent or repeated test runs cannot collide.
        "health_port": 0,
        "health_stale_after_seconds": 300,
        "log_level": "INFO",
        "mock_port": 0,
        "mock_vehicle_count": 6,
        "mock_failure_rate": 0.0,
        "mock_slow_rate": 0.0,
        "mock_slow_seconds": 5.0,
        "mock_truncate_rate": 0.0,
        "mock_feed_refresh_seconds": 30,
        "mock_trip_update_style": "per_stop",
    }
    base.update(overrides)
    return Settings(**base)  # type: ignore[arg-type]


@pytest.fixture(scope="session")
def settings() -> Settings:
    """Settings for tests, defaulting to the compose database."""
    os.environ.setdefault("DATABASE_URL", "postgresql://ontime:ontime@localhost:5433/ontime_sd")
    return Settings.from_env()


@pytest_asyncio.fixture(scope="session", loop_scope="session")
async def db_pool(settings: Settings) -> AsyncIterator[asyncpg.Pool]:
    """A migrated, throwaway database for the whole session."""
    db_name = f"ontime_test_{uuid.uuid4().hex[:12]}"
    maintenance_url = _maintenance_url(settings.database_url)

    try:
        admin = await asyncpg.connect(maintenance_url, timeout=5)
    except (OSError, asyncpg.PostgresError) as exc:
        message = (
            f"cannot reach Postgres at {_redact(maintenance_url)}: {exc}. "
            "Start it with `make up` (requires Docker Desktop running)."
        )
        if _REQUIRE_DB:
            pytest.fail(message)
        pytest.skip(message, allow_module_level=True)

    try:
        await admin.execute(f'create database "{db_name}"')
    finally:
        await admin.close()

    test_url = _with_database(settings.database_url, db_name)
    pool = await asyncpg.create_pool(test_url, min_size=1, max_size=4)
    try:
        async with pool.acquire() as conn:
            await run_migrations(conn)
        yield pool
    finally:
        await pool.close()
        admin = await asyncpg.connect(maintenance_url, timeout=5)
        try:
            await admin.execute(f'drop database if exists "{db_name}" with (force)')
        finally:
            await admin.close()


@pytest_asyncio.fixture(loop_scope="session")
async def conn(db_pool: asyncpg.Pool) -> AsyncIterator[asyncpg.Connection]:
    """A connection whose work is rolled back when the test ends."""
    async with db_pool.acquire() as connection:
        transaction = connection.transaction()
        await transaction.start()
        try:
            yield connection
        finally:
            await transaction.rollback()


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Strip every setting from the environment so defaults can be tested."""
    for key in list(os.environ):
        if key.startswith(("MTS_", "MOCK_", "POLL_", "BACKOFF_", "HEALTH_", "PREDICTION_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    yield monkeypatch


def _redact(url: str) -> str:
    parts = urlsplit(url)
    if parts.password:
        netloc = parts.netloc.replace(f":{parts.password}", ":REDACTED")
        return urlunsplit(parts._replace(netloc=netloc))
    return url
