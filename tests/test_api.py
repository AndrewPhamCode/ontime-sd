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

from ontime_sd.api import TIME_BANDS, create_app
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


# --- settings: parameters must reach the query and the cache key --------------
#
# Every one of these endpoints caches its aggregate. Before the settings work
# the cache keys contained none of the parameters, so changing a setting
# returned the previous setting's numbers for up to a minute with nothing on
# screen saying so. The tests below pass a parameter, then a different one, back
# to back inside the TTL, and require the answer to change.


async def _seed_poorly_observed(pool: asyncpg.Pool) -> None:
    """A second arrival whose GPS evidence is weak, and badly mispredicted.

    It is excluded by the default label filter and admitted by a loose one, so
    the two filter settings must produce different numbers.
    """
    await pool.execute(
        """
        insert into arrivals (start_date, trip_id, stop_sequence, feed_version,
                              stop_id, vehicle_id, arrived_at, method, ping_gap_seconds)
        values ($1::date, 'trip-1', 9, $2::text, 'stop-A', 'bus-2',
                $3::timestamptz, 'interpolated', 600)
        """,
        DAY_TEST,
        VERSION,
        ARRIVED,
    )
    for source in ("mts", "lgbm"):
        for horizon in (1, 5, 10, 20):
            await pool.execute(
                """
                insert into prediction_errors (
                    start_date, trip_id, stop_sequence, horizon_minutes, source,
                    feed_version, stop_id, route_id, arrived_at, predicted_arrival,
                    predicted_at, error_seconds, abs_error_seconds, ping_gap_seconds,
                    service_minute, is_weekend, has_all_horizons)
                values ($1::date,'trip-1',9,$2::int,$3::text,$4::text,
                        'stop-A','route-9',
                        $5::timestamptz,
                        $5::timestamptz + (900 * interval '1 second'),
                        $5::timestamptz - ($2::int * interval '1 minute'),
                        900, 900, 600, 720, false, true)
                """,
                DAY_TEST,
                horizon,
                source,
                VERSION,
                ARRIVED,
            )


@pytest.mark.usefixtures("clean")
async def test_headline_cache_is_keyed_on_the_label_filter(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    await _seed_poorly_observed(db_pool)

    tight = (await client.get("/api/headline?max_ping_gap=180")).json()
    loose = (await client.get("/api/headline?max_ping_gap=900")).json()

    def mae(body: dict, source: str) -> float:
        row = next(r for r in body["rows"] if r["source"] == source and r["horizon_minutes"] == 10)
        return float(row["mae_seconds"])

    # The tight filter sees only the well observed arrival, which MTS missed by
    # 120s. The loose one also admits the 900s miss, so the average must rise.
    assert mae(tight, "mts") == pytest.approx(120.0)
    assert mae(loose, "mts") > mae(tight, "mts")
    assert tight["label_filter_seconds"] == 180
    assert loose["label_filter_seconds"] == 900


@pytest.mark.usefixtures("clean")
async def test_distribution_cache_is_keyed_on_the_label_filter(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    await _seed_poorly_observed(db_pool)

    tight = (await client.get("/api/error-distribution?horizon=10&max_ping_gap=180")).json()
    loose = (await client.get("/api/error-distribution?horizon=10&max_ping_gap=900")).json()

    assert sum(b["n"] for b in loose) > sum(b["n"] for b in tight)


@pytest.mark.usefixtures("clean")
async def test_routes_cache_is_keyed_on_the_label_filter_and_predictor(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)
    await _seed_poorly_observed(db_pool)

    tight = (await client.get("/api/routes?horizon=10&min_n=1&max_ping_gap=180")).json()
    loose = (await client.get("/api/routes?horizon=10&min_n=1&max_ping_gap=900")).json()
    assert tight[0]["n"] < loose[0]["n"]

    # Switching the compared predictor must not return the previous one's column.
    lgbm = (await client.get("/api/routes?horizon=10&min_n=1&compare=lgbm")).json()
    mts = (await client.get("/api/routes?horizon=10&min_n=1&compare=mts")).json()
    assert lgbm[0]["model_mae_seconds"] != mts[0]["model_mae_seconds"]
    # Comparing MTS against itself is a no-op by construction, which is a useful
    # sanity check that the parameter reaches the aggregate at all.
    assert mts[0]["model_mae_seconds"] == pytest.approx(mts[0]["mts_mae_seconds"])


@pytest.mark.usefixtures("clean")
async def test_data_quality_caveat_states_the_filter_actually_in_use(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)

    default = (await client.get("/api/data-quality")).json()
    loose = (await client.get("/api/data-quality?max_ping_gap=900")).json()

    # The caveat is prose a reader trusts, so it has to track the setting rather
    # than repeat the constant it was written against.
    assert any("3 min" in c for c in default["caveats"])
    assert any("15 min" in c for c in loose["caveats"])


@pytest.mark.usefixtures("clean")
async def test_stop_detail_cache_is_keyed_on_the_predictor(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)

    lgbm = (await client.get("/api/stops/stop-A?horizon=10&compare=lgbm")).json()
    mts = (await client.get("/api/stops/stop-A?horizon=10&compare=mts")).json()

    assert lgbm["recent"][0]["model_error_seconds"] == pytest.approx(60.0)
    assert mts["recent"][0]["model_error_seconds"] == pytest.approx(120.0)


@pytest.mark.parametrize(
    "path",
    [
        "/api/routes?horizon=10&compare=nonsense",
        "/api/stops/stop-A?compare=nonsense",
    ],
)
async def test_an_unknown_predictor_is_rejected_not_silently_empty(
    client: httpx.AsyncClient, path: str
) -> None:
    # An empty column reads as "the model has no data for this", which is a very
    # different claim from "that predictor does not exist".
    response = await client.get(path)
    assert response.status_code == 422


async def test_every_known_predictor_is_accepted(client: httpx.AsyncClient) -> None:
    from ontime_sd.api import SOURCES, validate_source

    for source in SOURCES:
        assert validate_source(source) == source


def test_cache_entries_do_not_collide_across_parameters() -> None:
    """The cache itself, independent of any endpoint.

    This is the invariant the endpoints rely on: a key is only a cache hit for
    the exact same key, so a key that omits a parameter serves the wrong answer.
    """
    import asyncio

    from ontime_sd.api import _TtlCache

    async def exercise() -> None:
        cache = _TtlCache(ttl=60)
        calls: list[str] = []

        async def produce(tag: str) -> str:
            calls.append(tag)
            return tag

        assert await cache.get("headline:180", lambda: produce("tight")) == "tight"
        assert await cache.get("headline:900", lambda: produce("loose")) == "loose"
        # The second key must have produced its own value, not reused the first.
        assert calls == ["tight", "loose"]
        # And the same key within the TTL must not produce again.
        assert await cache.get("headline:180", lambda: produce("again")) == "tight"
        assert calls == ["tight", "loose"]

    asyncio.run(exercise())


def test_the_minimum_sample_threshold_is_a_parameter_not_a_constant() -> None:
    """The setting has to reach the decision, or the panel would lie."""
    from ontime_sd.api import BASIS_NONE, BASIS_STOP_ROUTE, choose_bias

    # Three observations: refused at the default, allowed when the viewer lowers
    # the bar, and the basis says which happened either way.
    _, basis, _ = choose_bias(3, -300.0, 0, None)
    assert basis == BASIS_NONE

    bias, basis, sample = choose_bias(3, -300.0, 0, None, min_stop_route=1)
    assert basis == BASIS_STOP_ROUTE
    assert bias == pytest.approx(-300.0)
    assert sample == 3

    # Raising it past the evidence refuses a correction that the default allowed.
    _, basis, _ = choose_bias(91, -131.0, 0, None, min_stop_route=100)
    assert basis == BASIS_NONE


# --- time of day bands --------------------------------------------------------


def test_every_service_minute_falls_in_exactly_one_band() -> None:
    """Exhaustive and non-overlapping, so per-band counts sum to the all-day count.

    Not a stylistic preference. A page showing five bands whose row counts do not
    add up to the total gives a visitor a concrete reason to distrust every other
    number on it. GTFS service times run past 24:00:00 for trips crossing
    midnight, so the range checked here goes beyond 1440.
    """
    bands = {name: bounds for name, bounds in TIME_BANDS.items() if bounds is not None}

    def contains(bounds: tuple[int, int], minute: int) -> bool:
        low, high = bounds
        if low < high:
            return low <= minute < high
        # Wraps around midnight, which is the case worth checking.
        return minute >= low or minute < high

    for minute in range(0, 1740):
        matches = [name for name, bounds in bands.items() if contains(bounds, minute)]
        assert len(matches) == 1, f"service minute {minute} matched {matches}"


@pytest.mark.usefixtures("clean")
async def test_a_band_excludes_arrivals_outside_it(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)

    # The fixture sits at service minute 720, which is midday.
    midday = (await client.get("/api/headline?time_band=midday")).json()
    am_rush = (await client.get("/api/headline?time_band=am_rush")).json()

    assert midday["rows"], "midday should contain the seeded arrival"
    assert am_rush["rows"] == [], "the morning band must not borrow midday's rows"


@pytest.mark.usefixtures("clean")
async def test_bands_partition_the_all_day_population(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """The property the exhaustiveness test asserts in the abstract, over real rows."""
    await _seed(db_pool)
    # A second arrival in the evening, so more than one band is populated.
    #
    # Seeded for BOTH sources on purpose. The headline scores a matched
    # population (ADR-0043), so an arrival only one source predicted is excluded
    # and would not appear in any band, which is the behaviour the next test
    # pins rather than something to work around here.
    for source in ("mts", "lgbm"):
        await db_pool.execute(
            """
            insert into prediction_errors (
                start_date, trip_id, stop_sequence, horizon_minutes, source,
                feed_version, stop_id, route_id, arrived_at, predicted_arrival,
                predicted_at, error_seconds, abs_error_seconds, ping_gap_seconds,
                service_minute, is_weekend, has_all_horizons)
            values ($1::date,'trip-1',4,10,$4::text,$2::text,'stop-A','route-9',
                    $3::timestamptz, $3::timestamptz,
                    -- The schema enforces that a prediction precedes its own cutoff.
                    $3::timestamptz - interval '10 minutes', 0, 0, 30,
                    1200, false, true)
            """,
            DAY_TEST,
            VERSION,
            ARRIVED,
            source,
        )

    def mts_at_ten(body: dict) -> int:
        rows = [
            row for row in body["rows"] if row["source"] == "mts" and row["horizon_minutes"] == 10
        ]
        return rows[0]["n"] if rows else 0

    all_day = mts_at_ten((await client.get("/api/headline?time_band=all")).json())
    per_band = 0
    for band in ("am_rush", "midday", "pm_rush", "evening", "late"):
        per_band += mts_at_ten((await client.get(f"/api/headline?time_band={band}")).json())

    assert per_band == all_day == 2


@pytest.mark.usefixtures("clean")
async def test_an_unknown_band_is_rejected_rather_than_ignored(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """Falling back to all-day would show the unfiltered number under a band label."""
    await _seed(db_pool)

    response = await client.get("/api/headline?time_band=rush")

    assert response.status_code == 422


@pytest.mark.usefixtures("clean")
async def test_two_bands_do_not_share_a_cache_entry(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """The ADR-0039 invariant, extended to the new parameter.

    Every cache key must contain every parameter that changes the result. A band
    missing from the key would serve the previous band's numbers for up to the
    cache TTL with nothing on screen saying so, which is the exact bug the
    settings work was built to fix.
    """
    await _seed(db_pool)

    midday = (await client.get("/api/headline?time_band=midday")).json()
    am_rush = (await client.get("/api/headline?time_band=am_rush")).json()

    assert midday["time_band"] == "midday"
    assert am_rush["time_band"] == "am_rush"
    assert midday["rows"] != am_rush["rows"]


@pytest.mark.usefixtures("clean")
async def test_the_band_reaches_routes_and_distribution_too(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    await _seed(db_pool)

    routes = await client.get("/api/routes?min_n=1&time_band=am_rush")
    dist = await client.get("/api/error-distribution?time_band=am_rush")

    assert routes.status_code == 200
    assert dist.status_code == 200
    # Seeded row is midday, so the morning band is empty on both endpoints.
    assert routes.json() == []
    assert dist.json() == []


@pytest.mark.usefixtures("clean")
async def test_a_day_with_one_poll_does_not_crash_data_quality(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """Coverage is measured against the span from the first poll to the last.

    With a single poll that span is zero, the query's nullif turns the division
    into null, and the response model used to reject it with a 500. The panel
    whose job is to admit what the data cannot support was the one endpoint that
    fell over when the data was thin.
    """
    await _seed(db_pool)
    await db_pool.execute(
        """
        insert into poll_log (feed, started_at, status, http_code, entity_count,
                              rows_written, duration_ms)
        values ('vehicle_positions', $1::timestamptz, 'ok', 200, 10, 10, 50)
        """,
        ARRIVED,
    )

    response = await client.get("/api/data-quality")

    assert response.status_code == 200
    days = response.json()["coverage"]
    assert len(days) == 1
    # Reported as unmeasurable rather than invented as 100%.
    assert days[0]["coverage_pct"] is None
    assert days[0]["successful_polls"] == 1


@pytest.mark.usefixtures("clean")
async def test_the_headline_excludes_arrivals_only_one_source_predicted(
    client: httpx.AsyncClient, db_pool: asyncpg.Pool
) -> None:
    """ADR-0043, enforced at the endpoint rather than only in compare.sql.

    Our predictors only score where an anchor existed, which on real data is 77%
    of rows at the twenty minute horizon, and the rows they decline are the hard
    ones near the start of a trip. Averaging MTS over all rows while averaging the
    model over its own subset charged MTS for cases the model never attempted.
    """
    await _seed(db_pool)
    before = (await client.get("/api/headline")).json()
    n_before = next(
        r["n"] for r in before["rows"] if r["source"] == "mts" and r["horizon_minutes"] == 10
    )

    # An arrival MTS predicted and the model did not.
    await db_pool.execute(
        """
        insert into prediction_errors (
            start_date, trip_id, stop_sequence, horizon_minutes, source,
            feed_version, stop_id, route_id, arrived_at, predicted_arrival,
            predicted_at, error_seconds, abs_error_seconds, ping_gap_seconds,
            service_minute, is_weekend, has_all_horizons)
        values ($1::date,'trip-1',9,10,'mts',$2::text,'stop-A','route-9',
                $3::timestamptz, $3::timestamptz,
                $3::timestamptz - interval '10 minutes', 600, 600, 30,
                720, false, true)
        """,
        DAY_TEST,
        VERSION,
        ARRIVED,
    )

    after = (await client.get("/api/headline")).json()
    n_after = next(
        r["n"] for r in after["rows"] if r["source"] == "mts" and r["horizon_minutes"] == 10
    )

    # The unmatched row must not inflate MTS's population, and since its error is
    # large it would also have worsened MTS's average for free.
    assert n_after == n_before
