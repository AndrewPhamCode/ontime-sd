"""Route conditions, and the gate that keeps them honest.

The feature itself turned out to carry 0.12% of the model's gain (ADR-0045), so
these tests are not protecting much accuracy. They are protecting the property
that made it safe to compute over the test window at all: a bucket may only
contain observations that had already become knowable when the bucket started. If
that ever silently breaks, the feature stops being a reading of conditions and
becomes a window onto the answer, which is ADR-0038 all over again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ontime_sd.network_state import build_conditions

BUCKET = 300
NOON = datetime(2026, 10, 1, 19, 0, tzinfo=UTC)


def _row(route_id: str, arrived_at: datetime, ping_gap: int, delay: float) -> dict:
    """One observation, shaped like the query's output.

    `known_at` is the arrival plus the ping gap, because the arrival time is
    interpolated between two pings and does not exist until the later one lands.
    """
    return {
        "route_id": route_id,
        "arrived_at": arrived_at,
        "known_at": arrived_at + timedelta(seconds=ping_gap),
        "delay_seconds": delay,
    }


def test_an_observation_is_averaged_once_its_window_has_closed() -> None:
    conditions = build_conditions([_row("10", NOON, 30, 120.0)])

    # A cutoff in the bucket after the one holding the observation.
    delay, count = conditions.lookup("10", NOON + timedelta(seconds=BUCKET))

    assert count == 1
    assert delay == 120.0


def test_an_observation_whose_window_closes_after_the_bucket_is_refused() -> None:
    """The gate that matters.

    The arrival happens just before the bucket boundary but its ping gap carries
    the knowable instant past it. Including it would mean the bucket contained a
    value derived from a ping that had not been received yet.
    """
    arrived = NOON - timedelta(seconds=5)
    conditions = build_conditions([_row("10", arrived, 600, 120.0)])

    # The bucket containing NOON must not see it: known_at is NOON + 595s.
    delay, count = conditions.lookup("10", NOON + timedelta(seconds=60))

    assert count == 0
    assert delay == 0.0


def test_observations_older_than_the_window_fall_out() -> None:
    conditions = build_conditions(
        [
            _row("10", NOON - timedelta(hours=2), 0, 600.0),
            _row("10", NOON, 0, 60.0),
        ]
    )

    delay, count = conditions.lookup("10", NOON + timedelta(seconds=BUCKET))

    # Only the recent one, so the stale 600s delay cannot drag the mean.
    assert count == 1
    assert delay == 60.0


def test_routes_do_not_borrow_each_others_conditions() -> None:
    conditions = build_conditions(
        [
            _row("10", NOON, 0, 60.0),
            _row("992", NOON, 0, 600.0),
        ]
    )

    assert conditions.lookup("10", NOON + timedelta(seconds=BUCKET))[0] == 60.0
    assert conditions.lookup("992", NOON + timedelta(seconds=BUCKET))[0] == 600.0


def test_an_unobserved_route_reports_no_observations_rather_than_zero_delay() -> None:
    """Zero delay and no evidence must not look the same to the model.

    A route running exactly on time and a route nobody has seen are very different
    inputs, so the count travels with the mean.
    """
    conditions = build_conditions([_row("10", NOON, 0, 60.0)])

    delay, count = conditions.lookup("unknown-route", NOON + timedelta(seconds=BUCKET))

    assert (delay, count) == (0.0, 0)


def test_a_missing_route_id_is_tolerated() -> None:
    conditions = build_conditions([_row("10", NOON, 0, 60.0)])

    assert conditions.lookup(None, NOON) == (0.0, 0)


def test_an_empty_bucket_falls_back_to_the_previous_one() -> None:
    """A five minute gap in arrivals is ordinary on a quiet route.

    The previous bucket is a worse estimate than a fresh one and a much better
    estimate than nothing, so the lookup steps back exactly one bucket.
    """
    conditions = build_conditions([_row("10", NOON, 0, 90.0)])

    # Two buckets after the observation: the immediate bucket is empty because the
    # window has moved on, so this exercises the step back.
    found = conditions.lookup("10", NOON + timedelta(seconds=BUCKET + 60))

    assert found == (90.0, 1)
