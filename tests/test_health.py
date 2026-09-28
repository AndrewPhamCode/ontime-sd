"""The health endpoint.

This answers exactly one question: should a supervisor restart this process. The
tests pin that it says yes only when collection has genuinely stopped, because a
false unhealthy under launchd KeepAlive means a restart loop that throws away the
prediction cache each time.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio

from ontime_sd.collector import CollectorState
from ontime_sd.config import TRIP_UPDATES, VEHICLE_POSITIONS
from ontime_sd.health import HealthServer, build_report
from tests.conftest import make_settings

NOW = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)
STALE_AFTER = 300


def _state(**last_success: datetime | None) -> CollectorState:
    state = CollectorState(started_at=NOW - timedelta(minutes=10))
    for feed, ts in last_success.items():
        state.health(feed).last_success = ts
    return state


# --- report contents, no socket needed ---


def test_no_successful_poll_yet_is_unhealthy() -> None:
    """At startup nothing has succeeded, so the process is not ready."""
    healthy, report = build_report(CollectorState(), STALE_AFTER, now=NOW)

    assert healthy is False
    assert report["status"] == "stale"


def test_recent_success_is_healthy() -> None:
    state = _state(vehicle_positions=NOW - timedelta(seconds=30))
    healthy, report = build_report(state, STALE_AFTER, now=NOW)

    assert healthy is True
    assert report["status"] == "ok"


def test_one_healthy_feed_is_enough() -> None:
    """See ADR-0022: a single failing feed is not a reason to be restarted."""
    state = _state(
        vehicle_positions=NOW - timedelta(seconds=10),
        trip_updates=NOW - timedelta(hours=3),
    )
    healthy, report = build_report(state, STALE_AFTER, now=NOW)

    assert healthy is True
    feeds = report["feeds"]
    assert isinstance(feeds, dict)
    # The struggling feed is still reported as stale, for the runbook to act on.
    assert feeds[TRIP_UPDATES]["stale"] is True
    assert feeds[VEHICLE_POSITIONS]["stale"] is False


def test_every_feed_stale_is_unhealthy() -> None:
    state = _state(
        vehicle_positions=NOW - timedelta(seconds=STALE_AFTER + 1),
        trip_updates=NOW - timedelta(seconds=STALE_AFTER + 60),
    )
    healthy, _ = build_report(state, STALE_AFTER, now=NOW)
    assert healthy is False


def test_staleness_boundary_is_exclusive() -> None:
    """Exactly at the threshold is still healthy, one second past is not."""
    at = _state(vehicle_positions=NOW - timedelta(seconds=STALE_AFTER))
    past = _state(vehicle_positions=NOW - timedelta(seconds=STALE_AFTER + 1))

    assert build_report(at, STALE_AFTER, now=NOW)[0] is True
    assert build_report(past, STALE_AFTER, now=NOW)[0] is False


def test_report_includes_what_an_operator_needs() -> None:
    state = _state(vehicle_positions=NOW - timedelta(seconds=45))
    health = state.health(VEHICLE_POSITIONS)
    health.polls = 120
    health.rows_written = 3400
    health.consecutive_failures = 2

    _, report = build_report(state, STALE_AFTER, now=NOW)

    assert report["uptime_seconds"] == 600.0
    assert report["stale_after_seconds"] == STALE_AFTER
    feed = report["feeds"][VEHICLE_POSITIONS]
    assert feed["seconds_since_success"] == 45.0
    assert feed["polls"] == 120
    assert feed["rows_written"] == 3400
    assert feed["consecutive_failures"] == 2


# --- over HTTP ---


@pytest_asyncio.fixture(loop_scope="session")
async def served() -> AsyncIterator[tuple[CollectorState, str]]:
    state = CollectorState()
    server = HealthServer(state, make_settings(health_stale_after_seconds=STALE_AFTER))
    port = await server.start(0)
    try:
        yield state, f"http://127.0.0.1:{port}"
    finally:
        await server.stop()


async def test_endpoint_returns_503_before_any_poll_succeeds(
    served: tuple[CollectorState, str],
) -> None:
    _, base = served
    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{base}/healthz")

    assert response.status_code == 503
    assert response.headers["content-type"] == "application/json"
    assert json.loads(response.text)["status"] == "stale"


async def test_endpoint_returns_200_once_a_feed_succeeds(
    served: tuple[CollectorState, str],
) -> None:
    state, base = served
    state.health(VEHICLE_POSITIONS).last_success = datetime.now(tz=UTC)

    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{base}/healthz")

    assert response.status_code == 200
    assert json.loads(response.text)["status"] == "ok"


async def test_endpoint_goes_unhealthy_again_when_collection_stops(
    served: tuple[CollectorState, str],
) -> None:
    """The case that matters: the process is alive but no longer collecting."""
    state, base = served
    state.health(VEHICLE_POSITIONS).last_success = datetime.now(tz=UTC) - timedelta(
        seconds=STALE_AFTER + 30
    )

    async with httpx.AsyncClient(timeout=5) as client:
        response = await client.get(f"{base}/healthz")

    assert response.status_code == 503


async def test_unknown_path_is_a_404(served: tuple[CollectorState, str]) -> None:
    _, base = served
    async with httpx.AsyncClient(timeout=5) as client:
        assert (await client.get(f"{base}/metrics")).status_code == 404


async def test_endpoint_survives_repeated_probes(
    served: tuple[CollectorState, str],
) -> None:
    """A probe every few seconds forever must not leak or degrade."""
    state, base = served
    state.health(VEHICLE_POSITIONS).last_success = datetime.now(tz=UTC)

    async with httpx.AsyncClient(timeout=5) as client:
        for _ in range(25):
            assert (await client.get(f"{base}/healthz")).status_code == 200


@pytest.mark.parametrize("path", ["/healthz", "/"])
async def test_both_accepted_paths_work(served: tuple[CollectorState, str], path: str) -> None:
    _, base = served
    async with httpx.AsyncClient(timeout=5) as client:
        assert (await client.get(f"{base}{path}")).status_code in (200, 503)
