"""Expanding calendar.txt and calendar_dates.txt into concrete service dates.

This is the piece Phase 3 and 4 lean on constantly when asking what ran on a
given day, and it is the kind of logic that is easy to get subtly wrong in a way
nothing notices: an off by one at a range boundary, or an exception applied in the
wrong order. Materializing it once puts the logic in one place. See ADR-0027.
"""

from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from ontime_sd.gtfs_load import _chunks, expand_service_dates
from ontime_sd.gtfs_static import GtfsParseError


def _rows(text: str) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(text)))


WEEKDAY_ONLY = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
weekday,1,1,1,1,1,0,0,20260907,20260913
"""

SUNDAY_ONLY = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
sunday,0,0,0,0,0,0,1,20260907,20260927
"""


def test_weekday_service_covers_only_weekdays() -> None:
    """2026-09-07 is a Monday, so the week runs Mon to Fri and stops."""
    pairs = expand_service_dates(_rows(WEEKDAY_ONLY))

    assert pairs == [
        (date(2026, 9, 7), "weekday"),
        (date(2026, 9, 8), "weekday"),
        (date(2026, 9, 9), "weekday"),
        (date(2026, 9, 10), "weekday"),
        (date(2026, 9, 11), "weekday"),
    ]


def test_range_boundaries_are_inclusive() -> None:
    """GTFS start_date and end_date both count, which is an easy off by one."""
    single = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
one_day,1,1,1,1,1,1,1,20260907,20260907
"""
    assert expand_service_dates(_rows(single)) == [(date(2026, 9, 7), "one_day")]


def test_sunday_service_picks_every_sunday_in_range() -> None:
    pairs = expand_service_dates(_rows(SUNDAY_ONLY))
    assert [day for day, _ in pairs] == [
        date(2026, 9, 13),
        date(2026, 9, 20),
        date(2026, 9, 27),
    ]
    assert all(day.weekday() == 6 for day, _ in pairs)


def test_exception_type_2_removes_a_date() -> None:
    """A holiday cancels service that the weekday pattern would include."""
    exceptions = _rows("service_id,date,exception_type\nweekday,20260909,2\n")
    pairs = expand_service_dates(_rows(WEEKDAY_ONLY), exceptions)

    assert (date(2026, 9, 9), "weekday") not in pairs
    assert (date(2026, 9, 8), "weekday") in pairs
    assert len(pairs) == 4


def test_exception_type_1_adds_a_date() -> None:
    """Sunday service running on a Saturday holiday."""
    exceptions = _rows("service_id,date,exception_type\nsunday,20260919,1\n")
    pairs = expand_service_dates(_rows(SUNDAY_ONLY), exceptions)

    assert (date(2026, 9, 19), "sunday") in pairs
    assert date(2026, 9, 19).weekday() == 5, "that date is a Saturday"


def test_service_defined_only_by_exceptions_still_appears() -> None:
    """Legal GTFS: a service with no calendar.txt row at all."""
    exceptions = _rows(
        "service_id,date,exception_type\nspecial_event,20260919,1\nspecial_event,20260920,1\n"
    )
    pairs = expand_service_dates([], exceptions)

    assert pairs == [
        (date(2026, 9, 19), "special_event"),
        (date(2026, 9, 20), "special_event"),
    ]


def test_all_zero_weekdays_yields_nothing_without_exceptions() -> None:
    """Also legal: a calendar row that only ever runs on listed dates."""
    never = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
never,0,0,0,0,0,0,0,20260907,20260930
"""
    assert expand_service_dates(_rows(never)) == []


def test_removing_a_date_that_was_never_added_is_not_an_error() -> None:
    """Feeds do this. It must be a no-op, not a crash."""
    exceptions = _rows("service_id,date,exception_type\nweekday,20260913,2\n")
    pairs = expand_service_dates(_rows(WEEKDAY_ONLY), exceptions)
    assert len(pairs) == 5


def test_output_is_sorted_and_deterministic() -> None:
    """Two loads of one feed must produce identical rows."""
    combined = SUNDAY_ONLY + WEEKDAY_ONLY.split("\n", 1)[1]
    first = expand_service_dates(_rows(combined))
    second = expand_service_dates(_rows(combined))

    assert first == second
    assert first == sorted(first)


def test_services_are_independent() -> None:
    combined = SUNDAY_ONLY + WEEKDAY_ONLY.split("\n", 1)[1]
    exceptions = _rows("service_id,date,exception_type\nweekday,20260909,2\n")
    pairs = expand_service_dates(_rows(combined), exceptions)

    assert (date(2026, 9, 9), "weekday") not in pairs
    assert (date(2026, 9, 13), "sunday") in pairs


# --- refusals ---


def test_backwards_date_range_is_refused() -> None:
    backwards = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
oops,1,1,1,1,1,1,1,20260930,20260907
"""
    with pytest.raises(GtfsParseError, match="ends"):
        expand_service_dates(_rows(backwards))


def test_unknown_exception_type_is_refused() -> None:
    """Only 1 and 2 exist. A 3 means the feed is not what we think it is."""
    exceptions = _rows("service_id,date,exception_type\nweekday,20260909,3\n")
    with pytest.raises(GtfsParseError, match="exception_type"):
        expand_service_dates(_rows(WEEKDAY_ONLY), exceptions)


def test_missing_service_id_is_refused() -> None:
    bad = """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
,1,1,1,1,1,1,1,20260907,20260930
"""
    with pytest.raises(GtfsParseError, match="service_id"):
        expand_service_dates(_rows(bad))


# --- chunking: ADR-0028 ---


def test_chunks_groups_to_the_requested_size() -> None:
    assert list(_chunks(range(7), 3)) == [[0, 1, 2], [3, 4, 5], [6]]


def test_chunks_of_empty_input_yields_nothing() -> None:
    assert list(_chunks([], 3)) == []


def test_chunks_stays_lazy() -> None:
    """The whole point: 1.37M rows must never be materialized at once."""
    consumed = []

    def source():
        for i in range(10):
            consumed.append(i)
            yield i

    chunks = _chunks(source(), 3)
    first = next(chunks)

    assert first == [0, 1, 2]
    # Only enough of the source was pulled to fill one chunk.
    assert consumed == [0, 1, 2]
