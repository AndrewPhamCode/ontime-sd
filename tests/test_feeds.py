"""Feed parsing, including the malformed cases a real feed is allowed to send.

Almost every GTFS-Realtime field is optional, so these tests are mostly about
what happens when something this project needs is absent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from google.transit import gtfs_realtime_pb2 as gtfs_rt

from ontime_sd.feeds import (
    FeedParseError,
    extract_positions,
    extract_predictions,
    header_timestamp,
    parse_message,
)
from ontime_sd.mock.simulator import PER_STOP, SINGLE_DELAY, Simulator

NOW = datetime(2026, 9, 27, 19, 0, tzinfo=UTC)
NOW_EPOCH = int(NOW.timestamp())


def _message(header_ts: int | None = NOW_EPOCH) -> gtfs_rt.FeedMessage:
    message = gtfs_rt.FeedMessage()
    message.header.gtfs_realtime_version = "2.0"
    if header_ts is not None:
        message.header.timestamp = header_ts
    return message


# --- happy path against the mock ---


def test_positions_extracted_from_the_mock_feed() -> None:
    simulator = Simulator(vehicle_count=5)
    parsed = extract_positions(simulator.vehicle_positions(NOW))

    assert parsed.entity_count == 5
    assert len(parsed.positions) == 5
    assert parsed.dropped == 0

    row = parsed.positions[0]
    assert row.vehicle_id.startswith("MOCK_BUS_")
    assert row.ts == NOW
    assert row.trip_id
    assert row.start_date == NOW.astimezone().date() or isinstance(row.start_date, date)
    assert row.lat is not None and row.lon is not None
    assert row.current_stop_sequence >= 1


def test_predictions_extracted_from_per_stop_feed() -> None:
    simulator = Simulator(vehicle_count=3)
    parsed = extract_predictions(simulator.trip_updates(NOW, style=PER_STOP))

    assert parsed.dropped == 0
    assert len(parsed.predictions) == 3 * simulator.upcoming_stops

    row = parsed.predictions[0]
    assert row.observed_at == NOW
    assert row.arrival_time is not None
    assert row.delay_seconds is not None
    assert row.stop_sequence >= 1


def test_predictions_extracted_from_single_delay_feed() -> None:
    """The shape CLAUDE.md flags as unverified must produce a usable row."""
    simulator = Simulator(vehicle_count=3)
    parsed = extract_predictions(simulator.trip_updates(NOW, style=SINGLE_DELAY))

    assert parsed.dropped == 0
    assert len(parsed.predictions) == 3

    row = parsed.predictions[0]
    assert row.arrival_time is None, "single_delay carries no absolute time"
    assert row.delay_seconds is not None, "the delay is the whole payload"


# --- header timestamp ---


def test_header_timestamp_read_when_present() -> None:
    assert header_timestamp(_message()) == NOW


@pytest.mark.parametrize("value", [None, 0])
def test_missing_or_zero_header_timestamp_is_none(value: int | None) -> None:
    assert header_timestamp(_message(header_ts=value)) is None


# --- malformed input ---


def test_garbage_bytes_raise_a_parse_error() -> None:
    with pytest.raises(FeedParseError):
        parse_message(b"\xff\xfe\xfd this is not protobuf at all \x00\x01")


def test_truncated_feed_raises_a_parse_error() -> None:
    payload = Simulator(vehicle_count=4).vehicle_positions(NOW).SerializeToString()
    with pytest.raises(FeedParseError):
        parse_message(payload[: len(payload) // 2])


def test_empty_payload_parses_to_an_empty_feed() -> None:
    """Valid protobuf, no entities. Not an error, just nothing to write."""
    parsed = extract_positions(parse_message(b""))
    assert parsed.entity_count == 0
    assert parsed.positions == ()


def test_position_without_a_vehicle_id_is_dropped() -> None:
    message = _message()
    entity = message.entity.add()
    # entity.id left empty too, so there is no fallback.
    entity.id = ""
    entity.vehicle.position.latitude = 32.7
    entity.vehicle.timestamp = NOW_EPOCH

    parsed = extract_positions(message)
    assert parsed.positions == ()
    assert parsed.dropped == 1
    assert "no_vehicle_id" in parsed.drop_reasons


def test_entity_id_is_used_when_vehicle_id_is_absent() -> None:
    message = _message()
    entity = message.entity.add()
    entity.id = "fallback-id"
    entity.vehicle.timestamp = NOW_EPOCH

    parsed = extract_positions(message)
    assert [row.vehicle_id for row in parsed.positions] == ["fallback-id"]


def test_position_timestamp_falls_back_to_the_header_and_says_so() -> None:
    """Safe for dedupe, but it collapses a poll onto one instant."""
    message = _message()
    entity = message.entity.add()
    entity.id = "bus-1"
    entity.vehicle.vehicle.id = "bus-1"

    parsed = extract_positions(message)
    assert parsed.positions[0].ts == NOW
    assert "ts_from_header" in parsed.drop_reasons


def test_position_with_no_timestamp_anywhere_is_dropped() -> None:
    message = _message(header_ts=None)
    entity = message.entity.add()
    entity.id = "bus-1"
    entity.vehicle.vehicle.id = "bus-1"

    parsed = extract_positions(message)
    assert parsed.positions == ()
    assert "no_timestamp" in parsed.drop_reasons


def test_start_date_is_parsed_from_the_trip_descriptor() -> None:
    message = _message()
    entity = message.entity.add()
    entity.id = "bus-1"
    entity.vehicle.vehicle.id = "bus-1"
    entity.vehicle.timestamp = NOW_EPOCH
    entity.vehicle.trip.start_date = "20260926"

    parsed = extract_positions(message)
    assert parsed.positions[0].start_date == date(2026, 9, 26)


def test_unparseable_start_date_falls_back_to_the_service_day() -> None:
    message = _message()
    entity = message.entity.add()
    entity.id = "bus-1"
    entity.vehicle.vehicle.id = "bus-1"
    entity.vehicle.timestamp = NOW_EPOCH
    entity.vehicle.trip.start_date = "not-a-date"

    parsed = extract_positions(message)
    # 19:00 UTC is midday in Los Angeles, so the service day is the same date.
    assert parsed.positions[0].start_date == date(2026, 9, 27)


def test_prediction_without_stop_sequence_is_kept_for_resolution() -> None:
    """The real MTS feed never sends stop_sequence, only stop_id.

    Dropping these would discard every prediction MTS publishes, which is exactly
    what happened before this was measured against the real feed. The row is kept
    unresolved and the sequence is recovered from the static schedule before it is
    written. See ADR-0033.
    """
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.trip.trip_id = "trip-1"
    entity.trip_update.trip.start_date = "20260927"
    entity.trip_update.timestamp = NOW_EPOCH
    stop_time = entity.trip_update.stop_time_update.add()
    stop_time.stop_id = "STOP_ONLY"
    stop_time.arrival.time = NOW_EPOCH + 300

    parsed = extract_predictions(message)

    assert parsed.dropped == 0
    assert len(parsed.predictions) == 1
    row = parsed.predictions[0]
    assert row.stop_sequence is None
    assert row.resolved is False
    assert row.stop_id == "STOP_ONLY"


def test_explicit_stop_sequence_is_used_when_present() -> None:
    """A feed that does send it must be unaffected by the resolution path."""
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.trip.trip_id = "trip-1"
    entity.trip_update.trip.start_date = "20260927"
    entity.trip_update.timestamp = NOW_EPOCH
    stop_time = entity.trip_update.stop_time_update.add()
    stop_time.stop_sequence = 7
    stop_time.stop_id = "STOP_A"
    stop_time.arrival.time = NOW_EPOCH + 300

    row = extract_predictions(message).predictions[0]
    assert row.stop_sequence == 7
    assert row.resolved is True


def test_feed_order_records_position_within_the_trip() -> None:
    """Alignment depends on this ordering, so it must survive extraction."""
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.trip.trip_id = "trip-1"
    entity.trip_update.trip.start_date = "20260927"
    entity.trip_update.timestamp = NOW_EPOCH
    for i, stop_id in enumerate(["a", "b", "c"]):
        stop_time = entity.trip_update.stop_time_update.add()
        stop_time.stop_id = stop_id
        stop_time.arrival.time = NOW_EPOCH + 60 * (i + 1)

    rows = extract_predictions(message).predictions
    assert [(r.stop_id, r.feed_order) for r in rows] == [("a", 0), ("b", 1), ("c", 2)]


def test_prediction_with_no_stop_identity_at_all_is_dropped() -> None:
    """Neither a sequence nor a stop_id means the row cannot be keyed."""
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.trip.trip_id = "trip-1"
    entity.trip_update.trip.start_date = "20260927"
    entity.trip_update.timestamp = NOW_EPOCH
    stop_time = entity.trip_update.stop_time_update.add()
    stop_time.arrival.time = NOW_EPOCH + 300

    parsed = extract_predictions(message)
    assert parsed.predictions == ()
    assert "no_stop_identity" in parsed.drop_reasons


def test_prediction_with_no_content_is_dropped() -> None:
    """A stop_time_update with neither a time nor a delay says nothing."""
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.trip.trip_id = "trip-1"
    entity.trip_update.trip.start_date = "20260927"
    entity.trip_update.timestamp = NOW_EPOCH
    stop_time = entity.trip_update.stop_time_update.add()
    stop_time.stop_sequence = 4

    parsed = extract_predictions(message)
    assert parsed.predictions == ()
    assert "no_prediction_content" in parsed.drop_reasons


def test_prediction_without_a_trip_id_is_dropped() -> None:
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.timestamp = NOW_EPOCH
    stop_time = entity.trip_update.stop_time_update.add()
    stop_time.stop_sequence = 1
    stop_time.arrival.time = NOW_EPOCH + 60

    parsed = extract_predictions(message)
    assert parsed.predictions == ()
    assert "no_trip_id" in parsed.drop_reasons


def test_departure_delay_is_used_when_arrival_has_none() -> None:
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.trip.trip_id = "trip-1"
    entity.trip_update.trip.start_date = "20260927"
    entity.trip_update.timestamp = NOW_EPOCH
    stop_time = entity.trip_update.stop_time_update.add()
    stop_time.stop_sequence = 2
    stop_time.departure.delay = 45

    parsed = extract_predictions(message)
    assert parsed.predictions[0].delay_seconds == 45


def test_trip_update_timestamp_preferred_over_the_header() -> None:
    """A feed may carry updates produced at slightly different times."""
    message = _message()
    entity = message.entity.add()
    entity.id = "tu-1"
    entity.trip_update.trip.trip_id = "trip-1"
    entity.trip_update.trip.start_date = "20260927"
    entity.trip_update.timestamp = NOW_EPOCH - 12
    stop_time = entity.trip_update.stop_time_update.add()
    stop_time.stop_sequence = 1
    stop_time.arrival.time = NOW_EPOCH + 60

    parsed = extract_predictions(message)
    assert parsed.predictions[0].observed_at == datetime.fromtimestamp(NOW_EPOCH - 12, tz=UTC)
    assert parsed.feed_timestamp == NOW


def test_mixed_feed_ignores_entities_of_the_other_kind() -> None:
    """A trip update in the positions feed must not become a position row."""
    message = _message()
    tu = message.entity.add()
    tu.id = "tu-1"
    tu.trip_update.trip.trip_id = "trip-1"
    vp = message.entity.add()
    vp.id = "bus-1"
    vp.vehicle.vehicle.id = "bus-1"
    vp.vehicle.timestamp = NOW_EPOCH

    positions = extract_positions(message)
    assert len(positions.positions) == 1
    assert positions.entity_count == 2
