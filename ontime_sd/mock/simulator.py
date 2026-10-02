"""Synthetic moving buses in GTFS-Realtime format.

Exists so the collector, schema, backoff, and health checks can be proven
correct before the MTS API key arrives. See DESIGN.md ADR-0012.

What this does not do: GPS noise, dropped pings, detours, or trips that never
finish. Those are exactly what makes Phase 3 arrival inference hard, and
simulating them would risk building inference that only works against my own
idea of messy data. This proves the pipeline runs. Inference quality waits for
real data.

Everything here is a pure function of the timestamp passed in, so the same
instant always produces the same feed. That is what lets tests assert on feed
contents without controlling a clock.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from google.transit import gtfs_realtime_pb2 as gtfs_rt

from ontime_sd.config import SERVICE_TZ

# A plausible north-south corridor, downtown San Diego up to UCSD. Real
# coordinates so that anything plotted on a map looks sane, but this is not a
# real MTS route and is not pretending to be.
DEFAULT_SHAPE: tuple[tuple[float, float], ...] = (
    (32.7157, -117.1700),  # Santa Fe Depot
    (32.7270, -117.1690),
    (32.7545, -117.1975),  # Old Town
    (32.7900, -117.2020),
    (32.8200, -117.2100),  # Clairemont
    (32.8480, -117.2160),
    (32.8700, -117.2200),  # La Jolla Village
    (32.8800, -117.2340),  # UCSD
)

STOP_COUNT = 12
DWELL_SECONDS = 20.0
STOPPED_RADIUS_M = 40.0
EARTH_RADIUS_M = 6_371_000.0


def _haversine_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlat, dlon = lat2 - lat1, lon2 - lon1
    h = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(h))


def _bearing_deg(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1 = math.radians(a[0]), math.radians(a[1])
    lat2, lon2 = math.radians(b[0]), math.radians(b[1])
    dlon = lon2 - lon1
    y = math.sin(dlon) * math.cos(lat2)
    x = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def _unit_noise(*parts: object) -> float:
    """Deterministic pseudo-random float in [-1, 1) from the given key parts.

    Python's builtin hash is salted per process, so it cannot be used: the mock
    has to produce the same feed for the same instant across restarts.
    """
    key = "|".join(str(p) for p in parts).encode()
    digest = hashlib.sha256(key).digest()
    (value,) = struct.unpack_from("<Q", digest)
    return (value / (2**63)) - 1.0


@dataclass(frozen=True, slots=True)
class Stop:
    stop_id: str
    stop_sequence: int
    distance_m: float
    lat: float
    lon: float


@dataclass(frozen=True, slots=True)
class Route:
    route_id: str
    shape: tuple[tuple[float, float], ...]
    cumulative_m: tuple[float, ...]
    stops: tuple[Stop, ...]

    @property
    def length_m(self) -> float:
        return self.cumulative_m[-1]

    def position_at(self, distance_m: float) -> tuple[float, float, float]:
        """Interpolate (lat, lon, bearing) at a distance along the shape."""
        distance_m = min(max(distance_m, 0.0), self.length_m)

        # Linear scan is fine: the shape has a handful of vertices.
        index = 0
        for i in range(len(self.cumulative_m) - 1):
            if self.cumulative_m[i + 1] >= distance_m:
                index = i
                break
        else:
            index = len(self.shape) - 2

        start, end = self.shape[index], self.shape[index + 1]
        segment_m = self.cumulative_m[index + 1] - self.cumulative_m[index]
        fraction = 0.0 if segment_m == 0 else (distance_m - self.cumulative_m[index]) / segment_m

        lat = start[0] + (end[0] - start[0]) * fraction
        lon = start[1] + (end[1] - start[1]) * fraction
        return lat, lon, _bearing_deg(start, end)

    def next_stop_index(self, distance_m: float) -> int:
        """Index of the first stop at or ahead of this distance."""
        for i, stop in enumerate(self.stops):
            if stop.distance_m >= distance_m:
                return i
        return len(self.stops) - 1


def build_route(
    route_id: str = "MOCK-1",
    shape: tuple[tuple[float, float], ...] = DEFAULT_SHAPE,
    stop_count: int = STOP_COUNT,
) -> Route:
    cumulative = [0.0]
    for a, b in itertools.pairwise(shape):
        cumulative.append(cumulative[-1] + _haversine_m(a, b))

    route = Route(route_id=route_id, shape=shape, cumulative_m=tuple(cumulative), stops=())
    total = cumulative[-1]

    stops = []
    for i in range(stop_count):
        # Stops span the whole shape, first at the start, last at the end.
        distance = total * i / (stop_count - 1)
        lat, lon, _ = route.position_at(distance)
        stops.append(
            Stop(
                stop_id=f"MOCK_STOP_{i + 1:02d}",
                stop_sequence=i + 1,
                distance_m=distance,
                lat=lat,
                lon=lon,
            )
        )

    return Route(route_id=route_id, shape=shape, cumulative_m=tuple(cumulative), stops=tuple(stops))


@dataclass(frozen=True, slots=True)
class Vehicle:
    vehicle_id: str
    offset_m: float
    speed_mps: float
    # A standing bias, so some buses run reliably late the way real ones do.
    delay_bias_s: float


@dataclass(frozen=True, slots=True)
class VehicleState:
    vehicle: Vehicle
    lap: int
    distance_m: float
    lat: float
    lon: float
    bearing: float
    next_stop_index: int
    stopped: bool

    @property
    def trip_id(self) -> str:
        # Each lap is a distinct trip, so trips actually begin and end rather
        # than one immortal trip per bus.
        return f"MOCK_TRIP_{self.vehicle.vehicle_id}_{self.lap:04d}"


# Feed shapes.
#
# SHAPE_MTS mirrors what the real MTS feed actually sends, measured from
# .pbtext the day the API key arrived. It is the default, because a mock that
# emits fields the real feed omits gives false confidence: it hid the fact that
# every prediction was being dropped for a missing stop_sequence, and it made
# change-only storage look far more effective than it is. See ADR-0034.
#
# SHAPE_RICH populates every optional field, which keeps the parser's fallback
# paths under test. A conforming GTFS-Realtime feed may send them even though
# MTS does not.
SHAPE_MTS = "mts"
SHAPE_RICH = "rich"
FEED_SHAPES = (SHAPE_MTS, SHAPE_RICH)

# Fraction of stop_time_updates carrying a delay. The real feed had 30 of 7,750.
_DELAY_FRACTION = 0.004


@dataclass(slots=True)
class Simulator:
    vehicle_count: int = 40
    route: Route = field(default_factory=build_route)
    # Feed epoch. Distances are measured from here so output is reproducible.
    epoch: datetime = field(default_factory=lambda: datetime(2026, 1, 1, tzinfo=SERVICE_TZ))
    upcoming_stops: int = 8
    # How often the simulated prediction drifts. Longer than one poll interval
    # so consecutive polls often agree, which is what change-only storage needs
    # to actually compress.
    drift_period_s: int = 120
    vehicles: tuple[Vehicle, ...] = field(init=False)

    def __post_init__(self) -> None:
        vehicles = []
        length = self.route.length_m
        for i in range(self.vehicle_count):
            # Spread buses evenly along the corridor with varied speeds, so
            # they bunch and separate the way a real headway does.
            speed = 9.0 + 3.0 * _unit_noise("speed", i)
            vehicles.append(
                Vehicle(
                    vehicle_id=f"MOCK_BUS_{i + 1:03d}",
                    offset_m=length * i / max(self.vehicle_count, 1),
                    speed_mps=speed,
                    delay_bias_s=90.0 * _unit_noise("bias", i),
                )
            )
        self.vehicles = tuple(vehicles)

    # --- kinematics ---

    def state_at(self, vehicle: Vehicle, now: datetime) -> VehicleState:
        elapsed = (now - self.epoch).total_seconds()
        travelled = vehicle.offset_m + vehicle.speed_mps * elapsed
        length = self.route.length_m

        lap, distance = divmod(travelled, length)
        lat, lon, bearing = self.route.position_at(distance)
        index = self.route.next_stop_index(distance)
        stop = self.route.stops[index]

        return VehicleState(
            vehicle=vehicle,
            lap=int(lap),
            distance_m=distance,
            lat=lat,
            lon=lon,
            bearing=bearing,
            next_stop_index=index,
            stopped=abs(stop.distance_m - distance) <= STOPPED_RADIUS_M,
        )

    def states_at(self, now: datetime) -> list[VehicleState]:
        return [self.state_at(v, now) for v in self.vehicles]

    def _predicted_arrival(
        self, state: VehicleState, stop: Stop, now: datetime
    ) -> tuple[datetime, float]:
        """Predicted arrival at a stop, and the delay against schedule.

        The drift term changes on a slow cycle and is sometimes above and
        sometimes below the collector's 30 second write threshold, which is what
        makes change-only storage testable rather than trivially true.
        """
        remaining_m = max(stop.distance_m - state.distance_m, 0.0)
        travel_s = remaining_m / state.vehicle.speed_mps
        dwell_s = DWELL_SECONDS * max(stop.stop_sequence - state.next_stop_index, 0)

        bucket = int((now - self.epoch).total_seconds() // self.drift_period_s)
        drift_s = 75.0 * _unit_noise("drift", state.trip_id, stop.stop_sequence, bucket)
        delay_s = state.vehicle.delay_bias_s + drift_s

        arrival = now + timedelta(seconds=travel_s + dwell_s + delay_s)
        return arrival, delay_s

    # --- feed construction ---

    def _header(self, message: gtfs_rt.FeedMessage, feed_timestamp: datetime) -> None:
        message.header.gtfs_realtime_version = "2.0"
        message.header.incrementality = gtfs_rt.FeedHeader.FULL_DATASET
        message.header.timestamp = int(feed_timestamp.timestamp())

    def vehicle_positions(
        self,
        now: datetime,
        feed_timestamp: datetime | None = None,
        shape: str = SHAPE_MTS,
    ) -> gtfs_rt.FeedMessage:
        if shape not in FEED_SHAPES:
            raise ValueError(f"unknown shape {shape!r}, expected one of {FEED_SHAPES}")

        message = gtfs_rt.FeedMessage()
        self._header(message, feed_timestamp or now)

        for state in self.states_at(now):
            entity = message.entity.add()
            entity.id = f"vp-{state.vehicle.vehicle_id}"

            position = entity.vehicle
            # Everything below this comment is what the real feed sends.
            position.trip.trip_id = state.trip_id
            position.trip.route_id = self.route.route_id
            position.vehicle.id = state.vehicle.vehicle_id
            position.position.latitude = state.lat
            position.position.longitude = state.lon
            position.timestamp = int(now.timestamp())

            if shape == SHAPE_RICH:
                # None of these appear in the real MTS feed. Phase 3 therefore
                # cannot rely on current_stop_sequence or current_status and has
                # to snap GPS to the route shape instead.
                position.trip.start_date = now.astimezone(SERVICE_TZ).strftime("%Y%m%d")
                position.position.bearing = state.bearing
                position.position.speed = 0.0 if state.stopped else state.vehicle.speed_mps
                position.current_stop_sequence = self.route.stops[
                    state.next_stop_index
                ].stop_sequence
                position.current_status = (
                    gtfs_rt.VehiclePosition.STOPPED_AT
                    if state.stopped
                    else gtfs_rt.VehiclePosition.IN_TRANSIT_TO
                )

        return message

    def trip_updates(
        self,
        now: datetime,
        feed_timestamp: datetime | None = None,
        shape: str = SHAPE_MTS,
    ) -> gtfs_rt.FeedMessage:
        if shape not in FEED_SHAPES:
            raise ValueError(f"unknown shape {shape!r}, expected one of {FEED_SHAPES}")

        message = gtfs_rt.FeedMessage()
        self._header(message, feed_timestamp or now)

        for state in self.states_at(now):
            entity = message.entity.add()
            entity.id = f"tu-{state.vehicle.vehicle_id}"

            update = entity.trip_update
            update.trip.trip_id = state.trip_id
            update.trip.route_id = self.route.route_id
            update.vehicle.id = state.vehicle.vehicle_id
            update.timestamp = int(now.timestamp())

            if shape == SHAPE_RICH:
                update.trip.start_date = now.astimezone(SERVICE_TZ).strftime("%Y%m%d")

            upcoming = self.route.stops[
                state.next_stop_index : state.next_stop_index + self.upcoming_stops
            ]
            for stop in upcoming:
                arrival, delay_s = self._predicted_arrival(state, stop, now)
                stop_time = update.stop_time_update.add()

                # The real feed identifies a stop by stop_id alone and sends
                # absolute times. stop_sequence never appears, which is why it
                # has to be recovered from the schedule. See ADR-0033.
                stop_time.stop_id = stop.stop_id
                stop_time.arrival.time = int(arrival.timestamp())
                stop_time.departure.time = int(arrival.timestamp() + DWELL_SECONDS)

                # Rare in the real feed: 30 of 7,750 stop time updates.
                if _unit_noise("delay", state.trip_id, stop.stop_sequence) > (
                    1.0 - 2.0 * _DELAY_FRACTION
                ):
                    stop_time.arrival.delay = int(delay_s)

                if shape == SHAPE_RICH:
                    stop_time.stop_sequence = stop.stop_sequence
                    stop_time.arrival.delay = int(delay_s)
                    stop_time.schedule_relationship = gtfs_rt.TripUpdate.StopTimeUpdate.SCHEDULED

        return message
