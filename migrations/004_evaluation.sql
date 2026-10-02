-- Phase 4: how wrong MTS's predictions were, per horizon.
--
-- One row per (arrival, horizon). For an arrival that actually happened at A and a
-- horizon h, the prediction in force at A - h is the most recent predictions row
-- whose observed_at is at or before that cutoff. Because predictions are stored
-- change-only (ADR-0005), that row may have been written much earlier: no row in
-- between means MTS did not change its mind, which is information rather than a
-- gap.
--
-- The horizon is measured back from the ACTUAL arrival, not the predicted one.
-- "Twenty minutes before the bus really came, what was MTS saying" is what a rider
-- experiences. Measuring back from the predicted arrival would be self-referential:
-- the worse the prediction, the further off the evaluation point. See ADR-0037.

create table prediction_errors (
    start_date       date    not null,
    trip_id          text    not null,
    stop_sequence    integer not null,
    horizon_minutes  integer not null,

    feed_version     text    not null references feed_versions on delete cascade,
    stop_id          text    not null,
    route_id         text,

    arrived_at        timestamptz not null,
    predicted_arrival timestamptz not null,
    -- When MTS made the prediction being scored. Always at or before
    -- arrived_at - horizon.
    predicted_at      timestamptz not null,

    -- Signed: positive means MTS predicted later than the bus actually came, so
    -- the rider was told to wait longer than they did. Kept alongside the absolute
    -- value because a systematic bias is the easiest thing for a model to beat,
    -- and an absolute-only metric would hide it.
    error_seconds     integer not null,
    abs_error_seconds integer not null,

    -- Carried from the arrival so label quality can be filtered without a join.
    -- 24.6% of arrivals were interpolated across ping gaps over 10 minutes and
    -- carry real uncertainty of their own, which would otherwise be measured as
    -- MTS's error rather than ours.
    ping_gap_seconds  integer,

    -- Minutes past service-day midnight, so it may exceed 1440 for an arrival
    -- after midnight on a trip that started the previous evening.
    service_minute    integer not null,
    is_weekend        boolean not null,

    -- True when this arrival has a prediction at every horizon. Prediction
    -- availability falls from 94.7% at 1 minute to 58.4% at 20, so comparing
    -- horizons over whatever each one has would compare different populations.
    -- Precomputed so the comparable subset is a filter, not a self join.
    has_all_horizons  boolean not null,

    computed_at       timestamptz not null default now(),

    primary key (start_date, trip_id, stop_sequence, horizon_minutes),

    constraint prediction_errors_horizon_check
        check (horizon_minutes in (1, 5, 10, 20)),
    constraint prediction_errors_abs_matches
        check (abs_error_seconds = abs(error_seconds)),
    constraint prediction_errors_prediction_precedes_cutoff
        check (predicted_at <= arrived_at - (horizon_minutes * interval '1 minute'))
);

-- The headline aggregation: error by horizon, filtered to the comparable subset
-- and to well observed arrivals.
create index prediction_errors_headline_idx
    on prediction_errors (horizon_minutes, has_all_horizons, ping_gap_seconds);

-- Per route and per time of day breakdowns from the charter.
create index prediction_errors_route_idx
    on prediction_errors (route_id, horizon_minutes);
create index prediction_errors_time_idx
    on prediction_errors (service_minute, horizon_minutes);
