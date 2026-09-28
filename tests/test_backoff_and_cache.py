"""Backoff and change-only storage. Pure logic, no database or network.

These two pieces carry the load bearing claims in DESIGN.md: that a feed outage
cannot turn into a hammering loop, and that prediction storage is proportional to
how often MTS changes its mind rather than to how often we poll.
"""

from __future__ import annotations

import random
from datetime import UTC, date, datetime, timedelta

import pytest

from ontime_sd.collector import Backoff, CollectorState
from ontime_sd.feeds import PredictionRow
from ontime_sd.sinks import PredictionCache

SERVICE_DAY = date(2026, 9, 27)
NOW = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)


# --- Backoff: ADR-0009 ---


def _backoff(base: float = 1.0, maximum: float = 300.0) -> Backoff:
    return Backoff(base, maximum, rng=random.Random(0))


def test_no_delay_before_any_failure() -> None:
    assert _backoff().ceiling == 0.0


def test_first_failure_waits_up_to_the_base_not_double_it() -> None:
    backoff = _backoff(base=2.0)
    backoff.record_failure()
    assert backoff.ceiling == 2.0


def test_ceiling_doubles_per_failure() -> None:
    backoff = _backoff(base=1.0)
    seen = []
    for _ in range(6):
        backoff.record_failure()
        seen.append(backoff.ceiling)
    assert seen == [1.0, 2.0, 4.0, 8.0, 16.0, 32.0]


def test_ceiling_is_capped() -> None:
    """The cap bounds worst case staleness after an outage to five minutes."""
    backoff = _backoff(base=1.0, maximum=300.0)
    for _ in range(50):
        backoff.record_failure()
    assert backoff.ceiling == 300.0


def test_delay_is_jittered_within_the_ceiling() -> None:
    """Full jitter, so two feeds recovering from one outage do not sync up."""
    backoff = _backoff(base=10.0)
    delays = [backoff.record_failure() for _ in range(40)]

    assert all(0.0 <= d <= backoff.ceiling for d in delays)
    assert len(set(delays)) > 1, "delays must not be identical"


def test_two_feeds_backing_off_together_do_not_align() -> None:
    a = Backoff(10.0, 300.0, rng=random.Random(1))
    b = Backoff(10.0, 300.0, rng=random.Random(2))
    assert [a.record_failure() for _ in range(5)] != [b.record_failure() for _ in range(5)]


def test_success_resets_the_backoff() -> None:
    backoff = _backoff()
    for _ in range(5):
        backoff.record_failure()
    backoff.reset()

    assert backoff.failures == 0
    assert backoff.ceiling == 0.0


# --- Staleness: what the health endpoint reads ---


def test_state_is_stale_before_any_poll_succeeds() -> None:
    state = CollectorState()
    assert state.is_stale(NOW, stale_after_seconds=300) is True


def test_one_healthy_feed_keeps_the_process_healthy() -> None:
    """A single feed failing is a data problem, not a reason to be restarted."""
    state = CollectorState()
    state.health("vehicle_positions").last_success = NOW
    state.health("trip_updates").last_success = NOW - timedelta(hours=2)

    assert state.is_stale(NOW, stale_after_seconds=300) is False


def test_state_is_stale_once_every_feed_goes_quiet() -> None:
    state = CollectorState()
    state.health("vehicle_positions").last_success = NOW - timedelta(seconds=301)

    assert state.is_stale(NOW, stale_after_seconds=300) is True


# --- PredictionCache: ADR-0005 ---


def _row(
    stop_sequence: int = 5,
    observed_at: datetime = NOW,
    arrival_offset_s: int | None = 600,
    delay_seconds: int | None = None,
    trip_id: str = "trip-1",
) -> PredictionRow:
    return PredictionRow(
        start_date=SERVICE_DAY,
        trip_id=trip_id,
        stop_sequence=stop_sequence,
        observed_at=observed_at,
        stop_id="stop-1",
        route_id="route-1",
        arrival_time=(
            None if arrival_offset_s is None else NOW + timedelta(seconds=arrival_offset_s)
        ),
        departure_time=None,
        delay_seconds=delay_seconds,
        schedule_relationship=None,
        vehicle_id="bus-1",
    )


def test_first_sighting_is_always_written() -> None:
    cache = PredictionCache(threshold_seconds=30)
    assert cache.select_changed([_row()]) == [_row()]


def test_identical_repeat_is_not_written() -> None:
    cache = PredictionCache(threshold_seconds=30)
    rows = [_row()]
    cache.remember(cache.select_changed(rows))

    assert cache.select_changed(rows) == []


def test_small_revision_is_not_written() -> None:
    """A 29 second shift is below the threshold and is discarded."""
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(arrival_offset_s=600)]))

    assert cache.select_changed([_row(arrival_offset_s=629)]) == []


def test_revision_exactly_at_the_threshold_is_written() -> None:
    """The charter says write on a change of 30s or more, so 30 counts."""
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(arrival_offset_s=600)]))

    assert len(cache.select_changed([_row(arrival_offset_s=630)])) == 1


def test_large_revision_is_written() -> None:
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(arrival_offset_s=600)]))

    assert len(cache.select_changed([_row(arrival_offset_s=300)])) == 1


def test_revision_earlier_by_more_than_the_threshold_is_written() -> None:
    """The comparison is on magnitude, so running early counts too."""
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(arrival_offset_s=600)]))

    assert len(cache.select_changed([_row(arrival_offset_s=560)])) == 1


def test_select_changed_does_not_mutate_the_cache() -> None:
    """A failed write must not suppress the row on the next poll.

    If select_changed remembered rows itself, a database error after it would
    lose those predictions permanently.
    """
    cache = PredictionCache(threshold_seconds=30)
    rows = [_row()]

    assert cache.select_changed(rows) == rows
    assert len(cache) == 0, "nothing recorded until the write succeeds"
    assert cache.select_changed(rows) == rows, "still offered after a failed write"


def test_stops_are_tracked_independently() -> None:
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(stop_sequence=1), _row(stop_sequence=2)]))

    changed = cache.select_changed(
        [_row(stop_sequence=1), _row(stop_sequence=2, arrival_offset_s=900)]
    )
    assert [row.stop_sequence for row in changed] == [2]


def test_same_stop_on_different_trips_is_tracked_independently() -> None:
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(trip_id="trip-1")]))

    assert len(cache.select_changed([_row(trip_id="trip-2")])) == 1


# --- delay-only feeds, the single_delay shape ---


def test_delay_only_row_uses_the_delay_for_comparison() -> None:
    cache = PredictionCache(threshold_seconds=30)
    first = _row(arrival_offset_s=None, delay_seconds=60)
    cache.remember(cache.select_changed([first]))

    small = _row(arrival_offset_s=None, delay_seconds=80)
    large = _row(arrival_offset_s=None, delay_seconds=120)

    assert cache.select_changed([small]) == []
    assert len(cache.select_changed([large])) == 1


def test_arrival_time_appearing_is_always_written() -> None:
    """A feed gaining absolute times is a real change at any magnitude."""
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(arrival_offset_s=None, delay_seconds=60)]))

    assert len(cache.select_changed([_row(arrival_offset_s=600, delay_seconds=60)])) == 1


def test_arrival_time_disappearing_is_always_written() -> None:
    cache = PredictionCache(threshold_seconds=30)
    cache.remember(cache.select_changed([_row(arrival_offset_s=600)]))

    assert len(cache.select_changed([_row(arrival_offset_s=None, delay_seconds=60)])) == 1


# --- pruning: bounding memory ---


def test_prune_drops_closed_service_days() -> None:
    cache = PredictionCache(threshold_seconds=30)
    old = PredictionRow(
        start_date=date(2026, 9, 1),
        trip_id="old-trip",
        stop_sequence=1,
        observed_at=NOW,
        stop_id=None,
        route_id=None,
        arrival_time=NOW,
        departure_time=None,
        delay_seconds=None,
        schedule_relationship=None,
        vehicle_id=None,
    )
    cache.remember([old, _row()])
    assert len(cache) == 2

    removed = cache.prune(before=date(2026, 9, 25))
    assert removed == 1
    assert len(cache) == 1


def test_prune_keeps_the_current_service_day() -> None:
    cache = PredictionCache(threshold_seconds=30)
    cache.remember([_row()])

    assert cache.prune(before=SERVICE_DAY) == 0
    assert len(cache) == 1


def test_a_zero_threshold_writes_every_change() -> None:
    """Configurable so the lossy compression can be turned off. See ADR-0017."""
    cache = PredictionCache(threshold_seconds=0)
    cache.remember(cache.select_changed([_row(arrival_offset_s=600)]))

    assert len(cache.select_changed([_row(arrival_offset_s=601)])) == 1


@pytest.mark.parametrize("threshold", [30, 60, 120])
def test_threshold_is_respected_at_its_boundary(threshold: int) -> None:
    cache = PredictionCache(threshold_seconds=threshold)
    cache.remember(cache.select_changed([_row(arrival_offset_s=1000)]))

    assert cache.select_changed([_row(arrival_offset_s=1000 + threshold - 1)]) == []
    assert len(cache.select_changed([_row(arrival_offset_s=1000 + threshold)])) == 1


# --- HTTP timeouts derived from the poll interval ---


@pytest.mark.parametrize("interval", [1, 2, 5, 10, 30, 60, 300])
def test_http_timeouts_are_always_positive(interval: int) -> None:
    """Regression: a constant subtracted from the interval went negative.

    With a one second interval the read timeout became -4, httpx rejected it,
    and every single poll failed.
    """
    from ontime_sd.collector import http_timeout
    from tests.conftest import make_settings

    timeout = http_timeout(make_settings(poll_interval_seconds=interval))

    assert timeout.read is not None and timeout.read > 0
    assert timeout.connect is not None and timeout.connect > 0


def test_read_timeout_stays_inside_the_poll_interval() -> None:
    """Otherwise a slow response would eat the next cycle."""
    from ontime_sd.collector import http_timeout
    from tests.conftest import make_settings

    for interval in (5, 10, 30):
        timeout = http_timeout(make_settings(poll_interval_seconds=interval))
        assert timeout.read is not None and timeout.read < interval
