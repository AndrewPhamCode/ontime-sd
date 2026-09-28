"""Fetching and parsing GTFS-Realtime feeds into rows ready for insert.

Parsing is deliberately defensive. Almost every field in GTFS-Realtime is
optional, and a feed is free to omit things this project needs. Where a missing
field can be substituted safely there is a documented fallback; where it cannot,
the entity is dropped and counted rather than written as a broken row.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime

import httpx
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2 as gtfs_rt

from ontime_sd.config import SERVICE_TZ

log = logging.getLogger(__name__)


class FeedError(Exception):
    """Base class for anything that makes a poll fail."""


class FeedHTTPError(FeedError):
    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"feed returned HTTP {status_code}")
        self.status_code = status_code
        self.url = url


class FeedTransportError(FeedError):
    """Connection refused, DNS failure, timeout: no HTTP status exists."""


class FeedParseError(FeedError):
    """The bytes were not a parseable FeedMessage."""


@dataclass(frozen=True, slots=True)
class VehiclePositionRow:
    vehicle_id: str
    ts: datetime
    trip_id: str | None
    route_id: str | None
    start_date: date | None
    lat: float | None
    lon: float | None
    bearing: float | None
    speed: float | None
    current_stop_sequence: int | None
    current_status: int | None
    occupancy_status: int | None


@dataclass(frozen=True, slots=True)
class PredictionRow:
    start_date: date
    trip_id: str
    stop_sequence: int
    observed_at: datetime
    stop_id: str | None
    route_id: str | None
    arrival_time: datetime | None
    departure_time: datetime | None
    delay_seconds: int | None
    schedule_relationship: int | None
    vehicle_id: str | None

    @property
    def cache_key(self) -> tuple[date, str, int]:
        return (self.start_date, self.trip_id, self.stop_sequence)


@dataclass(frozen=True, slots=True)
class ParsedFeed:
    """Rows extracted from one feed response, plus what had to be dropped."""

    feed_timestamp: datetime | None
    entity_count: int
    positions: tuple[VehiclePositionRow, ...] = ()
    predictions: tuple[PredictionRow, ...] = ()
    dropped: int = 0
    drop_reasons: tuple[str, ...] = ()


async def fetch_feed(client: httpx.AsyncClient, url: str) -> bytes:
    """Fetch raw feed bytes.

    Transport failures and HTTP failures are raised as distinct types because
    poll_log records them differently: an HTTP status is real information, and
    a connection refused has none to record.
    """
    try:
        response = await client.get(url)
    except httpx.HTTPError as exc:
        raise FeedTransportError(str(exc)) from exc

    if response.status_code != 200:
        raise FeedHTTPError(response.status_code, url)
    return response.content


def parse_message(payload: bytes) -> gtfs_rt.FeedMessage:
    message = gtfs_rt.FeedMessage()
    try:
        message.ParseFromString(payload)
    except (DecodeError, ValueError) as exc:
        raise FeedParseError(f"could not decode {len(payload)} bytes: {exc}") from exc
    return message


def _epoch_to_utc(seconds: int) -> datetime:
    return datetime.fromtimestamp(seconds, tz=UTC)


def header_timestamp(message: gtfs_rt.FeedMessage) -> datetime | None:
    """The feed's own statement of freshness, used for the skip check."""
    if message.header.HasField("timestamp") and message.header.timestamp > 0:
        return _epoch_to_utc(message.header.timestamp)
    return None


def _service_day(trip: gtfs_rt.TripDescriptor, fallback: datetime | None) -> date | None:
    """Resolve the service day for a trip.

    Trip identity in GTFS-Realtime is (start_date, trip_id), so this matters for
    correctness and not just for tidiness. When the feed omits start_date, the
    service day of the observation in the agency timezone is the best available
    substitute. That is wrong for a trip observed after midnight that began the
    previous service day, which is a known limitation until Phase 2 provides
    schedules to resolve it properly.
    """
    if trip.HasField("start_date") and trip.start_date:
        try:
            return datetime.strptime(trip.start_date, "%Y%m%d").date()
        except ValueError:
            log.warning("unparseable start_date", extra={"start_date": trip.start_date})

    if fallback is not None:
        return fallback.astimezone(SERVICE_TZ).date()
    return None


def _optional_float(message: object, field: str) -> float | None:
    return getattr(message, field) if message.HasField(field) else None  # type: ignore[attr-defined]


def extract_positions(message: gtfs_rt.FeedMessage) -> ParsedFeed:
    feed_ts = header_timestamp(message)
    rows: list[VehiclePositionRow] = []
    dropped = 0
    reasons: set[str] = set()

    for entity in message.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle

        # The primary key needs both of these, so neither can be guessed.
        vehicle_id = vehicle.vehicle.id or entity.id
        if not vehicle_id:
            dropped += 1
            reasons.add("no_vehicle_id")
            continue

        if vehicle.HasField("timestamp") and vehicle.timestamp > 0:
            ts = _epoch_to_utc(vehicle.timestamp)
        elif feed_ts is not None:
            # Falling back to the header is safe for dedupe, but it collapses
            # every vehicle in the poll onto one instant, which would distort
            # Phase 3 interpolation. Worth knowing if it ever happens.
            ts = feed_ts
            reasons.add("ts_from_header")
        else:
            dropped += 1
            reasons.add("no_timestamp")
            continue

        position = vehicle.position
        rows.append(
            VehiclePositionRow(
                vehicle_id=vehicle_id,
                ts=ts,
                trip_id=vehicle.trip.trip_id or None,
                route_id=vehicle.trip.route_id or None,
                start_date=_service_day(vehicle.trip, ts),
                lat=position.latitude if vehicle.HasField("position") else None,
                lon=position.longitude if vehicle.HasField("position") else None,
                bearing=_optional_float(position, "bearing")
                if vehicle.HasField("position")
                else None,
                speed=_optional_float(position, "speed") if vehicle.HasField("position") else None,
                current_stop_sequence=(
                    vehicle.current_stop_sequence
                    if vehicle.HasField("current_stop_sequence")
                    else None
                ),
                current_status=(
                    vehicle.current_status if vehicle.HasField("current_status") else None
                ),
                occupancy_status=(
                    vehicle.occupancy_status if vehicle.HasField("occupancy_status") else None
                ),
            )
        )

    return ParsedFeed(
        feed_timestamp=feed_ts,
        entity_count=len(message.entity),
        positions=tuple(rows),
        dropped=dropped,
        drop_reasons=tuple(sorted(reasons)),
    )


def extract_predictions(message: gtfs_rt.FeedMessage) -> ParsedFeed:
    feed_ts = header_timestamp(message)
    rows: list[PredictionRow] = []
    dropped = 0
    reasons: set[str] = set()

    for entity in message.entity:
        if not entity.HasField("trip_update"):
            continue
        update = entity.trip_update

        trip_id = update.trip.trip_id
        if not trip_id:
            dropped += len(update.stop_time_update) or 1
            reasons.add("no_trip_id")
            continue

        # observed_at is when this prediction was current. The trip update's own
        # timestamp is preferred over the header because a feed may carry
        # updates produced at slightly different times.
        if update.HasField("timestamp") and update.timestamp > 0:
            observed_at = _epoch_to_utc(update.timestamp)
        elif feed_ts is not None:
            observed_at = feed_ts
        else:
            dropped += len(update.stop_time_update) or 1
            reasons.add("no_timestamp")
            continue

        start_date = _service_day(update.trip, observed_at)
        if start_date is None:
            dropped += len(update.stop_time_update) or 1
            reasons.add("no_start_date")
            continue

        for stop_time in update.stop_time_update:
            # stop_sequence is part of the primary key. A feed is allowed to
            # identify a stop by stop_id alone, and resolving that to a sequence
            # needs the static schedule, which is Phase 2. Until then such rows
            # are dropped and counted rather than written under a guessed key.
            if not stop_time.HasField("stop_sequence"):
                dropped += 1
                reasons.add("no_stop_sequence")
                continue

            arrival = stop_time.arrival
            departure = stop_time.departure

            arrival_time = (
                _epoch_to_utc(arrival.time)
                if stop_time.HasField("arrival") and arrival.HasField("time") and arrival.time > 0
                else None
            )
            departure_time = (
                _epoch_to_utc(departure.time)
                if stop_time.HasField("departure")
                and departure.HasField("time")
                and departure.time > 0
                else None
            )

            # A single_delay style feed carries the delay and no time at all,
            # so the delay has to be read independently of the arrival time.
            delay_seconds: int | None = None
            if stop_time.HasField("arrival") and arrival.HasField("delay"):
                delay_seconds = arrival.delay
            elif stop_time.HasField("departure") and departure.HasField("delay"):
                delay_seconds = departure.delay

            if arrival_time is None and departure_time is None and delay_seconds is None:
                dropped += 1
                reasons.add("no_prediction_content")
                continue

            rows.append(
                PredictionRow(
                    start_date=start_date,
                    trip_id=trip_id,
                    stop_sequence=stop_time.stop_sequence,
                    observed_at=observed_at,
                    stop_id=stop_time.stop_id or None,
                    route_id=update.trip.route_id or None,
                    arrival_time=arrival_time,
                    departure_time=departure_time,
                    delay_seconds=delay_seconds,
                    schedule_relationship=(
                        stop_time.schedule_relationship
                        if stop_time.HasField("schedule_relationship")
                        else None
                    ),
                    vehicle_id=update.vehicle.id or None,
                )
            )

    return ParsedFeed(
        feed_timestamp=feed_ts,
        entity_count=len(message.entity),
        predictions=tuple(rows),
        dropped=dropped,
        drop_reasons=tuple(sorted(reasons)),
    )
