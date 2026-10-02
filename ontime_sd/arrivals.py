"""Phase 3: reconstructing when each vehicle actually reached each stop.

This derives the ground truth the project is built on. Phase 4 scores MTS's
predictions against these arrivals and Phase 5 trains on them, so accuracy here
caps accuracy everywhere downstream.

The approach follows from what the real MTS feed actually provides, measured rather
than assumed. A position report carries a vehicle id, a trip id, a coordinate and a
timestamp. There is no speed, no bearing, and no "stopped at stop" flag, so the
only route to an arrival time is geometric: work out where along its route each
GPS fix puts the vehicle, then find when it passed each stop's known position.

What makes that tractable is that MTS populates `shape_dist_traveled` on both
shapes and stop times, so every stop already has a distance along its route. The
geometry therefore reduces to projecting a point onto a polyline and comparing one
number. See DESIGN.md ADR-0036.

Four stages, each a pure function so each can be tested alone:

  1. project each ping onto the route shape      -> distance along route
  2. build a monotonic distance versus time track -> noise and detours handled
  3. find each stop crossing and interpolate      -> arrival times
  4. detect dwell                                 -> arrival at the stop, not mid-dwell
"""

from __future__ import annotations

import bisect
import itertools
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta

log = logging.getLogger(__name__)

EARTH_RADIUS_M = 6_371_000.0

# Bounds the forward search when projecting a ping: a vehicle cannot have advanced
# further than this since the previous fix. Generous, since the trolley is faster
# than a bus and a wide ping gap legitimately covers a lot of route.
MAX_SPEED_MPS = 35.0

# How far back along the route a ping may snap relative to the previous one. GPS
# noise and genuine small reversals at terminals need some slack, but a loop route
# revisits the same coordinates and must never snap back to an earlier pass.
BACKTRACK_M = 300.0

# A fix further than this from the route is treated as off route: a detour, a
# vehicle deadheading, or a bad fix. Snapping it would invent a position.
MAX_OFFROUTE_M = 150.0

# A vehicle within this distance of a stop's position is considered at the stop
# rather than passing it, which is what distinguishes a dwell from a crossing.
DWELL_RADIUS_M = 40.0

# Treat a ping this close to a stop as landing on it, rather than interpolating
# across a span of essentially zero.
AT_STOP_M = 15.0

METHOD_INTERPOLATED = "interpolated"
METHOD_DWELL = "dwell"
METHOD_AT_PING = "at_ping"


# --- inputs -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Ping:
    ts: datetime
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class ShapePoint:
    lat: float
    lon: float
    offset_m: float


@dataclass(frozen=True, slots=True)
class ScheduledStop:
    stop_sequence: int
    stop_id: str
    offset_m: float


@dataclass(frozen=True, slots=True)
class TripTrack:
    start_date: date
    trip_id: str
    vehicle_id: str
    feed_version: str
    pings: tuple[Ping, ...]
    shape: tuple[ShapePoint, ...]
    stops: tuple[ScheduledStop, ...]


# --- outputs ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class TrackPoint:
    """One ping, placed along the route."""

    ts: datetime
    offset_m: float
    # Before monotonic clamping, kept so noise is measurable rather than hidden.
    raw_offset_m: float
    perpendicular_m: float


@dataclass(frozen=True, slots=True)
class Arrival:
    stop_sequence: int
    stop_id: str
    arrived_at: datetime
    departed_at: datetime | None
    method: str
    ping_gap_seconds: int | None
    nearest_ping_m: float | None
    stop_offset_m: float


@dataclass(frozen=True, slots=True)
class InferenceResult:
    arrivals: tuple[Arrival, ...]
    pings_used: int
    pings_offroute: int
    clamped: int
    stops_skipped: int
    status: str
    reason: str | None = None


# --- stage 1: geometry --------------------------------------------------------


def _local_metres(lat: float, lon: float, lat0: float) -> tuple[float, float]:
    """Equirectangular projection to metres around a reference latitude.

    Accurate to well under a metre over the span of a single shape segment, which
    is all this is used for, and far cheaper than a full geodesic calculation per
    candidate segment.
    """
    x = math.radians(lon) * EARTH_RADIUS_M * math.cos(math.radians(lat0))
    y = math.radians(lat) * EARTH_RADIUS_M
    return x, y


def project_onto_segment(
    lat: float, lon: float, start: ShapePoint, end: ShapePoint
) -> tuple[float, float]:
    """Project a point onto one shape segment.

    Returns the distance along the route at the closest point, and the
    perpendicular distance from the point to the segment, both in metres.
    """
    lat0 = (start.lat + end.lat) / 2.0
    px, py = _local_metres(lat, lon, lat0)
    ax, ay = _local_metres(start.lat, start.lon, lat0)
    bx, by = _local_metres(end.lat, end.lon, lat0)

    dx, dy = bx - ax, by - ay
    length_sq = dx * dx + dy * dy

    if length_sq == 0.0:
        # Degenerate segment: the two shape points coincide.
        return start.offset_m, math.hypot(px - ax, py - ay)

    # Fraction along the segment of the closest point, clamped to the segment so a
    # point beyond either end projects to that end rather than off the line.
    t = ((px - ax) * dx + (py - ay) * dy) / length_sq
    t = min(max(t, 0.0), 1.0)

    closest_x, closest_y = ax + t * dx, ay + t * dy
    offset = start.offset_m + t * (end.offset_m - start.offset_m)
    return offset, math.hypot(px - closest_x, py - closest_y)


def project_onto_shape(
    lat: float,
    lon: float,
    shape: tuple[ShapePoint, ...],
    offsets: list[float],
    *,
    min_offset: float | None = None,
    max_offset: float | None = None,
) -> tuple[float, float]:
    """Project a point onto the whole shape, within an optional offset window.

    The window is what makes loop routes work. A route that passes the same
    coordinate twice would otherwise snap a late fix back to the earlier pass, so
    the search is bounded to the stretch the vehicle could plausibly be on.
    """
    if len(shape) < 2:
        return (shape[0].offset_m if shape else 0.0), math.inf

    first = 0 if min_offset is None else max(0, bisect.bisect_left(offsets, min_offset) - 1)
    last = len(shape) - 1
    if max_offset is not None:
        last = min(last, bisect.bisect_right(offsets, max_offset) + 1)

    best_offset, best_perpendicular = shape[first].offset_m, math.inf
    for index in range(first, min(last, len(shape) - 1)):
        offset, perpendicular = project_onto_segment(lat, lon, shape[index], shape[index + 1])
        if perpendicular < best_perpendicular:
            best_offset, best_perpendicular = offset, perpendicular

    return best_offset, best_perpendicular


# --- stage 2: the monotonic track --------------------------------------------


def build_track(
    pings: tuple[Ping, ...],
    shape: tuple[ShapePoint, ...],
    *,
    max_offroute_m: float = MAX_OFFROUTE_M,
    backtrack_m: float = BACKTRACK_M,
    max_speed_mps: float = MAX_SPEED_MPS,
) -> tuple[list[TrackPoint], int, int]:
    """Place each ping along the route, in order.

    Returns the track, how many pings were dropped as off route, and how many had
    to be clamped forward.

    Distance along the route is treated as non-decreasing, because a vehicle does
    not drive its route backwards. GPS noise makes the raw projection wobble, and
    clamping is what keeps a wobble from producing an arrival earlier than the
    arrival before it.
    """
    track: list[TrackPoint] = []
    offsets = [point.offset_m for point in shape]
    offroute = 0
    clamped = 0

    previous: TrackPoint | None = None

    for ping in pings:
        if previous is None:
            window_min, window_max = None, None
        else:
            elapsed = (ping.ts - previous.ts).total_seconds()
            reach = max(elapsed, 0.0) * max_speed_mps
            window_min = previous.offset_m - backtrack_m
            window_max = previous.offset_m + reach + backtrack_m

        raw_offset, perpendicular = project_onto_shape(
            ping.lat,
            ping.lon,
            shape,
            offsets,
            min_offset=window_min,
            max_offset=window_max,
        )

        if perpendicular > max_offroute_m:
            # Off route: a detour, a deadheading vehicle, or a bad fix. Including
            # it would invent a position somewhere on the route it was not on.
            offroute += 1
            continue

        offset = raw_offset
        if previous is not None and offset < previous.offset_m:
            offset = previous.offset_m
            clamped += 1

        point = TrackPoint(
            ts=ping.ts,
            offset_m=offset,
            raw_offset_m=raw_offset,
            perpendicular_m=perpendicular,
        )
        track.append(point)
        previous = point

    return track, offroute, clamped


# --- stages 3 and 4: crossings and dwell -------------------------------------


def _interpolate_time(before: TrackPoint, after: TrackPoint, stop_offset_m: float) -> datetime:
    """Linear interpolation of the moment the vehicle passed a distance."""
    span = after.offset_m - before.offset_m
    if span <= 0:
        return before.ts
    fraction = (stop_offset_m - before.offset_m) / span
    fraction = min(max(fraction, 0.0), 1.0)
    return before.ts + timedelta(seconds=(after.ts - before.ts).total_seconds() * fraction)


def _dwell_at(
    track: list[TrackPoint], stop_offset_m: float, dwell_radius_m: float
) -> tuple[datetime, datetime] | None:
    """The span a vehicle was observed sitting at a stop, if it was.

    A vehicle waiting at a stop reports several fixes in essentially one place,
    which is visible in the raw data. Interpolating across that cluster would put
    the arrival in the middle of the wait rather than at its start, and the start
    is what a rider experiences as the arrival.

    Only the run of consecutive in-range points is considered, so a route that
    passes near the stop again later does not extend the dwell.
    """
    inside = [
        index
        for index, point in enumerate(track)
        if abs(point.offset_m - stop_offset_m) <= dwell_radius_m
    ]
    if len(inside) < 2:
        return None

    run_start = inside[0]
    run_end = inside[0]
    for index in inside[1:]:
        if index == run_end + 1:
            run_end = index
        else:
            break

    if run_end == run_start:
        return None
    return track[run_start].ts, track[run_end].ts


def find_arrivals(
    track: list[TrackPoint],
    stops: tuple[ScheduledStop, ...],
    *,
    dwell_radius_m: float = DWELL_RADIUS_M,
    at_stop_m: float = AT_STOP_M,
) -> tuple[list[Arrival], int]:
    """Work out when the vehicle reached each stop. Returns arrivals and skips.

    A stop with no pair of pings bracketing it produces no arrival. It is not
    extrapolated: with ping gaps reaching 843 seconds at p99 on real data,
    extrapolation would manufacture plausible looking times that are badly wrong,
    and Phase 5 would train on them. A missing label costs one row; a wrong label
    corrupts the model.
    """
    arrivals: list[Arrival] = []
    skipped = 0

    if len(track) < 2:
        return arrivals, len(stops)

    for stop in stops:
        target = stop.offset_m
        if target is None:
            skipped += 1
            continue

        nearest = min(abs(point.offset_m - target) for point in track)

        dwell = _dwell_at(track, target, dwell_radius_m)
        if dwell is not None:
            arrived, departed = dwell
            arrivals.append(
                Arrival(
                    stop_sequence=stop.stop_sequence,
                    stop_id=stop.stop_id,
                    arrived_at=arrived,
                    departed_at=departed if departed > arrived else None,
                    method=METHOD_DWELL,
                    ping_gap_seconds=0,
                    nearest_ping_m=nearest,
                    stop_offset_m=target,
                )
            )
            continue

        # The bracketing pair: the vehicle was before the stop, then past it.
        bracket: tuple[TrackPoint, TrackPoint] | None = None
        for before, after in itertools.pairwise(track):
            if before.offset_m <= target <= after.offset_m:
                bracket = (before, after)
                break

        if bracket is None:
            skipped += 1
            continue

        before, after = bracket
        gap_seconds = round((after.ts - before.ts).total_seconds())

        if nearest <= at_stop_m:
            # A fix landed on the stop, so there is nothing to interpolate.
            closest = min(track, key=lambda point: abs(point.offset_m - target))
            arrivals.append(
                Arrival(
                    stop_sequence=stop.stop_sequence,
                    stop_id=stop.stop_id,
                    arrived_at=closest.ts,
                    departed_at=None,
                    method=METHOD_AT_PING,
                    ping_gap_seconds=gap_seconds,
                    nearest_ping_m=nearest,
                    stop_offset_m=target,
                )
            )
            continue

        arrivals.append(
            Arrival(
                stop_sequence=stop.stop_sequence,
                stop_id=stop.stop_id,
                arrived_at=_interpolate_time(before, after, target),
                departed_at=None,
                method=METHOD_INTERPOLATED,
                ping_gap_seconds=gap_seconds,
                nearest_ping_m=nearest,
                stop_offset_m=target,
            )
        )

    return arrivals, skipped


# --- the whole inference ------------------------------------------------------


def infer_arrivals(trip: TripTrack, **options: float) -> InferenceResult:
    """Reconstruct arrivals for one vehicle's run of one trip."""
    if not trip.pings:
        return InferenceResult((), 0, 0, 0, len(trip.stops), "no_pings")
    if len(trip.shape) < 2:
        return InferenceResult((), len(trip.pings), 0, 0, len(trip.stops), "no_shape")
    if not trip.stops:
        return InferenceResult((), len(trip.pings), 0, 0, 0, "no_stops")

    build_options = {
        key: value
        for key, value in options.items()
        if key in {"max_offroute_m", "backtrack_m", "max_speed_mps"}
    }
    find_options = {
        key: value for key, value in options.items() if key in {"dwell_radius_m", "at_stop_m"}
    }

    track, offroute, clamped = build_track(trip.pings, trip.shape, **build_options)
    arrivals, skipped = find_arrivals(track, trip.stops, **find_options)

    status = "ok" if arrivals else "no_arrivals"
    return InferenceResult(
        arrivals=tuple(arrivals),
        pings_used=len(track),
        pings_offroute=offroute,
        clamped=clamped,
        stops_skipped=skipped,
        status=status,
    )
