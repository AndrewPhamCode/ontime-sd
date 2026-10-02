-- Phase 3: inferred arrivals, the ground truth the project has been missing.
--
-- Until now the database held MTS's predictions and raw GPS pings, with nothing
-- connecting them. These tables hold the derived answer to "when did this vehicle
-- actually reach this stop", which is the label Phase 5 trains on and the actual
-- that Phase 4 scores MTS's predictions against.
--
-- Accuracy here caps accuracy everywhere downstream. An arrival inferred 40
-- seconds wrong puts a 40 second floor under every error measurement that
-- follows, which is why the quality columns exist rather than just a timestamp.

create table arrivals (
    -- Trip identity in GTFS-Realtime is (start_date, trip_id). stop_sequence
    -- completes it, because a trip may call at the same stop twice.
    start_date    date        not null,
    trip_id       text        not null,
    stop_sequence integer     not null,

    -- Which loaded schedule the stop positions came from. Shapes and stop
    -- distances change between feed versions, so an arrival is only
    -- reproducible alongside the version used to derive it.
    feed_version  text        not null references feed_versions on delete cascade,

    stop_id       text        not null,
    vehicle_id    text        not null,

    -- The inferred moment the vehicle reached the stop.
    arrived_at    timestamptz not null,
    -- When it left, where a dwell was detected. Null when the vehicle was only
    -- seen passing through.
    departed_at   timestamptz,

    -- How this arrival was derived:
    --   interpolated  between two pings bracketing the stop
    --   dwell         vehicle seen stationary at the stop, arrival is the first
    --                 ping of the cluster rather than the midpoint
    --   at_ping       a ping landed essentially on the stop
    method        text        not null,

    -- Quality signals. Ping gaps reach 843 seconds at p99 on real data, so an
    -- arrival interpolated across a wide gap is close to a guess. Phase 4 must be
    -- able to report the metric over all arrivals and over well observed ones
    -- separately, otherwise a poor MAE cannot be told apart from poor GPS
    -- coverage.
    ping_gap_seconds integer,
    nearest_ping_m   double precision,

    -- The stop's distance along the route shape, carried so a result can be
    -- checked without re-reading the schedule.
    stop_offset_m    double precision,

    inferred_at   timestamptz not null default now(),

    primary key (start_date, trip_id, stop_sequence),

    constraint arrivals_method_check
        check (method in ('interpolated', 'dwell', 'at_ping')),
    constraint arrivals_gap_nonneg
        check (ping_gap_seconds is null or ping_gap_seconds >= 0),
    constraint arrivals_nearest_nonneg
        check (nearest_ping_m is null or nearest_ping_m >= 0),
    constraint arrivals_departure_after_arrival
        check (departed_at is null or departed_at >= arrived_at)
);

-- Phase 4 walks a stop's arrivals to join them against predictions.
create index arrivals_stop_idx on arrivals (stop_id, arrived_at);

-- Phase 5 aggregates travel time per stop-to-stop segment.
create index arrivals_trip_idx on arrivals (start_date, trip_id, stop_sequence);

-- Filtering to well observed arrivals, which is expected to be common.
create index arrivals_quality_idx on arrivals (ping_gap_seconds)
    where ping_gap_seconds is not null;

-- One row per trip processed, including the ones that produced nothing. Same
-- reasoning as poll_log in ADR-0018: without recording the skips, a trip that
-- yielded no arrivals is indistinguishable from a trip never processed, and the
-- coverage of the label set would be unknowable.
create table inference_log (
    id              bigserial   primary key,
    run_at          timestamptz not null default now(),
    start_date      date        not null,
    trip_id         text        not null,
    vehicle_id      text,
    pings           integer,
    stops_total     integer,
    arrivals_written integer,
    stops_skipped   integer,
    duration_ms     integer,
    status          text        not null,
    reason          text,

    constraint inference_log_status_check check (status in (
        'ok',
        'no_pings',             -- the trip was never observed
        'no_shape',             -- trips.shape_id missing, cannot snap
        'no_stops',             -- the schedule has no stop_times for it
        'no_arrivals',          -- observed, but no stop was bracketed by pings
        'error'
    ))
);

create index inference_log_trip_idx on inference_log (start_date, trip_id);
create index inference_log_run_idx on inference_log (run_at desc);
