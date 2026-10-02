"""Deciding which service day an observation belongs to.

The real MTS feed omits start_date on every entity, so it has to be derived. Using
the observation date alone is wrong for any trip crossing midnight, and on real
data that filed the tail of one night's run together with the start of the next,
producing a single trip record spanning 23.8 hours. See ADR-0035.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ontime_sd.trip_stops import (
    resolve_service_day,
    seconds_into_service_day,
)

# Trip 19627988 in the real feed: scheduled 23:35 to 24:01, so it starts one
# calendar day and finishes the next.
MIDNIGHT_CROSSER = (23 * 3600 + 35 * 60, 24 * 3600 + 60)
# An ordinary daytime trip, 14:00 to 14:45.
DAYTIME = (14 * 3600, 14 * 3600 + 45 * 60)
# A very late trip, 25:30 to 26:05, entirely past midnight.
DEEP_NIGHT = (25 * 3600 + 30 * 60, 26 * 3600 + 5 * 60)


def _local(year: int, month: int, day: int, hour: int, minute: int = 0) -> datetime:
    """A UTC instant corresponding to the given Los Angeles wall clock time."""
    from zoneinfo import ZoneInfo

    return datetime(
        year, month, day, hour, minute, tzinfo=ZoneInfo("America/Los_Angeles")
    ).astimezone(UTC)


# --- seconds_into_service_day ------------------------------------------------


def test_offset_within_the_same_day() -> None:
    at = _local(2026, 9, 29, 14, 30)
    assert seconds_into_service_day(at, date(2026, 9, 29)) == 14 * 3600 + 30 * 60


def test_offset_past_midnight_exceeds_86400() -> None:
    """The whole point of service day seconds: 00:05 is 24:05 of yesterday."""
    at = _local(2026, 9, 29, 0, 5)
    assert seconds_into_service_day(at, date(2026, 9, 28)) == 24 * 3600 + 5 * 60


def test_offset_is_negative_before_the_service_day() -> None:
    at = _local(2026, 9, 28, 23, 0)
    assert seconds_into_service_day(at, date(2026, 9, 29)) == -3600


# --- resolve_service_day -----------------------------------------------------


def test_daytime_trip_belongs_to_today() -> None:
    at = _local(2026, 9, 29, 14, 20)
    assert resolve_service_day(at, DAYTIME) == date(2026, 9, 29)


def test_after_midnight_observation_belongs_to_yesterday() -> None:
    """The bug this fixes. Trip began 23:35 on the 28th, seen at 00:05 on the 29th."""
    at = _local(2026, 9, 29, 0, 5)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 9, 28)


def test_the_same_trip_before_midnight_belongs_to_today() -> None:
    """The next night's run of the same trip id must not be confused with it."""
    at = _local(2026, 9, 29, 23, 40)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 9, 29)


def test_a_trip_entirely_past_midnight_resolves_to_the_previous_day() -> None:
    """Scheduled 25:30, which is 01:30 the next morning."""
    at = _local(2026, 9, 29, 1, 40)
    assert resolve_service_day(at, DEEP_NIGHT) == date(2026, 9, 28)


def test_no_schedule_falls_back_to_the_local_date() -> None:
    """Same answer the old clock based inference gave, for unknown trips."""
    at = _local(2026, 9, 29, 0, 5)
    assert resolve_service_day(at, None) == date(2026, 9, 29)


def test_an_observation_matching_neither_candidate_defaults_to_today() -> None:
    """A midday ping on a trip scheduled near midnight: report it under today."""
    at = _local(2026, 9, 29, 12, 0)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 9, 29)


# --- the grace margins -------------------------------------------------------


def test_a_late_running_trip_is_still_attributed_correctly() -> None:
    """Scheduled to finish 00:01, observed 00:45. Buses run late."""
    at = _local(2026, 9, 29, 0, 45)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 9, 28)


def test_far_beyond_the_grace_margin_falls_back_to_today() -> None:
    """Over an hour after the scheduled end is no longer that run."""
    at = _local(2026, 9, 29, 1, 30)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 9, 29)


def test_slightly_early_departure_is_attributed_to_the_right_day() -> None:
    at = _local(2026, 9, 29, 13, 50)
    assert resolve_service_day(at, DAYTIME) == date(2026, 9, 29)


@pytest.mark.parametrize("minutes_late", [0, 10, 30, 55])
def test_lateness_within_the_margin_keeps_the_service_day(minutes_late: int) -> None:
    at = _local(2026, 9, 29, 0, 1 + minutes_late)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 9, 28)


# --- daylight saving ---------------------------------------------------------


def test_resolution_works_across_the_autumn_clock_change() -> None:
    """2026-11-01 is when US daylight saving ends, so that service day is 25 hours.

    A trip observed after midnight on the 1st still belongs to the 31st.
    """
    at = _local(2026, 11, 1, 0, 10)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 10, 31)


def test_resolution_works_across_the_spring_clock_change() -> None:
    """2026-03-08, when the service day is 23 hours long."""
    at = _local(2026, 3, 8, 0, 10)
    assert resolve_service_day(at, MIDNIGHT_CROSSER) == date(2026, 3, 7)
