"""Arrival inference: reconstructing stop crossings from GPS.

Every test uses a synthetic track whose answer is known by construction, so the
assertions are exact rather than approximate. The cases are the ones the real data
actually contains: wide ping gaps, dwell at stops, GPS jitter, loop routes, trips
that stop being observed partway, and backwards noise.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ontime_sd.arrivals import (
    METHOD_AT_PING,
    METHOD_DWELL,
    METHOD_INTERPOLATED,
    Arrival,
    Ping,
    ScheduledStop,
    ShapePoint,
    TripTrack,
    build_track,
    find_arrivals,
    infer_arrivals,
    project_onto_segment,
)

T0 = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
BASE_LAT = 32.70
BASE_LON = -117.16

# Degrees per metre near San Diego.
LAT_DEG_PER_M = 1.0 / 111_320.0
LON_DEG_PER_M = 1.0 / 93_675.0


def straight_shape(length_m: float = 5000.0, step_m: float = 250.0) -> tuple[ShapePoint, ...]:
    """A due north straight route, so distance along it is just latitude."""
    count = int(length_m / step_m) + 1
    return tuple(
        ShapePoint(
            lat=BASE_LAT + (i * step_m) * LAT_DEG_PER_M,
            lon=BASE_LON,
            offset_m=i * step_m,
        )
        for i in range(count)
    )


def ping(seconds: float, metres: float, lateral_m: float = 0.0) -> Ping:
    """A fix at a given time and distance along the straight route."""
    return Ping(
        ts=T0 + timedelta(seconds=seconds),
        lat=BASE_LAT + metres * LAT_DEG_PER_M,
        lon=BASE_LON + lateral_m * LON_DEG_PER_M,
    )


def track_of(*points: tuple[float, float]) -> tuple[Ping, ...]:
    return tuple(ping(seconds, metres) for seconds, metres in points)


def _run(
    pings: tuple[Ping, ...],
    stops: tuple[ScheduledStop, ...],
    shape: tuple[ShapePoint, ...] | None = None,
    **options: float,
):
    return infer_arrivals(
        TripTrack(
            start_date=date(2026, 10, 2),
            trip_id="trip-1",
            vehicle_id="bus-1",
            feed_version="f" * 64,
            pings=pings,
            shape=shape or straight_shape(),
            stops=stops,
        ),
        **options,
    )


def seconds_after_t0(arrival: Arrival) -> float:
    return (arrival.arrived_at - T0).total_seconds()


def by_sequence(result) -> dict[int, Arrival]:
    return {a.stop_sequence: a for a in result.arrivals}


# --- stage 1: projection -----------------------------------------------------


def test_point_on_the_segment_projects_to_its_own_distance() -> None:
    start = ShapePoint(BASE_LAT, BASE_LON, 0.0)
    end = ShapePoint(BASE_LAT + 1000 * LAT_DEG_PER_M, BASE_LON, 1000.0)

    offset, perpendicular = project_onto_segment(
        BASE_LAT + 400 * LAT_DEG_PER_M, BASE_LON, start, end
    )
    assert offset == pytest.approx(400.0, abs=1.0)
    assert perpendicular == pytest.approx(0.0, abs=1.0)


def test_point_beside_the_segment_reports_perpendicular_distance() -> None:
    start = ShapePoint(BASE_LAT, BASE_LON, 0.0)
    end = ShapePoint(BASE_LAT + 1000 * LAT_DEG_PER_M, BASE_LON, 1000.0)

    offset, perpendicular = project_onto_segment(
        BASE_LAT + 400 * LAT_DEG_PER_M, BASE_LON + 30 * LON_DEG_PER_M, start, end
    )
    assert offset == pytest.approx(400.0, abs=1.0)
    assert perpendicular == pytest.approx(30.0, abs=2.0)


def test_point_past_the_end_clamps_to_the_end() -> None:
    """Otherwise a projection could run off the end of the route."""
    start = ShapePoint(BASE_LAT, BASE_LON, 0.0)
    end = ShapePoint(BASE_LAT + 1000 * LAT_DEG_PER_M, BASE_LON, 1000.0)

    offset, _ = project_onto_segment(BASE_LAT + 1500 * LAT_DEG_PER_M, BASE_LON, start, end)
    assert offset == pytest.approx(1000.0, abs=1.0)


def test_degenerate_segment_does_not_divide_by_zero() -> None:
    point = ShapePoint(BASE_LAT, BASE_LON, 500.0)
    offset, perpendicular = project_onto_segment(BASE_LAT, BASE_LON, point, point)
    assert offset == 500.0
    assert perpendicular == pytest.approx(0.0, abs=1.0)


# --- the headline case -------------------------------------------------------


def test_constant_speed_interpolates_exactly() -> None:
    """10 m/s with stops between pings. The arithmetic must be exact."""
    pings = track_of((0, 0), (100, 1000), (200, 2000), (300, 3000))
    stops = (
        ScheduledStop(1, "A", 500.0),
        ScheduledStop(2, "B", 1500.0),
        ScheduledStop(3, "C", 2500.0),
    )
    result = _run(pings, stops)
    arrivals = by_sequence(result)

    assert result.status == "ok"
    assert seconds_after_t0(arrivals[1]) == pytest.approx(50.0, abs=1.0)
    assert seconds_after_t0(arrivals[2]) == pytest.approx(150.0, abs=1.0)
    assert seconds_after_t0(arrivals[3]) == pytest.approx(250.0, abs=1.0)
    assert all(a.method == METHOD_INTERPOLATED for a in result.arrivals)


def test_arrival_times_increase_with_stop_sequence() -> None:
    """A violation would mean the projection ran backwards."""
    pings = track_of((0, 0), (60, 600), (120, 1200), (180, 1800), (240, 2400))
    stops = tuple(ScheduledStop(i, f"S{i}", i * 400.0) for i in range(1, 6))

    result = _run(pings, stops)
    times = [a.arrived_at for a in sorted(result.arrivals, key=lambda a: a.stop_sequence)]
    assert times == sorted(times)


# --- dwell -------------------------------------------------------------------


def test_dwell_reports_the_start_of_the_wait_not_its_middle() -> None:
    """A rider experiences the arrival when the bus pulls in, not mid wait."""
    pings = track_of(
        (0, 0),
        (100, 1000),  # arrives at the stop
        (130, 1005),  # sitting
        (160, 1010),  # still sitting
        (190, 1015),  # still sitting
        (250, 2000),  # gone
    )
    stops = (ScheduledStop(1, "A", 1000.0),)

    arrival = by_sequence(_run(pings, stops))[1]
    assert arrival.method == METHOD_DWELL
    assert seconds_after_t0(arrival) == pytest.approx(100.0, abs=1.0)
    assert arrival.departed_at is not None
    assert (arrival.departed_at - T0).total_seconds() == pytest.approx(190.0, abs=1.0)


def test_passing_a_stop_without_stopping_is_not_a_dwell() -> None:
    pings = track_of((0, 0), (100, 1000), (200, 2000))
    stops = (ScheduledStop(1, "A", 1500.0),)

    arrival = by_sequence(_run(pings, stops))[1]
    assert arrival.method == METHOD_INTERPOLATED
    assert arrival.departed_at is None


def test_a_single_fix_at_the_stop_is_not_treated_as_a_dwell() -> None:
    """One fix is a crossing, not evidence of waiting."""
    pings = track_of((0, 0), (100, 1000), (200, 2000))
    stops = (ScheduledStop(1, "A", 1000.0),)

    arrival = by_sequence(_run(pings, stops))[1]
    assert arrival.method == METHOD_AT_PING
    assert arrival.departed_at is None


def test_a_long_dwell_is_reported_in_full() -> None:
    """A vehicle genuinely sitting at a stop for ten minutes dwelled for ten
    minutes. The arrival is still the start of the wait.
    """
    pings = track_of((0, 1000), (30, 1010), (600, 1005))
    stops = (ScheduledStop(1, "A", 1000.0),)

    arrival = by_sequence(_run(pings, stops))[1]
    assert arrival.method == METHOD_DWELL
    assert seconds_after_t0(arrival) == pytest.approx(0.0, abs=1.0)
    assert arrival.departed_at is not None
    assert (arrival.departed_at - T0).total_seconds() == pytest.approx(600.0, abs=1.0)


def test_dwell_counts_only_the_consecutive_run() -> None:
    """Defensive: build_track emits a monotonic track, so a vehicle cannot return
    to a stop it has left. find_arrivals does not rely on that, and this pins the
    behaviour if a non-monotonic track is ever passed in.
    """
    from ontime_sd.arrivals import TrackPoint

    def at(seconds: float, offset: float) -> TrackPoint:
        return TrackPoint(
            ts=T0 + timedelta(seconds=seconds),
            offset_m=offset,
            raw_offset_m=offset,
            perpendicular_m=0.0,
        )

    # In range, in range, far away, in range again.
    handmade = [at(0, 1000), at(30, 1010), at(300, 5000), at(600, 1005)]
    arrivals, _ = find_arrivals(handmade, (ScheduledStop(1, "A", 1000.0),))

    assert arrivals[0].departed_at is not None
    departed = (arrivals[0].departed_at - T0).total_seconds()
    assert departed == pytest.approx(30.0, abs=1.0), "the later pass must not extend it"


# --- wide gaps ---------------------------------------------------------------


def test_a_wide_gap_still_interpolates_and_records_its_width() -> None:
    """p99 ping gap on real data is 843 seconds, so this is the normal bad case."""
    pings = track_of((0, 0), (900, 9000))
    stops = (ScheduledStop(1, "A", 4500.0),)

    arrival = by_sequence(_run(pings, stops, shape=straight_shape(10000)))[1]
    assert seconds_after_t0(arrival) == pytest.approx(450.0, abs=2.0)
    assert arrival.ping_gap_seconds == 900, "Phase 4 must be able to filter this out"


def test_gap_width_is_recorded_per_stop() -> None:
    """Two stops in the same trip can have very different label quality."""
    pings = track_of((0, 0), (30, 300), (930, 9300))
    stops = (ScheduledStop(1, "A", 150.0), ScheduledStop(2, "B", 5000.0))

    arrivals = by_sequence(_run(pings, stops, shape=straight_shape(10000)))
    assert arrivals[1].ping_gap_seconds == 30
    assert arrivals[2].ping_gap_seconds == 900


# --- noise and detours -------------------------------------------------------


def test_gps_jitter_beside_the_route_does_not_move_the_arrival() -> None:
    """A fix 20 m off the road still belongs at its distance along the route."""
    clean = track_of((0, 0), (100, 1000), (200, 2000))
    jittered = (
        ping(0, 0, lateral_m=15),
        ping(100, 1000, lateral_m=-20),
        ping(200, 2000, lateral_m=18),
    )
    stops = (ScheduledStop(1, "A", 1500.0),)

    assert seconds_after_t0(by_sequence(_run(jittered, stops))[1]) == pytest.approx(
        seconds_after_t0(by_sequence(_run(clean, stops))[1]), abs=2.0
    )


def test_a_fix_far_off_route_is_dropped() -> None:
    """A detour or a bad fix must not be snapped onto a route it was not on."""
    pings = (ping(0, 0), ping(100, 1000, lateral_m=800), ping(200, 2000))
    result = _run(pings, (ScheduledStop(1, "A", 1500.0),))

    assert result.pings_offroute == 1
    assert result.pings_used == 2


def test_backwards_noise_is_clamped_not_believed() -> None:
    """A bus does not drive its route in reverse, so a reversal is noise."""
    pings = track_of((0, 0), (100, 1000), (130, 940), (200, 2000))
    result = _run(pings, (ScheduledStop(1, "A", 1500.0),))

    assert result.clamped == 1
    times = [a.arrived_at for a in result.arrivals]
    assert times == sorted(times)


def test_clamping_is_counted_so_noise_is_measurable() -> None:
    pings = track_of((0, 0), (50, 500), (60, 450), (70, 400), (200, 2000))
    assert _run(pings, (ScheduledStop(1, "A", 1800.0),)).clamped == 2


# --- loop routes -------------------------------------------------------------


def test_a_loop_resolves_the_second_pass_to_the_later_stop() -> None:
    """The case that forbids a global nearest point search.

    This shape runs 1000 m north then back south over the same coordinates, so one
    latitude corresponds to two distances along the route. A stop at 250 m and
    another at 1750 m sit at the same place on the ground.
    """
    out = [
        ShapePoint(BASE_LAT + d * LAT_DEG_PER_M, BASE_LON, float(d)) for d in range(0, 1001, 250)
    ]
    back = [
        ShapePoint(BASE_LAT + (1000 - d) * LAT_DEG_PER_M, BASE_LON, float(1000 + d))
        for d in range(250, 1001, 250)
    ]
    shape = tuple(out + back)

    stops = (ScheduledStop(1, "A", 250.0), ScheduledStop(2, "B", 1750.0))

    # Out and back at 10 m/s: north for 100 s, then south for 100 s.
    pings = tuple(
        [ping(t, float(t * 10)) for t in range(0, 101, 20)]
        + [ping(100 + t, float(1000 - t * 10)) for t in range(20, 101, 20)]
    )

    arrivals = by_sequence(_run(pings, stops, shape=shape))
    assert set(arrivals) == {1, 2}
    # Outbound at 250 m is 25 s in; inbound at 1750 m is 175 s in.
    assert seconds_after_t0(arrivals[1]) == pytest.approx(25.0, abs=5.0)
    assert seconds_after_t0(arrivals[2]) == pytest.approx(175.0, abs=5.0)


# --- partial observation -----------------------------------------------------


def test_a_trip_observed_only_partway_yields_no_later_arrivals() -> None:
    """Not extrapolated. A missing label beats a fabricated one."""
    pings = track_of((0, 0), (100, 1000))
    stops = (
        ScheduledStop(1, "A", 500.0),
        ScheduledStop(2, "B", 2500.0),
        ScheduledStop(3, "C", 4500.0),
    )
    result = _run(pings, stops)

    assert set(by_sequence(result)) == {1}
    assert result.stops_skipped == 2


def test_a_stop_before_the_first_fix_is_not_invented() -> None:
    """The vehicle was already past it when observation began."""
    pings = track_of((0, 2000), (100, 3000))
    stops = (ScheduledStop(1, "A", 500.0), ScheduledStop(2, "B", 2500.0))

    assert set(by_sequence(_run(pings, stops))) == {2}


# --- degenerate input --------------------------------------------------------


def test_no_pings_reports_no_pings() -> None:
    result = _run((), (ScheduledStop(1, "A", 500.0),))
    assert result.status == "no_pings"
    assert result.arrivals == ()


def test_a_single_ping_cannot_bracket_anything() -> None:
    result = _run(track_of((0, 1000)), (ScheduledStop(1, "A", 500.0),))
    assert result.arrivals == ()
    assert result.stops_skipped == 1


def test_no_stops_reports_no_stops() -> None:
    result = _run(track_of((0, 0), (100, 1000)), ())
    assert result.status == "no_stops"


def test_a_shape_with_one_point_reports_no_shape() -> None:
    result = _run(
        track_of((0, 0), (100, 1000)),
        (ScheduledStop(1, "A", 500.0),),
        shape=(ShapePoint(BASE_LAT, BASE_LON, 0.0),),
    )
    assert result.status == "no_shape"


def test_observed_but_never_reaching_a_stop_is_distinct_from_unobserved() -> None:
    """status no_arrivals, not no_pings: the trip ran, we just learned nothing."""
    pings = track_of((0, 0), (30, 100))
    result = _run(pings, (ScheduledStop(1, "A", 4000.0),))

    assert result.status == "no_arrivals"
    assert result.pings_used == 2


# --- find_arrivals directly --------------------------------------------------


def test_find_arrivals_needs_at_least_two_track_points() -> None:
    arrivals, skipped = find_arrivals([], (ScheduledStop(1, "A", 100.0),))
    assert arrivals == []
    assert skipped == 1


def test_build_track_places_pings_in_order() -> None:
    track, offroute, clamped = build_track(
        track_of((0, 0), (100, 1000), (200, 2000)), straight_shape()
    )
    assert [round(p.offset_m) for p in track] == [0, 1000, 2000]
    assert (offroute, clamped) == (0, 0)


def test_build_track_keeps_the_raw_offset_for_inspection() -> None:
    """So the amount of clamping can be audited rather than just counted."""
    track, _, _ = build_track(track_of((0, 1000), (30, 940)), straight_shape())
    assert track[1].offset_m == pytest.approx(1000.0, abs=5.0)
    assert track[1].raw_offset_m == pytest.approx(940.0, abs=5.0)
