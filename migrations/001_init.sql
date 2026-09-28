-- Phase 1 realtime collection schema.
--
-- All timestamps here are timestamptz because GTFS-Realtime reports absolute
-- POSIX instants. Static GTFS times stored as seconds past service day
-- midnight are a Phase 2 concern and deliberately do not appear here.
-- See DESIGN.md ADR-0013.

-- Raw vehicle position pings. This is the ground truth input for Phase 3
-- arrival inference, so it is stored losslessly.
--
-- The primary key is the natural key. Feeds republish the same record across
-- consecutive polls, so making (vehicle_id, ts) the primary key turns
-- deduplication into a database guarantee and makes ingest idempotent.
-- See ADR-0004.
create table vehicle_positions (
    vehicle_id            text        not null,
    ts                    timestamptz not null,
    trip_id               text,
    route_id              text,
    start_date            date,
    lat                   double precision,
    lon                   double precision,
    bearing               real,
    speed                 real,
    current_stop_sequence integer,
    current_status        smallint,
    occupancy_status      smallint,
    ingested_at           timestamptz not null default now(),
    primary key (vehicle_id, ts)
);

-- Phase 3 reads every ping for one trip in time order to interpolate stop
-- crossings. Trip identity in GTFS-Realtime is (start_date, trip_id), not
-- trip_id alone, because the same trip_id recurs every service day.
create index vehicle_positions_trip_idx
    on vehicle_positions (start_date, trip_id, ts);

-- Supports "what was moving recently", used by the health dashboard and for
-- spot checking collection coverage.
create index vehicle_positions_ts_idx
    on vehicle_positions (ts desc);

-- Official MTS predictions, stored change-only: a row exists only where the
-- prediction moved by at least the configured threshold. See ADR-0005.
--
-- observed_at is part of the primary key because the prediction time series is
-- the point of this table. Phase 4 asks "what did MTS predict N minutes before
-- the actual arrival", which is a lookup on
-- (start_date, trip_id, stop_sequence) with observed_at <= arrival - N,
-- ordered by observed_at descending, limit 1. The primary key index serves
-- that directly via a backward scan, so no separate descending index is
-- needed.
create table predictions (
    start_date            date        not null,
    trip_id               text        not null,
    stop_sequence         integer     not null,
    observed_at           timestamptz not null,
    stop_id               text,
    route_id              text,
    arrival_time          timestamptz,
    departure_time        timestamptz,
    delay_seconds         integer,
    schedule_relationship smallint,
    vehicle_id            text,
    ingested_at           timestamptz not null default now(),
    primary key (start_date, trip_id, stop_sequence, observed_at)
);

-- Phase 4 also aggregates error by stop and by time of day. That index is
-- deliberately not created yet: the query shape is not final until the real
-- feed confirms whether trip updates carry per stop times or a single delay,
-- and an unused index costs write throughput on the hot ingest path.

-- One row per poll attempt per feed, including successes and skips. Without
-- the successes there is no denominator, so collection coverage and feed
-- failure rate would both be uncomputable. See ADR-0018.
create table poll_log (
    id             bigserial   primary key,
    feed           text        not null,
    started_at     timestamptz not null,
    duration_ms    integer,
    status         text        not null,
    http_code      integer,
    feed_timestamp timestamptz,
    entity_count   integer,
    rows_written   integer,
    error          text,
    constraint poll_log_feed_check
        check (feed in ('vehicle_positions', 'trip_updates')),
    -- skipped_unchanged is a healthy outcome and must not be counted as a
    -- failure. http_error and parse_error are kept distinct because "MTS is
    -- down" and "MTS changed its output" need different responses.
    constraint poll_log_status_check
        check (status in ('ok', 'skipped_unchanged', 'http_error',
                          'parse_error', 'db_error'))
);

create index poll_log_feed_started_idx
    on poll_log (feed, started_at desc);

-- Answers "is the collector alive" without scanning history.
create index poll_log_ok_idx
    on poll_log (feed, started_at desc)
    where status = 'ok';
