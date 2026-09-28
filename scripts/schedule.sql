-- Static GTFS state: which feed versions are loaded, and how the weekly
-- refresh has been going. See RUNBOOK.md.

\echo '== loaded feed versions =='
select left(feed_version, 12) as version,
       loaded_at::timestamp(0)                   as loaded_at,
       feed_start_date,
       feed_end_date,
       pg_size_pretty(content_length::numeric)   as download_size,
       (row_counts->>'stop_times')::int          as stop_times,
       (row_counts->>'trips')::int               as trips,
       (row_counts->>'service_dates')::int       as service_dates
from feed_versions
where loaded_at is not null
order by loaded_at desc;

\echo ''
\echo '== recent load attempts =='
-- skipped_unchanged is the healthy steady state: MTS republishes roughly
-- monthly, so most weekly runs have nothing to do.
select started_at::timestamp(0) as started_at,
       status,
       http_code,
       left(coalesce(feed_version, ''), 12) as version,
       round(duration_ms / 1000.0, 1)       as seconds,
       left(coalesce(error, ''), 60)        as error
from gtfs_load_log
order by started_at desc
limit 15;

\echo ''
\echo '== is the schedule current? =='
-- A feed whose end date has passed means trips may be missing entirely.
select case
         when max(feed_end_date) is null then 'NO SCHEDULE LOADED'
         when max(feed_end_date) < current_date then 'EXPIRED: reload needed'
         when max(feed_end_date) < current_date + 14 then 'expires within 2 weeks'
         else 'current'
       end                                as schedule_status,
       max(feed_end_date)                 as valid_until,
       max(loaded_at)::timestamp(0)       as last_loaded
from feed_versions
where loaded_at is not null;
