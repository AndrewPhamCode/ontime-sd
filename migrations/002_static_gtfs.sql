-- Static GTFS schedule, Phase 2.
--
-- Every table is keyed on feed_version first. MTS republishes this feed
-- periodically and a trip's stops and times can change between publications, so
-- keeping versions lets Phase 4 evaluate a prediction against the schedule that
-- was actually in effect when the prediction was made. See DESIGN.md ADR-0024.
--
-- Times are integer seconds past service-day midnight, not time values, because
-- GTFS times legitimately exceed 24:00:00 for trips running past midnight. In
-- the feed current at the time of writing, 11,432 stop_times rows are at hour 24
-- or later and the maximum hour is 27. See ADR-0025.
--
-- Distances are metres. The source feed expresses shape_dist_traveled in miles,
-- verified against haversine geometry, and it is converted on load so that
-- Phase 3 never mixes units with GPS distances. See ADR-0026.

-- The registry. loaded_at stays null until the whole load commits, which is what
-- makes a half finished load detectable rather than silently partial.
create table feed_versions (
    feed_version     text        primary key,  -- sha256 of the downloaded zip
    source_url       text        not null,
    downloaded_at    timestamptz not null default now(),
    loaded_at        timestamptz,
    content_length   bigint,
    -- The HTTP Last-Modified header, kept so the next run can ask whether
    -- anything changed before downloading 8.4 MB.
    last_modified    text,
    -- MTS publishes its own version string in feed_info.txt. Stored alongside
    -- our hash because it is what MTS support would ask about, but not used as
    -- the key: it is free text and not guaranteed unique.
    mts_feed_version text,
    feed_start_date  date,
    feed_end_date    date,
    row_counts       jsonb
);

-- Foreign keys to feed_versions with ON DELETE CASCADE throughout. The parent
-- table holds one row per version, so the per row check is a lookup into a tiny
-- index, and in exchange a version can be pruned with a single delete and no
-- table can be left holding orphans.

create table agencies (
    feed_version    text not null references feed_versions on delete cascade,
    agency_id       text not null,
    agency_name     text,
    agency_url      text,
    agency_timezone text,
    agency_lang     text,
    agency_phone    text,
    primary key (feed_version, agency_id)
);

create table routes (
    feed_version     text not null references feed_versions on delete cascade,
    route_id         text not null,
    agency_id        text,
    route_short_name text,
    route_long_name  text,
    -- GTFS route_type. This feed carries 3 (bus), 0 (trolley), and 4 (ferry).
    route_type       smallint,
    route_color      text,
    route_text_color text,
    primary key (feed_version, route_id)
);

create table stops (
    feed_version        text not null references feed_versions on delete cascade,
    stop_id             text not null,
    stop_code           text,
    stop_name           text,
    stop_lat            double precision,
    stop_lon            double precision,
    -- 0 is a stop, 1 is a station. This feed has 4,269 stops and 103 stations.
    location_type       smallint,
    parent_station      text,
    wheelchair_boarding smallint,
    primary key (feed_version, stop_id)
);

create table trips (
    feed_version  text not null references feed_versions on delete cascade,
    trip_id       text not null,
    route_id      text not null,
    service_id    text not null,
    shape_id      text,
    trip_headsign text,
    direction_id  smallint,
    block_id      text,
    primary key (feed_version, trip_id)
);

-- Which trips run on a given service pattern, for joining to service_dates.
create index trips_service_idx on trips (feed_version, service_id);
create index trips_route_idx on trips (feed_version, route_id);

create table stop_times (
    feed_version          text    not null references feed_versions on delete cascade,
    trip_id               text    not null,
    stop_sequence         integer not null,
    stop_id               text    not null,
    -- Seconds past service-day midnight. May exceed 86400. Nullable because
    -- GTFS permits a stop_time with no times at a non timepoint stop.
    arrival_seconds       integer,
    departure_seconds     integer,
    shape_dist_traveled_m double precision,
    pickup_type           smallint,
    drop_off_type         smallint,
    timepoint             smallint,
    stop_headsign         text,
    -- Also serves the dominant Phase 3 query: every stop on one trip in order.
    primary key (feed_version, trip_id, stop_sequence),
    constraint stop_times_arrival_nonneg  check (arrival_seconds is null or arrival_seconds >= 0),
    constraint stop_times_departure_nonneg check (departure_seconds is null or departure_seconds >= 0),
    constraint stop_times_dist_nonneg
        check (shape_dist_traveled_m is null or shape_dist_traveled_m >= 0)
);

-- Phase 4 aggregates prediction error per stop.
create index stop_times_stop_idx on stop_times (feed_version, stop_id);

create table shapes (
    feed_version          text    not null references feed_versions on delete cascade,
    shape_id              text    not null,
    shape_pt_sequence     integer not null,
    shape_pt_lat          double precision not null,
    shape_pt_lon          double precision not null,
    shape_dist_traveled_m double precision,
    -- Phase 3 walks these in sequence order to snap GPS to the route.
    primary key (feed_version, shape_id, shape_pt_sequence)
);

create table calendar (
    feed_version text not null references feed_versions on delete cascade,
    service_id   text not null,
    monday       boolean not null,
    tuesday      boolean not null,
    wednesday    boolean not null,
    thursday     boolean not null,
    friday       boolean not null,
    saturday     boolean not null,
    sunday       boolean not null,
    start_date   date not null,
    end_date     date not null,
    primary key (feed_version, service_id)
);

create table calendar_dates (
    feed_version   text not null references feed_versions on delete cascade,
    service_id     text not null,
    service_date   date not null,
    -- 1 adds service on this date, 2 removes it.
    exception_type smallint not null,
    primary key (feed_version, service_id, service_date),
    constraint calendar_dates_exception_type_check check (exception_type in (1, 2))
);

-- Materialized answer to "which services run on this date", built at load time
-- by expanding the calendar weekday pattern across its date range and then
-- applying calendar_dates exceptions. Computing this per query would put the
-- weekday and exception logic in every caller, which is exactly the kind of
-- thing that is wrong in one place and right in another. See ADR-0027.
create table service_dates (
    feed_version text not null references feed_versions on delete cascade,
    service_date date not null,
    service_id   text not null,
    primary key (feed_version, service_date, service_id)
);

create index service_dates_service_idx on service_dates (feed_version, service_id);

-- One row per load attempt, including the skips. Same reasoning as poll_log in
-- ADR-0018: without the successes there is no denominator, and a loader that
-- quietly stopped running looks identical to a feed that never changed.
create table gtfs_load_log (
    id            bigserial   primary key,
    started_at    timestamptz not null,
    duration_ms   integer,
    status        text        not null,
    http_code     integer,
    feed_version  text,
    bytes         bigint,
    rows_loaded   jsonb,
    error         text,
    constraint gtfs_load_log_status_check check (status in (
        'ok',
        'skipped_unchanged',        -- HEAD showed nothing new, no download
        'skipped_already_loaded',   -- downloaded, but this sha256 is already in
        'http_error',
        'parse_error',
        'db_error'
    ))
);

create index gtfs_load_log_started_idx on gtfs_load_log (started_at desc);
