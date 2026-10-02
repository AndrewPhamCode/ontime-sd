-- Phase 5: our own predictors, scored against MTS on identical rows.
--
-- prediction_errors gains a `source` column so every predictor is evaluated by the
-- same definition over the same arrivals. The head-to-head is then one GROUP BY
-- rather than a join across tables that might not agree on their population,
-- which is what makes the comparison airtight. See DESIGN.md ADR-0038.

alter table prediction_errors add column source text not null default 'mts';

alter table prediction_errors drop constraint prediction_errors_pkey;
alter table prediction_errors
    add primary key (start_date, trip_id, stop_sequence, horizon_minutes, source);

alter table prediction_errors add constraint prediction_errors_source_check
    check (source in ('mts', 'persist_delay', 'segment_mean', 'lgbm'));

drop index if exists prediction_errors_headline_idx;
create index prediction_errors_headline_idx
    on prediction_errors (source, horizon_minutes, has_all_horizons, ping_gap_seconds);

-- Historical travel time between consecutive stops.
--
-- fit_through_date is part of the key, not metadata. A "historical mean" computed
-- over data that includes the test period has already seen the future, so P2 and
-- any feature derived from it would quietly cheat. Keying on the last day of data
-- used makes that leak visible in the data rather than implicit in the code, and
-- lets several fits coexist.
create table segment_stats (
    feed_version     text    not null references feed_versions on delete cascade,
    from_stop_id     text    not null,
    to_stop_id       text    not null,
    -- Service hour, so it may exceed 23 for a trip running past midnight.
    hour_bin         smallint not null,
    is_weekend       boolean not null,
    fit_through_date date    not null,

    n                integer not null,
    mean_seconds     double precision not null,
    median_seconds   double precision,
    stddev_seconds   double precision,

    primary key (feed_version, from_stop_id, to_stop_id, hour_bin, is_weekend,
                 fit_through_date),

    constraint segment_stats_hour_range check (hour_bin between 0 and 29),
    constraint segment_stats_n_positive check (n > 0),
    constraint segment_stats_mean_positive check (mean_seconds > 0)
);

create index segment_stats_fit_idx on segment_stats (fit_through_date, feed_version);

-- Provenance for every model run, so a number quoted in the README can be traced
-- back to the training window, features and parameters that produced it.
create table model_runs (
    id             bigserial   primary key,
    trained_at     timestamptz not null default now(),
    source         text        not null,
    train_from     date        not null,
    train_to       date        not null,
    test_from      date        not null,
    test_to        date        not null,
    train_rows     integer,
    test_rows      integer,
    features       jsonb,
    params         jsonb,
    mae_by_horizon jsonb,
    notes          text,

    constraint model_runs_window_order check (train_to < test_from)
);

create index model_runs_trained_idx on model_runs (trained_at desc);
