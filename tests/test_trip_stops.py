"""Recovering stop_sequence from stop_id.

The real MTS feed never sends stop_sequence, so every prediction depends on this
working. The case that drives the design is a trip visiting the same stop twice:
3,350 real trips (7.2%) do, and keying on stop_id alone would merge two different
arrivals into one row.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import asyncpg
import pytest

from ontime_sd.feeds import PredictionRow
from ontime_sd.gtfs_load import load_archive
from ontime_sd.gtfs_static import GtfsArchive
from ontime_sd.trip_stops import (
    StopSequenceResolver,
    align_stop_sequences,
    resolve_predictions,
)
from tests.gtfs_fixtures import write_feed

SERVICE_DAY = date(2026, 9, 28)
NOW = datetime(2026, 9, 28, 19, 0, tzinfo=UTC)
FEED_VERSION = "e" * 64

# The real shape: trip 19261672 calls at stop 94031 at sequences 1 and 4.
LOOP_TRIP = [(1, "94031"), (2, "12510"), (3, "12853"), (4, "94031")]


# --- align_stop_sequences ----------------------------------------------------


def test_simple_in_order_match() -> None:
    assert align_stop_sequences(["a", "b", "c"], [(1, "a"), (2, "b"), (3, "c")]) == [1, 2, 3]


def test_repeated_stop_resolves_to_a_different_sequence_each_visit() -> None:
    """The whole reason this function exists."""
    assert align_stop_sequences(["94031", "12510", "12853", "94031"], LOOP_TRIP) == [
        1,
        2,
        3,
        4,
    ]


def test_second_visit_alone_still_resolves_to_the_first_match() -> None:
    """A trip already past its first call at a repeated stop.

    The feed only sends remaining stops, so a single 94031 here is genuinely
    ambiguous from the feed alone. Matching the earliest remaining candidate is the
    documented choice, and it is correct whenever the feed is a suffix of the trip.
    """
    assert align_stop_sequences(["94031"], LOOP_TRIP) == [1]


def test_feed_carrying_only_remaining_stops_aligns() -> None:
    """Normal mid trip case: earlier stops have dropped off the feed."""
    assert align_stop_sequences(["12853", "94031"], LOOP_TRIP) == [3, 4]


def test_a_stop_not_on_the_trip_is_unresolved() -> None:
    assert align_stop_sequences(["a", "zzz", "c"], [(1, "a"), (2, "b"), (3, "c")]) == [
        1,
        None,
        3,
    ]


def test_never_matches_backwards() -> None:
    """Going backwards would let a later feed stop claim an earlier sequence."""
    assert align_stop_sequences(["c", "a"], [(1, "a"), (2, "b"), (3, "c")]) == [3, None]


def test_every_stop_identical() -> None:
    assert align_stop_sequences(["x", "x", "x"], [(1, "x"), (2, "x"), (3, "x")]) == [1, 2, 3]


def test_more_feed_stops_than_scheduled() -> None:
    assert align_stop_sequences(["a", "a", "a"], [(1, "a"), (2, "b")]) == [1, None, None]


@pytest.mark.parametrize(
    ("feed", "scheduled", "expected"),
    [
        ([], [(1, "a")], []),
        (["a"], [], [None]),
        ([], [], []),
    ],
)
def test_empty_inputs(
    feed: list[str], scheduled: list[tuple[int, str]], expected: list[int | None]
) -> None:
    assert align_stop_sequences(feed, scheduled) == expected


# --- resolve_predictions, against a real database ---------------------------


def _row(stop_id: str, feed_order: int, trip_id: str = "trip_day") -> PredictionRow:
    return PredictionRow(
        start_date=SERVICE_DAY,
        trip_id=trip_id,
        stop_sequence=None,
        observed_at=NOW,
        stop_id=stop_id,
        route_id="1",
        arrival_time=NOW,
        departure_time=None,
        delay_seconds=None,
        schedule_relationship=None,
        vehicle_id="bus-1",
        feed_order=feed_order,
    )


@pytest.fixture
def feed(tmp_path: Path) -> Path:
    return write_feed(tmp_path / "google_transit.zip")


async def _load_schedule(conn: asyncpg.Connection, feed: Path) -> None:
    with GtfsArchive(feed) as archive:
        await load_archive(conn, archive, FEED_VERSION, source_url="https://example.test/f.zip")


async def test_sequences_are_filled_from_the_schedule(conn: asyncpg.Connection, feed: Path) -> None:
    """trip_day calls at stop_a, stop_b, stop_c in sequences 1, 2, 3."""
    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver()

    rows = [_row("stop_a", 0), _row("stop_b", 1), _row("stop_c", 2)]
    resolved, dropped = await resolve_predictions(conn, resolver, rows)

    assert dropped == {}
    assert sorted(r.stop_sequence for r in resolved) == [1, 2, 3]
    assert all(r.resolved for r in resolved)


async def test_resolution_survives_a_round_trip_to_the_database(
    conn: asyncpg.Connection, feed: Path
) -> None:
    """Resolved rows must satisfy the predictions primary key."""
    from ontime_sd.sinks import write_predictions

    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver()
    resolved, _ = await resolve_predictions(conn, resolver, [_row("stop_a", 0), _row("stop_b", 1)])

    assert await write_predictions(conn, resolved) == 2
    stored = await conn.fetch(
        "select stop_sequence, stop_id from predictions order by stop_sequence"
    )
    assert [(r["stop_sequence"], r["stop_id"]) for r in stored] == [(1, "stop_a"), (2, "stop_b")]


async def test_a_stop_not_on_the_trip_is_counted(conn: asyncpg.Connection, feed: Path) -> None:
    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver()

    resolved, dropped = await resolve_predictions(
        conn, resolver, [_row("stop_a", 0), _row("not_a_stop", 1)]
    )

    assert len(resolved) == 1
    assert dropped == {"stop_not_on_trip": 1}


async def test_a_trip_absent_from_the_schedule_is_counted(
    conn: asyncpg.Connection, feed: Path
) -> None:
    """An added or unscheduled trip has nothing to align against."""
    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver()

    resolved, dropped = await resolve_predictions(
        conn, resolver, [_row("stop_a", 0, trip_id="ghost_trip")]
    )

    assert resolved == []
    assert dropped == {"trip_not_in_schedule": 1}


async def test_no_loaded_schedule_is_counted_not_silent(
    conn: asyncpg.Connection,
) -> None:
    """An operator problem: the loader never ran. Must be visible."""
    resolver = StopSequenceResolver()
    resolved, dropped = await resolve_predictions(conn, resolver, [_row("stop_a", 0)])

    assert resolved == []
    assert dropped == {"no_schedule_loaded": 1}


async def test_already_resolved_rows_pass_through_untouched(
    conn: asyncpg.Connection, feed: Path
) -> None:
    """A feed that does send stop_sequence must not be re-derived."""
    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver()

    from dataclasses import replace

    row = replace(_row("stop_c", 0), stop_sequence=99)
    resolved, dropped = await resolve_predictions(conn, resolver, [row])

    assert [r.stop_sequence for r in resolved] == [99]
    assert dropped == {}
    assert resolver.misses == 0, "no lookup should have happened"


async def test_empty_input_does_nothing(conn: asyncpg.Connection) -> None:
    resolver = StopSequenceResolver()
    assert await resolve_predictions(conn, resolver, []) == ([], {})


# --- caching -----------------------------------------------------------------


async def test_the_same_trip_is_looked_up_once(conn: asyncpg.Connection, feed: Path) -> None:
    """450 trips repeating every 30 seconds would otherwise be 450 queries a poll."""
    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver()

    for _ in range(4):
        await resolve_predictions(conn, resolver, [_row("stop_a", 0), _row("stop_b", 1)])

    # One lookup per trip per call, not per row: the two rows in each call are
    # grouped by trip first. So four calls make four lookups of one trip, of
    # which one misses and three hit.
    assert resolver.misses == 1
    assert resolver.hits == 3
    assert len(resolver) == 1


async def test_cache_is_bounded(conn: asyncpg.Connection, feed: Path) -> None:
    """Memory must not grow without limit across service days."""
    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver(max_cached_trips=2)

    for trip in ("trip_day", "trip_owl", "trip_sun"):
        await resolver.scheduled_stops(conn, FEED_VERSION, trip)

    assert len(resolver) <= 2


async def test_clear_resets_the_resolver(conn: asyncpg.Connection, feed: Path) -> None:
    await _load_schedule(conn, feed)
    resolver = StopSequenceResolver()
    await resolve_predictions(conn, resolver, [_row("stop_a", 0)])

    resolver.clear()
    assert len(resolver) == 0
