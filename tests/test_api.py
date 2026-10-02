"""Phase 6 API.

The property worth protecting is that the page cannot disagree with the SQL: every
endpoint is shaped for one panel and does the aggregation itself, so the frontend
has nothing to get wrong. These tests pin the shapes and the window logic, and they
check the empty-database case because a fresh clone should render rather than
error.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx
import pytest
import pytest_asyncio

from ontime_sd.api import create_app
from tests.conftest import make_settings

DAY_TRAIN = date(2026, 10, 1)
DAY_TEST = date(2026, 10, 2)
ARRIVED = datetime(2026, 10, 2, 19, 0, tzinfo=UTC)
VERSION = "a" * 64


@pytest_asyncio.fixture(loop_scope="session")
async def clean(db_pool: asyncpg.Pool) -> AsyncIterator[None]:
    tables = (
        "prediction_errors",
        "model_runs",
        "arrivals",
        "trips",
        "stops",
        "poll_log",
        "vehicle_positions",
        "feed_versions",
    )

    async def wipe() -> None:
        for table in tables:
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


async def _seed(pool: asyncpg.Pool) -> None:
    await pool.execute(
        "insert into feed_versions (feed_version, source_url, loaded_at) "
        "values ($1, 'https://example.test/f.zip', now())",
        VERSION,
    )
    await pool.execute(
        "insert into trips (feed_version, trip_id, route_id, service_id) "
        "values ($1, 'trip-1', 'route-9', 'svc')",
        VERSION,
    )
    await pool.execute(
        "insert into stops (feed_version, stop_id, stop_name, stop_lat, stop_lon) "
        "values ($1, 'stop-A', 'Gilman & Eucalyptus', 32.87, -117.24)",
        VERSION,
    )
    await pool.execute(
        """
        insert into arrivals (start_date, trip_id, stop_sequence, feed_version,
                              stop_id, vehicle_id, arrived_at, method, ping_gap_seconds)
        values ($1::date, 'trip-1', 3, $2::text, 'stop-A', 'bus-1',
                $3::timestamptz, 'interpolated', 30)
        """,
        DAY_TEST,
        VERSION,
        ARRIVED,
    )
    await pool.execute(
        """
        insert into model_runs (source, train_from, train_to, test_from, test_to,
                                train_rows, test_rows, features, notes)
        values ('lgbm', $1::date, $1::date, $2::date, $2::date, 1000, 200,
                '["stops_ahead"]'::jsonb, 'note')
        """,
        DAY_TRAIN,
        DAY_TEST,
    )
    for source, predicted_offset in (("mts", 120), ("lgbm", 60)):
        for horizon in (1, 5, 10, 20):
            await pool.execute(
                """
                insert into prediction_errors (
                    start_date, trip_id, stop_sequence, horizon_minutes, source,
                    feed_version, stop_id, route_id, arrived_at, predicted_arrival,
                    predicted_at, error_seconds, abs_error_seconds, ping_gap_seconds,
                    service_minute, is_weekend, has_all_horizons)
                values ($1::date,'trip-1',3,$2::int,$3::text,$4::text,
                        'stop-A','route-9',
                        $5::timestamptz,
                        $5::timestamptz + ($6::int * interval '1 second'),
                        $5::timestamptz - ($2::int * interval '1 minute'),
                        $6::int, abs($6::int), 30, 720, false, true)
                """,
                DAY_TEST,
                horizon,
                source,
                VERSION,
                ARRIVED,
                predicted_offset,
            )


# --- the window, which decides what every comparison is computed over ---------


@pytest.mark.usefixtures("clean")
async def test_window_comes_from_the_latest_model_run(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """Comparing MTS over five days against a model tested on two would be wrong."""
    await _seed(db_pool)
    body = (await client.get("/api/window")).json()

    assert body["test_from"] == DAY_TEST.isoformat()
    assert body["test_to"] == DAY_TEST.isoformat()
    assert body["source_of_window"] == "latest model run"


@pytest.mark.usefixtures("clean")
async def test_window_falls_back_when_no_model_has_run(
    client: httpx.AsyncClient,
) -> None:
    body = (await client.get("/api/window")).json()
    assert body["source_of_window"] == "no data"


# --- headline ----------------------------------------------------------------


@pytest.mark.usefixtures("clean")
async def test_headline_returns_one_row_per_source_and_horizon(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    body = (await client.get("/api/headline")).json()

    assert len(body["rows"]) == 8
    sources = {row["source"] for row in body["rows"]}
    assert sources == {"mts", "lgbm"}

    ours = next(r for r in body["rows"] if r["source"] == "lgbm" and r["horizon_minutes"] == 10)
    assert ours["mae_seconds"] == 60
    assert ours["n"] == 1


@pytest.mark.usefixtures("clean")
async def test_headline_states_the_label_filter(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """The page has to be able to say what population the number describes."""
    await _seed(db_pool)
    body = (await client.get("/api/headline")).json()
    assert body["label_filter_seconds"] == 180


@pytest.mark.usefixtures("clean")
async def test_poorly_observed_arrivals_are_excluded(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """Otherwise the metric partly measures our own GPS gaps rather than MTS."""
    await _seed(db_pool)
    await db_pool.execute("update prediction_errors set ping_gap_seconds = 900")

    body = (await client.get("/api/headline")).json()
    assert body["rows"] == []


@pytest.mark.usefixtures("clean")
async def test_headline_is_empty_not_broken_on_a_fresh_database(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/headline")
    assert response.status_code == 200
    assert response.json()["rows"] == []


# --- the other panels --------------------------------------------------------


@pytest.mark.usefixtures("clean")
async def test_routes_compares_mts_against_the_model(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    body = (await client.get("/api/routes?horizon=10&min_n=1")).json()

    assert len(body) == 1
    assert body[0]["route_id"] == "route-9"
    assert body[0]["mts_mae_seconds"] == 120
    assert body[0]["model_mae_seconds"] == 60
    assert body[0]["improvement_seconds"] == 60


@pytest.mark.usefixtures("clean")
async def test_routes_hides_thinly_observed_routes(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """A route with a handful of arrivals is noise, not a finding."""
    await _seed(db_pool)
    body = (await client.get("/api/routes?horizon=10&min_n=500")).json()
    assert body == []


@pytest.mark.usefixtures("clean")
async def test_stop_detail_pairs_prediction_with_what_happened(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """The panel a visitor clicks into: predicted against actual, side by side."""
    await _seed(db_pool)
    body = (await client.get("/api/stops/stop-A?horizon=10")).json()

    assert body["stop_name"] == "Gilman & Eucalyptus"
    assert len(body["recent"]) == 1
    row = body["recent"][0]
    assert row["mts_error_seconds"] == 120
    assert row["model_error_seconds"] == 60
    assert row["arrived_at"].startswith("2026-10-02")


@pytest.mark.usefixtures("clean")
async def test_stop_detail_for_an_unknown_stop_is_empty_not_a_500(
    client: httpx.AsyncClient,
) -> None:
    response = await client.get("/api/stops/nope")
    assert response.status_code == 200
    assert response.json()["recent"] == []


@pytest.mark.usefixtures("clean")
async def test_stops_only_lists_places_we_have_observed(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    body = (await client.get("/api/stops")).json()

    assert len(body) == 1
    assert body[0]["stop_id"] == "stop-A"
    assert body[0]["lat"] == pytest.approx(32.87)


@pytest.mark.usefixtures("clean")
async def test_vehicles_ignores_stale_positions(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """A map of buses that stopped reporting hours ago would be a lie."""
    await db_pool.execute(
        "insert into vehicle_positions (vehicle_id, ts, lat, lon) values "
        "('fresh', now() - interval '1 minute', 32.87, -117.24), "
        "('stale', now() - interval '3 hours', 32.87, -117.24)"
    )
    body = (await client.get("/api/vehicles")).json()
    assert [v["vehicle_id"] for v in body] == ["fresh"]


@pytest.mark.usefixtures("clean")
async def test_data_quality_always_states_its_caveats(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """The caveats are part of the contract, not decoration."""
    await db_pool.execute(
        "insert into poll_log (feed, started_at, status) values "
        "('vehicle_positions', now() - interval '2 hours', 'ok'), "
        "('vehicle_positions', now(), 'ok')"
    )
    body = (await client.get("/api/data-quality")).json()

    assert len(body["coverage"]) >= 1
    assert any("laptop" in caveat for caveat in body["caveats"])
    assert any("parity" in caveat for caveat in body["caveats"])


@pytest.mark.usefixtures("clean")
async def test_model_run_exposes_provenance(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    body = (await client.get("/api/model-run")).json()

    assert body["train_from"] == DAY_TRAIN.isoformat()
    assert body["test_to"] == DAY_TEST.isoformat()
    assert body["features"] == ["stops_ahead"]


@pytest.mark.usefixtures("clean")
async def test_healthz_reports_the_database(client: httpx.AsyncClient) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


@pytest.mark.usefixtures("clean")
async def test_distribution_buckets_absolute_error(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    body = (await client.get("/api/error-distribution?horizon=10")).json()

    assert len(body) == 2
    assert {entry["source"] for entry in body} == {"mts", "lgbm"}
    assert all(entry["n"] == 1 for entry in body)


# --- the ETA correction ------------------------------------------------------


def test_the_most_specific_bias_with_enough_evidence_wins() -> None:
    """Stop and route beats route alone when it has the samples to back it."""
    from ontime_sd.api import BASIS_STOP_ROUTE, choose_bias

    bias, basis, sample = choose_bias(91, -131.0, 2000, -40.0)
    assert bias == -131.0
    assert basis == BASIS_STOP_ROUTE
    assert sample == 91


def test_a_thin_stop_level_bias_falls_back_to_the_route() -> None:
    """Correcting on three observations would be correcting on noise."""
    from ontime_sd.api import BASIS_ROUTE, choose_bias

    bias, basis, sample = choose_bias(3, -300.0, 2000, -40.0)
    assert bias == -40.0
    assert basis == BASIS_ROUTE
    assert sample == 2000


def test_no_evidence_means_no_correction_rather_than_a_guess() -> None:
    """The app then shows MTS unchanged and says why."""
    from ontime_sd.api import BASIS_NONE, choose_bias

    bias, basis, sample = choose_bias(None, None, 4, -40.0)
    assert bias is None
    assert basis == BASIS_NONE
    assert sample is None


@pytest.mark.usefixtures("clean")
async def test_upcoming_excludes_arrivals_already_in_the_past(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    now = datetime.now(tz=UTC)
    for offset, label in ((-10, "gone"), (10, "coming")):
        await db_pool.execute(
            """
            insert into predictions (start_date, trip_id, stop_sequence, observed_at,
                                     stop_id, route_id, arrival_time)
            values ($1::date, $2::text, 3, now(), 'stop-A', 'route-9',
                    $3::timestamptz)
            """,
            DAY_TEST,
            f"trip-{label}",
            now + timedelta(minutes=offset),
        )

    body = (await client.get("/api/stops/stop-A/upcoming")).json()
    assert [a["trip_id"] for a in body["arrivals"]] == ["trip-coming"]


@pytest.mark.usefixtures("clean")
async def test_upcoming_applies_the_measured_bias(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """MTS running early must push our estimate later, not earlier."""
    await _seed(db_pool)
    # _seed wrote 4 horizons of mts error at +120s, which is MTS predicting late.
    # Add enough rows at the 10 minute horizon to clear the sample threshold.
    for index in range(12):
        await db_pool.execute(
            """
            insert into prediction_errors (
                start_date, trip_id, stop_sequence, horizon_minutes, source,
                feed_version, stop_id, route_id, arrived_at, predicted_arrival,
                predicted_at, error_seconds, abs_error_seconds, ping_gap_seconds,
                service_minute, is_weekend, has_all_horizons)
            values ($1::date, $2::text, 3, 10, 'mts', $3::text, 'stop-A', 'route-9',
                    $4::timestamptz, $4::timestamptz, $4::timestamptz - interval '10 min',
                    -120, 120, 30, 720, false, true)
            """,
            DAY_TEST,
            f"filler-{index}",
            VERSION,
            ARRIVED,
        )

    due = datetime.now(tz=UTC) + timedelta(minutes=9)
    await db_pool.execute(
        """
        insert into predictions (start_date, trip_id, stop_sequence, observed_at,
                                 stop_id, route_id, arrival_time)
        values ($1::date, 'trip-live', 3, now(), 'stop-A', 'route-9', $2::timestamptz)
        """,
        DAY_TEST,
        due,
    )

    body = (await client.get("/api/stops/stop-A/upcoming")).json()
    arrival = body["arrivals"][0]

    assert arrival["correction_basis"] in {"stop_and_route", "route"}
    corrected = datetime.fromisoformat(arrival["corrected_arrival"])
    # Bias is negative overall, so the corrected estimate must be later than MTS.
    assert corrected > datetime.fromisoformat(arrival["mts_arrival"])


@pytest.mark.usefixtures("clean")
async def test_upcoming_without_history_returns_mts_unchanged(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """A stop we have never measured must not get a fabricated correction."""
    await _seed(db_pool)
    await db_pool.execute("delete from prediction_errors")
    await db_pool.execute(
        """
        insert into predictions (start_date, trip_id, stop_sequence, observed_at,
                                 stop_id, route_id, arrival_time)
        values ($1::date, 'trip-live', 3, now(), 'stop-A', 'route-9',
                now() + interval '6 minutes')
        """,
        DAY_TEST,
    )

    body = (await client.get("/api/stops/stop-A/upcoming")).json()
    arrival = body["arrivals"][0]
    assert arrival["corrected_arrival"] is None
    assert arrival["correction_basis"] == "none"


@pytest.mark.usefixtures("clean")
async def test_search_finds_stops_by_name(client: httpx.AsyncClient, db_pool: asyncpg.Pool) -> None:
    """Regression: this route was being swallowed by /api/stops/{stop_id}, so
    'search' was read as a stop id and the endpoint returned an empty stop."""
    await _seed(db_pool)
    body = (await client.get("/api/stops/search?q=Gilman")).json()

    assert isinstance(body, list)
    assert len(body) == 1
    assert body[0]["stop_id"] == "stop-A"


@pytest.mark.usefixtures("clean")
async def test_search_needs_more_than_one_character(
    client: httpx.AsyncClient,
) -> None:
    assert (await client.get("/api/stops/search?q=G")).status_code == 422


@pytest.mark.usefixtures("clean")
async def test_vehicles_carry_a_route_label_for_the_map(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    await db_pool.execute(
        "insert into routes (feed_version, route_id, route_short_name, route_type) "
        "values ($1, 'route-9', '30', 3)",
        VERSION,
    )
    await db_pool.execute(
        "insert into vehicle_positions (vehicle_id, ts, route_id, lat, lon) "
        "values ('bus-1', now(), 'route-9', 32.87, -117.24)"
    )
    body = (await client.get("/api/vehicles")).json()

    assert body[0]["route_short_name"] == "30"
    assert body[0]["route_type"] == 3
