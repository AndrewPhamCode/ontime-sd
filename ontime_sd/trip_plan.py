"""Trip planning: which stop to walk to, and when to be there.

Direct rides only, one bus or trolley from a stop near the origin to a stop near
the destination. The database work lives in the API; this module holds the parts
that are pure arithmetic and decisions, so they can be tested without one. See
DESIGN.md ADR-0052.

The answer a rider acts on is "be at the stop by", and the failure that strands
them is a bus that comes early. So that time is the departure estimate minus a
margin taken from the early tail of the measured error, not from the mean: the
mean is exactly the number that ignores the buses that leave without you.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

EARTH_RADIUS_M = 6_371_000.0

# Street distance is longer than straight line distance. 1.3 is a common planning
# factor for a street grid, and San Diego's canyons make it optimistic in places,
# which is why the margin below exists at all.
DETOUR_FACTOR = 1.3

# About 4.7 km/h, a normal adult walking pace.
WALK_SPEED_MPS = 1.3

DEFAULT_MAX_WALK_M = 800
MAX_WALK_M_LIMIT = 1500

# Nearest stops considered at each end. Enough to cover a corner with stops on
# all four sides plus the next block, few enough that the join stays small.
MAX_STOPS_PER_END = 12

# How far ahead "leave now" looks, and how far back from the deadline "arrive by"
# looks. Beyond these the answer is a timetable, not a plan.
LEAVE_NOW_WINDOW = timedelta(minutes=90)
ARRIVE_BY_WINDOW = timedelta(hours=2)

# Below this, a margin is false precision: the walk estimate alone is less exact.
MIN_MARGIN_SECONDS = 60

# With no measured tail to go on, either because the ride is schedule only or the
# route has too little history, a fixed margin that is stated as fixed.
DEFAULT_MARGIN_SECONDS = 120

# A prediction this old is no longer evidence that MTS is tracking the trip. The
# store is change only (ADR-0005), so an unchanged prediction can legitimately be
# a few minutes old, but half an hour of silence usually means the trip dropped
# out of the feed.
LIVE_PREDICTION_MAX_AGE = timedelta(minutes=30)

BASIS_CORRECTED = "corrected"
BASIS_MTS = "mts"
BASIS_SCHEDULED = "scheduled"

MARGIN_MEASURED = "measured"
MARGIN_DEFAULT = "default"


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great circle distance in metres."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def walk_seconds(distance_m: float) -> int:
    """Estimated walking time for a straight line distance, rounded up."""
    return math.ceil(distance_m * DETOUR_FACTOR / WALK_SPEED_MPS)


def bounding_box(lat: float, lon: float, radius_m: float) -> tuple[float, float, float, float]:
    """A lat/lon box containing every point within radius_m.

    A cheap prefilter so the exact distance is only computed for a handful of
    stops. A degree of longitude shrinks with latitude, hence the cosine.
    """
    dlat = math.degrees(radius_m / EARTH_RADIUS_M)
    dlon = math.degrees(radius_m / (EARTH_RADIUS_M * math.cos(math.radians(lat))))
    return lat - dlat, lat + dlat, lon - dlon, lon + dlon


@dataclass(frozen=True)
class NearbyStop:
    stop_id: str
    stop_name: str | None
    lat: float
    lon: float
    distance_m: float
    walk_seconds: int


def nearest_stops(
    lat: float,
    lon: float,
    candidates: list[tuple[str, str | None, float, float]],
    max_walk_m: float,
    limit: int = MAX_STOPS_PER_END,
) -> list[NearbyStop]:
    """Stops within walking distance, nearest first.

    `candidates` is (stop_id, name, lat, lon), typically the bounding box hits.
    The box is a square and the walk limit is a circle, so the corners are cut
    here.
    """
    found = []
    for stop_id, name, stop_lat, stop_lon in candidates:
        distance = haversine_m(lat, lon, stop_lat, stop_lon)
        if distance <= max_walk_m:
            found.append(
                NearbyStop(stop_id, name, stop_lat, stop_lon, distance, walk_seconds(distance))
            )
    found.sort(key=lambda s: (s.distance_m, s.stop_id))
    return found[:limit]


def choose_margin(
    departure_basis: str,
    route_tail: tuple[int, float, float] | None,
    min_sample: int,
) -> tuple[int, str]:
    """Seconds to be at the stop ahead of the estimated departure, and why.

    `route_tail` is (n, mean, p90) of the route's signed error, where positive
    means the bus came before MTS said it would.

    - Against MTS's raw estimate, the margin is the p90 itself: one bus in ten
      came at least that much earlier than MTS said.
    - Against our corrected estimate, the mean has already been removed, so what
      is left is the spread above it: p90 minus mean.
    - Against the schedule, there is no measured tail to use, so the fixed
      default, labelled as such rather than dressed up as a measurement.
    """
    if departure_basis == BASIS_SCHEDULED or route_tail is None or route_tail[0] < min_sample:
        return DEFAULT_MARGIN_SECONDS, MARGIN_DEFAULT
    _n, mean, p90 = route_tail
    tail = p90 - mean if departure_basis == BASIS_CORRECTED else p90
    return max(MIN_MARGIN_SECONDS, math.ceil(tail)), MARGIN_MEASURED


@dataclass(frozen=True)
class Timing:
    departs_at: datetime
    be_at_stop_by: datetime
    leave_by: datetime
    arrives_at: datetime
    arrive_at_destination: datetime


def timing(
    departs_at: datetime,
    arrives_at: datetime,
    margin_seconds: int,
    walk_to_stop_seconds: int,
    walk_from_stop_seconds: int,
) -> Timing:
    be_at_stop_by = departs_at - timedelta(seconds=margin_seconds)
    return Timing(
        departs_at=departs_at,
        be_at_stop_by=be_at_stop_by,
        leave_by=be_at_stop_by - timedelta(seconds=walk_to_stop_seconds),
        arrives_at=arrives_at,
        arrive_at_destination=arrives_at + timedelta(seconds=walk_from_stop_seconds),
    )


def is_feasible(t: Timing, now: datetime, arrive_by: datetime | None) -> bool:
    """Can the rider still make this, and does it get them there in time?

    The SQL already filters on the schedule. This repeats the check on the live
    estimate and the margin, which the SQL could not know: a bus running early may
    no longer be reachable, and one running late may now miss the deadline.
    """
    if t.leave_by < now:
        return False
    return arrive_by is None or t.arrive_at_destination <= arrive_by


def rank_key(t: Timing, arrive_by: datetime | None) -> tuple:
    """Leave now: get there first. Arrive by: leave as late as possible.

    Ties go to the shorter trip door to door, so the rider is not told to set
    off earlier for the same result.
    """
    door_to_door = t.arrive_at_destination - t.leave_by
    if arrive_by is None:
        return (t.arrive_at_destination, door_to_door)
    return (-t.leave_by.timestamp(), door_to_door)
