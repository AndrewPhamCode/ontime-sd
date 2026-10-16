"""Trip planning: which stop, which bus, and when to be there. See ADR-0052.

The pure arithmetic is tested directly. The endpoint is tested against a real
Postgres with a tiny timetable laid out around UCSD, with every time placed
relative to now so the tests do not depend on the date they run.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, time, timedelta

import asyncpg
import httpx
import pytest
import pytest_asyncio

from ontime_sd import trip_plan as tp
from ontime_sd.api import DIRECT_RIDES_SQL, create_app
from ontime_sd.config import SERVICE_TZ
from tests.conftest import make_settings

VERSION = "c" * 64
ROUTE = "201"

# The rider starts at ORIGIN and wants DEST, about 2.2 km north.
ORIGIN = (32.8800, -117.2340)
DEST = (32.9000, -117.2340)

# Stop A is about 200 m north of the origin, A2 about 400 m, and B about 150 m
# south of the destination.
STOPS = {
    "A": ("Gilman & Myers", 32.8818, -117.2340),
    "A2": ("Gilman & Villa La Jolla", 32.8836, -117.2340),
    "B": ("Voigt & Campus Point", 32.89865, -117.2340),
}


# --- pure arithmetic ---------------------------------------------------------


def test_haversine_matches_a_degree_of_latitude() -> None:
    assert tp.haversine_m(32.0, -117.0, 33.0, -117.0) == pytest.approx(111_195, rel=1e-3)


def test_walk_time_includes_the_detour_factor() -> None:
    # 130 m of straight line is 169 m of street, which is 130 s at 1.3 m/s.
    assert tp.walk_seconds(130) == 130


def test_nearest_stops_cuts_the_corners_of_the_box_and_sorts() -> None:
    lat, lon = ORIGIN
    _, lat_hi, _, lon_hi = tp.bounding_box(lat, lon, 500)
    candidates = [
        ("far", None, 32.8840, -117.2340),  # about 445 m, inside
        ("near", None, 32.8810, -117.2340),  # about 110 m
        ("corner", None, lat_hi, lon_hi),  # inside the box, outside the circle
    ]
    found = tp.nearest_stops(lat, lon, candidates, 500)
    assert [s.stop_id for s in found] == ["near", "far"]


def test_nearest_stops_respects_the_limit() -> None:
    candidates = [(f"s{i}", None, 32.8800 + i * 0.0001, -117.2340) for i in range(20)]
    assert len(tp.nearest_stops(*ORIGIN, candidates, 800, limit=5)) == 5


def test_margin_against_mts_is_the_early_tail_itself() -> None:
    assert tp.choose_margin(tp.BASIS_MTS, (100, 50.0, 240.0), 30) == (240, tp.MARGIN_MEASURED)


def test_margin_against_our_estimate_is_the_spread_above_the_mean() -> None:
    """The correction already removed the mean, so only the spread is left."""
    assert tp.choose_margin(tp.BASIS_CORRECTED, (100, 50.0, 240.0), 30) == (
        190,
        tp.MARGIN_MEASURED,
    )


def test_margin_never_goes_below_the_floor() -> None:
    margin, _ = tp.choose_margin(tp.BASIS_CORRECTED, (100, 50.0, 60.0), 30)
    assert margin == tp.MIN_MARGIN_SECONDS


@pytest.mark.parametrize(
    ("basis", "tail"),
    [
        (tp.BASIS_SCHEDULED, (100, 50.0, 240.0)),
        (tp.BASIS_MTS, None),
        (tp.BASIS_MTS, (5, 50.0, 240.0)),
    ],
)
def test_without_a_measured_tail_the_margin_is_the_stated_default(
    basis: str, tail: tuple | None
) -> None:
    assert tp.choose_margin(basis, tail, 30) == (tp.DEFAULT_MARGIN_SECONDS, tp.MARGIN_DEFAULT)


def test_timing_works_back_from_the_departure() -> None:
    departs = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)
    t = tp.timing(departs, departs + timedelta(minutes=12), 120, 300, 90)
    assert t.be_at_stop_by == departs - timedelta(minutes=2)
    assert t.leave_by == departs - timedelta(minutes=7)
    assert t.arrive_at_destination == departs + timedelta(minutes=13, seconds=30)


def test_arrive_by_prefers_leaving_later() -> None:
    base = datetime(2026, 10, 15, 17, 0, tzinfo=UTC)
    early = tp.timing(base, base + timedelta(minutes=10), 60, 60, 60)
    late = tp.timing(base + timedelta(minutes=15), base + timedelta(minutes=25), 60, 60, 60)
    deadline = base + timedelta(hours=1)
    assert tp.rank_key(late, deadline) < tp.rank_key(early, deadline)
    assert tp.rank_key(early, None) < tp.rank_key(late, None)


# --- the endpoint, against Postgres -------------------------------------------


@pytest_asyncio.fixture(loop_scope="session")
async def clean(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    async def wipe() -> None:
        for table in ("predictions", "prediction_errors", "feed_versions"):
            await db_pool.execute(f"truncate {table} cascade")

    await wipe()
    yield
    await wipe()


@pytest_asyncio.fixture(loop_scope="session")
async def client(db_pool: asyncpg.Pool) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app(make_settings(), pool=db_pool)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _today() -> date:
    return datetime.now(tz=UTC).astimezone(SERVICE_TZ).date()


def _seconds_from_now(minutes: float, service_day: date) -> int:
    """GTFS seconds past service_day's midnight for a time `minutes` from now."""
    midnight = datetime.combine(service_day, time(0), tzinfo=SERVICE_TZ)
    return int((datetime.now(tz=UTC) + timedelta(minutes=minutes) - midnight).total_seconds())


async def _seed_network(pool: asyncpg.Pool, days: tuple[date, ...] | None = None) -> None:
    await pool.execute(
        "insert into feed_versions (feed_version, source_url, loaded_at) "
        "values ($1, 'https://example.test/f.zip', now())",
        VERSION,
    )
    await pool.execute(
        "insert into routes (feed_version, route_id, route_short_name, route_type) "
        "values ($1, $2, '201', 3)",
        VERSION,
        ROUTE,
    )
    for stop_id, (name, lat, lon) in STOPS.items():
        await pool.execute(
            "insert into stops (feed_version, stop_id, stop_name, stop_lat, stop_lon, "
            "location_type) values ($1, $2, $3, $4, $5, 0)",
            VERSION,
            stop_id,
            name,
            lat,
            lon,
        )
    for day in days or (_today(),):
        await pool.execute(
            "insert into service_dates (feed_version, service_date, service_id) "
            "values ($1, $2, 'svc')",
            VERSION,
            day,
        )


async def _trip(
    pool: asyncpg.Pool,
    trip_id: str,
    stops: list[tuple[str, int]],
) -> None:
    """A trip calling at (stop_id, seconds past midnight) in order."""
    await pool.execute(
        "insert into trips (feed_version, trip_id, route_id, service_id, trip_headsign) "
        "values ($1, $2, $3, 'svc', 'UTC')",
        VERSION,
        trip_id,
        ROUTE,
    )
    for sequence, (stop_id, seconds) in enumerate(stops, start=1):
        await pool.execute(
            "insert into stop_times (feed_version, trip_id, stop_sequence, stop_id, "
            "arrival_seconds, departure_seconds) values ($1, $2, $3, $4, $5, $5)",
            VERSION,
            trip_id,
            sequence,
            stop_id,
            seconds,
        )


async def _plan(client: httpx.AsyncClient, **extra: object) -> dict:
    params: dict[str, object] = {
        "from_lat": ORIGIN[0],
        "from_lon": ORIGIN[1],
        "to_lat": DEST[0],
        "to_lon": DEST[1],
        **extra,
    }
    response = await client.get("/api/trip-plan", params=params)
    assert response.status_code == 200, response.text
    return response.json()


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)


@pytest.mark.usefixtures("clean")
async def test_a_direct_ride_names_the_stop_and_when_to_be_there(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed_network(db_pool)
    day = _today()
    await _trip(
        db_pool, "t1", [("A", _seconds_from_now(20, day)), ("B", _seconds_from_now(32, day))]
    )

    body = await _plan(client)
    assert body["message"] is None
    option = body["options"][0]
    assert option["board"]["stop_id"] == "A"
    assert option["alight"]["stop_id"] == "B"
    assert option["route_short_name"] == "201"
    assert option["departure_basis"] == "scheduled"
    assert option["margin_basis"] == "default"

    departs = _at(option["departs_at"])
    be_there = _at(option["be_at_stop_by"])
    assert departs - be_there == timedelta(seconds=tp.DEFAULT_MARGIN_SECONDS)
    assert be_there - _at(option["leave_by"]) == timedelta(seconds=option["walk_to_stop_seconds"])
    assert _at(option["arrive_at_destination"]) > _at(option["arrives_at"])


@pytest.mark.usefixtures("clean")
async def test_a_bus_going_the_other_way_is_not_a_ride(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """Both stops on one trip is not enough: the destination must come after."""
    await _seed_network(db_pool)
    day = _today()
    await _trip(
        db_pool, "rev", [("B", _seconds_from_now(20, day)), ("A", _seconds_from_now(32, day))]
    )

    body = await _plan(client)
    assert body["options"] == []
    assert "No direct ride" in body["message"]


@pytest.mark.usefixtures("clean")
async def test_leave_now_skips_a_bus_you_cannot_walk_to_in_time(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed_network(db_pool)
    day = _today()
    # A is about 200 m away, so roughly a 3.5 minute walk plus the margin.
    await _trip(
        db_pool, "soon", [("A", _seconds_from_now(2, day)), ("B", _seconds_from_now(14, day))]
    )
    await _trip(
        db_pool, "next", [("A", _seconds_from_now(15, day)), ("B", _seconds_from_now(27, day))]
    )

    body = await _plan(client)
    assert [o["trip_id"] for o in body["options"]] == ["next"]


@pytest.mark.usefixtures("clean")
async def test_arrive_by_takes_the_latest_bus_that_still_makes_it(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed_network(db_pool)
    day = _today()
    deadline = datetime.now(tz=UTC) + timedelta(minutes=60)
    # B is about 150 m from the destination, a 2.5 minute walk, so "late" arrives
    # at the stop before the deadline but at the door after it.
    for trip_id, board, alight in (("early", 15, 30), ("good", 35, 50), ("late", 45, 59)):
        await _trip(
            db_pool,
            trip_id,
            [("A", _seconds_from_now(board, day)), ("B", _seconds_from_now(alight, day))],
        )

    body = await _plan(client, arrive_by=deadline.isoformat())
    assert body["mode"] == "arrive_by"
    assert [o["trip_id"] for o in body["options"]] == ["good", "early"]
    for option in body["options"]:
        assert _at(option["arrive_at_destination"]) <= deadline


@pytest.mark.usefixtures("clean")
async def test_a_trip_from_yesterdays_service_day_after_midnight_is_found(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """A 12:30am bus may be yesterday's trip at 24:30:00."""
    yesterday = _today() - timedelta(days=1)
    await _seed_network(db_pool, days=(yesterday,))
    board = _seconds_from_now(20, yesterday)
    assert board > 86_400
    await _trip(db_pool, "owl", [("A", board), ("B", _seconds_from_now(32, yesterday))])

    body = await _plan(client)
    assert [o["trip_id"] for o in body["options"]] == ["owl"]


@pytest.mark.usefixtures("clean")
async def test_one_trip_past_several_nearby_stops_is_one_option(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed_network(db_pool)
    day = _today()
    await _trip(
        db_pool,
        "t1",
        [
            ("A2", _seconds_from_now(18, day)),
            ("A", _seconds_from_now(20, day)),
            ("B", _seconds_from_now(32, day)),
        ],
    )

    body = await _plan(client)
    assert len(body["options"]) == 1
    # The nearer stop is the shorter trip door to door.
    assert body["options"][0]["board"]["stop_id"] == "A"


async def _predict(pool: asyncpg.Pool, trip_id: str, sequence: int, minutes: float, age: float = 0):
    await pool.execute(
        """
        insert into predictions (start_date, trip_id, stop_sequence, observed_at,
                                 stop_id, route_id, arrival_time)
        values ($1, $2, $3, now() - ($4 * interval '1 minute'), null, $5,
                now() + ($6 * interval '1 minute'))
        """,
        _today(),
        trip_id,
        sequence,
        age,
        ROUTE,
        minutes,
    )


@pytest.mark.usefixtures("clean")
async def test_a_live_prediction_replaces_the_schedule(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed_network(db_pool)
    day = _today()
    await _trip(
        db_pool, "t1", [("A", _seconds_from_now(20, day)), ("B", _seconds_from_now(32, day))]
    )
    await _predict(db_pool, "t1", 1, 25)

    option = (await _plan(client))["options"][0]
    assert option["departure_basis"] == "mts"
    assert _at(option["departs_at"]) - _at(option["scheduled_departure"]) > timedelta(minutes=4)
    # No live time at the alighting stop, so the boarding delay carries forward and
    # the scheduled 12 minute ride is kept rather than the scheduled arrival.
    ride = _at(option["arrives_at"]) - _at(option["departs_at"])
    assert abs(ride - timedelta(minutes=12)) <= timedelta(seconds=2)


@pytest.mark.usefixtures("clean")
async def test_a_stale_prediction_is_not_trusted(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed_network(db_pool)
    day = _today()
    await _trip(
        db_pool, "t1", [("A", _seconds_from_now(20, day)), ("B", _seconds_from_now(32, day))]
    )
    await _predict(db_pool, "t1", 1, 25, age=45)

    option = (await _plan(client))["options"][0]
    assert option["departure_basis"] == "scheduled"


@pytest.mark.usefixtures("clean")
async def test_measured_history_corrects_the_time_and_sets_the_margin(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """Half the buses came on time and half came 400 s early.

    Mean 200, p90 400. The corrected departure is MTS minus 200 s, and the margin
    against it is the spread above the mean, also 200 s.
    """
    await _seed_network(db_pool)
    day = _today()
    await _trip(
        db_pool, "t1", [("A", _seconds_from_now(20, day)), ("B", _seconds_from_now(32, day))]
    )
    await _predict(db_pool, "t1", 1, 25)

    arrived = datetime.now(tz=UTC) - timedelta(days=1)
    for index in range(40):
        error = 0 if index < 20 else 400
        await db_pool.execute(
            """
            insert into prediction_errors (
                start_date, trip_id, stop_sequence, horizon_minutes, source,
                feed_version, stop_id, route_id, arrived_at, predicted_arrival,
                predicted_at, error_seconds, abs_error_seconds, ping_gap_seconds,
                service_minute, is_weekend, has_all_horizons)
            values ($1, $2, 1, 10, 'mts', $3, 'A', $4, $5::timestamptz,
                    $5::timestamptz + ($6 * interval '1 second'),
                    $5::timestamptz - interval '10 minutes',
                    $6, abs($6), 30, 720, false, true)
            """,
            day - timedelta(days=1),
            f"hist-{index}",
            VERSION,
            ROUTE,
            arrived,
            error,
        )

    option = (await _plan(client))["options"][0]
    assert option["departure_basis"] == "corrected"
    assert option["margin_basis"] == "measured"
    assert option["margin_seconds"] == 200


@pytest.mark.usefixtures("clean")
async def test_no_stop_in_reach_says_so(client: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    await _seed_network(db_pool)
    body = await _plan(client, to_lat=33.2, to_lon=-117.2)
    assert body["options"] == []
    assert "destination" in body["message"]


@pytest.mark.usefixtures("clean")
async def test_arrive_by_in_the_past_is_rejected(client: httpx.AsyncClient) -> None:
    past = datetime.now(tz=UTC) - timedelta(minutes=5)
    response = await client.get(
        "/api/trip-plan",
        params={
            "from_lat": ORIGIN[0],
            "from_lon": ORIGIN[1],
            "to_lat": DEST[0],
            "to_lon": DEST[1],
            "arrive_by": past.isoformat(),
        },
    )
    assert response.status_code == 422


async def test_direct_rides_are_found_through_indexes(conn: asyncpg.Connection) -> None:
    """Neither side of the stop_times self join may scan the timetable.

    On the real feed the planner boards through stop_times_stop_idx and alights
    through the primary key (24 ms, ADR-0052). On these empty tables it may reach
    the same rows from the calendar side instead, so the property pinned is the
    absence of a sequential scan rather than one particular index.
    """
    await conn.execute("set local enable_seqscan to off")
    now = datetime.now(tz=UTC)
    rows = await conn.fetch(
        f"explain (format text) {DIRECT_RIDES_SQL}",
        VERSION,
        ["A"],
        [200],
        ["B"],
        [150],
        [_today()],
        now,
        now + timedelta(minutes=90),
        None,
        40,
    )
    plan = "\n".join(row[0] for row in rows)
    assert "Seq Scan on stop_times" not in plan, plan
