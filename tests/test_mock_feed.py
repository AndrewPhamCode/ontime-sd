"""Mock feed server behavior.

These tests are what make the rest of Phase 1 testable: they establish that the
mock emits genuinely valid GTFS-Realtime, and that each injected failure mode
actually produces the failure the collector needs to survive.

No test here depends on randomness. Failures are forced with query parameters.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from google.protobuf import text_format
from google.transit import gtfs_realtime_pb2 as gtfs_rt

from ontime_sd.mock.server import MockFeedServer, _parse_path
from ontime_sd.mock.simulator import SHAPE_MTS, SHAPE_RICH, Simulator
from tests.conftest import make_settings

VEHICLE_COUNT = 6


def simulator_with_bus_mid_route(vehicle_count: int = VEHICLE_COUNT) -> Simulator:
    """A simulator whose first bus is a quarter of the way along its route.

    Vehicle position is derived from wall clock time, so a plain Simulator puts
    the first bus wherever the clock happens to leave it. When that is the final
    stop, only one stop lies ahead and a test asserting several upcoming
    predictions fails for reasons that have nothing to do with the code. Pinning
    the epoch relative to now fixes the position without freezing time.
    """
    probe = Simulator(vehicle_count=vehicle_count)
    bus = probe.vehicles[0]
    target_m = probe.route.length_m * 0.25
    elapsed_s = (target_m - bus.offset_m) / bus.speed_mps
    return Simulator(
        vehicle_count=vehicle_count,
        epoch=datetime.now(UTC) - timedelta(seconds=elapsed_s),
    )


@pytest_asyncio.fixture(loop_scope="session")
async def mock_server() -> AsyncIterator[tuple[MockFeedServer, str]]:
    server = MockFeedServer(
        make_settings(mock_vehicle_count=VEHICLE_COUNT),
        simulator=simulator_with_bus_mid_route(),
    )
    port = await server.start(0)
    try:
        yield server, server.base_url(port)
    finally:
        await server.stop()


@pytest_asyncio.fixture(loop_scope="session")
async def client() -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(timeout=10) as c:
        yield c


def _parse(content: bytes) -> gtfs_rt.FeedMessage:
    message = gtfs_rt.FeedMessage()
    message.ParseFromString(content)
    return message


# --- path routing ---


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            "/api/api/gtfs_realtime/vehicle-positions-for-agency/MTS.pb",
            ("vehicle_positions", False),
        ),
        ("/api/api/gtfs_realtime/trip-updates-for-agency/MTS.pb", ("trip_updates", False)),
        ("/api/api/gtfs_realtime/trip-updates-for-agency/MTS.pbtext", ("trip_updates", True)),
        ("/api/api/gtfs_realtime/nonsense-for-agency/MTS.pb", None),
        ("/api/api/gtfs_realtime/trip-updates-for-agency/MTS.json", None),
        ("/", None),
    ],
)
def test_path_parsing_matches_the_mts_url_shape(
    path: str, expected: tuple[str, bool] | None
) -> None:
    assert _parse_path(path) == expected


# --- valid output ---


async def test_vehicle_positions_are_valid_protobuf(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    _, base = mock_server
    response = await client.get(f"{base}/vehicle-positions-for-agency/MTS.pb")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/x-protobuf"

    message = _parse(response.content)
    assert message.header.gtfs_realtime_version == "2.0"
    assert message.header.timestamp > 0
    assert len(message.entity) == VEHICLE_COUNT

    position = message.entity[0].vehicle
    assert position.vehicle.id.startswith("MOCK_BUS_")
    assert position.trip.trip_id
    # Inside the San Diego area, so anything plotted on a map is plausible.
    assert 32.0 < position.position.latitude < 33.5
    assert -118.0 < position.position.longitude < -116.5
    # The real feed sends neither of these, so the default shape must not
    # either. See ADR-0034.
    assert not position.HasField("current_stop_sequence")
    assert not position.position.HasField("bearing")


async def test_trip_updates_carry_per_stop_predictions(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    _, base = mock_server
    response = await client.get(f"{base}/trip-updates-for-agency/MTS.pb")

    message = _parse(response.content)
    assert len(message.entity) == VEHICLE_COUNT

    update = message.entity[0].trip_update
    assert len(update.stop_time_update) > 1
    first = update.stop_time_update[0]
    assert first.arrival.HasField("time")
    assert first.stop_id.startswith("MOCK_STOP_")

    # Later stops are predicted later than earlier ones.
    times = [stu.arrival.time for stu in update.stop_time_update]
    assert times == sorted(times)


async def test_pbtext_is_human_readable_and_parses_back(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    """The charter says to fetch .pbtext first when the key arrives."""
    _, base = mock_server
    response = await client.get(f"{base}/vehicle-positions-for-agency/MTS.pbtext")

    assert response.status_code == 200
    assert "gtfs_realtime_version" in response.text

    reparsed = text_format.Parse(response.text, gtfs_rt.FeedMessage())
    assert len(reparsed.entity) == VEHICLE_COUNT


async def test_default_shape_omits_stop_sequence_like_the_real_feed() -> None:
    """The real MTS feed never sends stop_sequence. The mock must not either.

    When it did, every prediction was dropped against the real feed while tests
    stayed green. See ADR-0034.
    """
    # Pinned: a plain Simulator leaves the bus wherever the wall clock puts it,
    # and at the final stop only one stop lies ahead, so the assertion below
    # fails for reasons unrelated to the feed shape. Same flake as ADR-0035.
    server = MockFeedServer(
        make_settings(mock_vehicle_count=3),
        simulator=simulator_with_bus_mid_route(3),
    )
    port = await server.start(0)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            response = await c.get(f"{server.base_url(port)}/trip-updates-for-agency/MTS.pb")
        message = _parse(response.content)

        update = message.entity[0].trip_update
        assert len(update.stop_time_update) > 1, "per stop, not a single delay"
        for stop_time in update.stop_time_update:
            assert not stop_time.HasField("stop_sequence")
            assert stop_time.stop_id
            assert stop_time.arrival.HasField("time")
        assert not update.trip.HasField("start_date")
    finally:
        await server.stop()


async def test_rich_shape_populates_everything_optional() -> None:
    """Keeps the parser's fallback paths under test."""
    # Pinned for the same reason: stop_time_update[0] does not exist once the
    # bus has passed its last stop.
    server = MockFeedServer(
        make_settings(mock_vehicle_count=3, mock_feed_shape=SHAPE_RICH),
        simulator=simulator_with_bus_mid_route(3),
    )
    port = await server.start(0)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            response = await c.get(f"{server.base_url(port)}/trip-updates-for-agency/MTS.pb")
        update = _parse(response.content).entity[0].trip_update

        assert update.stop_time_update[0].HasField("stop_sequence")
        assert update.trip.HasField("start_date")
    finally:
        await server.stop()


def test_simulator_rejects_an_unknown_shape() -> None:
    with pytest.raises(ValueError, match="unknown shape"):
        Simulator(vehicle_count=2).trip_updates(datetime.now(tz=UTC), shape="vibes")


# --- movement and determinism ---


def test_buses_move_over_time() -> None:
    simulator = Simulator(vehicle_count=3)
    now = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)

    before = simulator.vehicle_positions(now).entity[0].vehicle.position
    after = simulator.vehicle_positions(now + timedelta(minutes=1)).entity[0].vehicle.position

    assert (before.latitude, before.longitude) != (after.latitude, after.longitude)


def test_same_instant_produces_identical_bytes() -> None:
    """Determinism is what lets tests assert on feed contents."""
    simulator = Simulator(vehicle_count=4)
    now = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)

    assert (
        simulator.vehicle_positions(now).SerializeToString()
        == simulator.vehicle_positions(now).SerializeToString()
    )


def test_a_fresh_simulator_agrees_with_an_older_one() -> None:
    """Restarting the mock must not change the feed for a given instant."""
    now = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)
    first = Simulator(vehicle_count=4).vehicle_positions(now).SerializeToString()
    second = Simulator(vehicle_count=4).vehicle_positions(now).SerializeToString()
    assert first == second


def test_predictions_drift_enough_to_exercise_change_only_storage() -> None:
    """Some revisions must cross the 30 second threshold and some must not.

    If every poll changed predictions, change-only storage would look useless.
    If none did, it would look perfect. Both would be the mock lying.
    """
    simulator = Simulator(vehicle_count=8)
    start = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)

    def arrivals(at: datetime) -> dict[tuple[str, int], int]:
        message = simulator.trip_updates(at, shape=SHAPE_MTS)
        return {
            (entity.trip_update.trip.trip_id, stu.stop_id): stu.arrival.time
            for entity in message.entity
            for stu in entity.trip_update.stop_time_update
        }

    baseline = arrivals(start)
    # Well beyond one drift period, so revisions have definitely happened.
    later = arrivals(start + timedelta(seconds=300))

    shared = baseline.keys() & later.keys()
    assert shared, "expected overlapping stop predictions between polls"

    shifts = [abs(later[key] - baseline[key]) for key in shared]
    assert any(shift >= 30 for shift in shifts), "no revision crossed the threshold"
    assert any(shift < 30 for shift in shifts), "every revision crossed the threshold"


# --- feed header quantization ---


def test_header_timestamp_is_stable_within_a_refresh_period() -> None:
    server = MockFeedServer(make_settings(mock_feed_refresh_seconds=30))
    base = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)

    first = server.feed_timestamp(base)
    within = server.feed_timestamp(base + timedelta(seconds=20))
    after = server.feed_timestamp(base + timedelta(seconds=45))

    assert first == within, "header must repeat within the refresh period"
    assert after > first, "header must advance once the period elapses"


async def test_frozen_query_pins_the_header(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    """Lets a test hold the header still without waiting on wall clock time."""
    _, base = mock_server
    url = f"{base}/vehicle-positions-for-agency/MTS.pb?frozen=1"

    first = _parse((await client.get(url)).content).header.timestamp
    second = _parse((await client.get(url)).content).header.timestamp
    assert first == second


# --- failure injection ---


async def test_forced_status_code_is_returned(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    _, base = mock_server
    for status in (500, 503, 404):
        response = await client.get(f"{base}/vehicle-positions-for-agency/MTS.pb?fail={status}")
        assert response.status_code == status


async def test_truncated_payload_fails_to_parse(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    """A parse_error must be reachable, since poll_log has a status for it."""
    _, base = mock_server
    response = await client.get(f"{base}/vehicle-positions-for-agency/MTS.pb?truncate=1")

    assert response.status_code == 200
    assert response.content
    with pytest.raises(Exception, match=r"(?i)error|truncat|wire"):
        _parse(response.content)


async def test_slow_response_actually_delays(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    _, base = mock_server
    started = time.monotonic()
    response = await client.get(f"{base}/vehicle-positions-for-agency/MTS.pb?slow=0.3")
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed >= 0.3


async def test_unknown_path_is_a_404(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    _, base = mock_server
    assert (await client.get(f"{base}/not-a-feed/MTS.pb")).status_code == 404


async def test_non_get_is_rejected(
    mock_server: tuple[MockFeedServer, str], client: httpx.AsyncClient
) -> None:
    _, base = mock_server
    response = await client.post(f"{base}/vehicle-positions-for-agency/MTS.pb")
    assert response.status_code == 400


# --- key handling ---


async def test_key_is_required_when_configured() -> None:
    """Exercises the auth path before the real key exists. See ADR-0012."""
    server = MockFeedServer(make_settings(mts_api_key="test-key", mock_vehicle_count=2))
    port = await server.start(0)
    base = server.base_url(port)
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            missing = await c.get(f"{base}/vehicle-positions-for-agency/MTS.pb")
            wrong = await c.get(f"{base}/vehicle-positions-for-agency/MTS.pb?key=nope")
            correct = await c.get(f"{base}/vehicle-positions-for-agency/MTS.pb?key=test-key")

        assert missing.status_code == 401
        assert wrong.status_code == 401
        assert correct.status_code == 200
    finally:
        await server.stop()
