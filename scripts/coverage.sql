-- Collection health. This is the first thing to run when the data looks wrong.
-- See RUNBOOK.md.

\echo '== polls by feed and status, last 24h =='
select feed,
       status,
       count(*)                                as polls,
       round(avg(duration_ms))                 as avg_ms,
       max(duration_ms)                        as worst_ms,
       sum(coalesce(rows_written, 0))          as rows_written
from poll_log
where started_at > now() - interval '24 hours'
group by feed, status
order by feed, polls desc;

\echo ''
\echo '== gaps over 2 minutes between SUCCESSFUL polls, last 24h =='
-- Measured between successes, not between attempts. Backoff keeps retrying every
-- few seconds during an outage, so a gap between attempts stays small while
-- collection is actually stopped. An earlier version of this query measured
-- attempts and reported zero gaps through a real 90 second outage.
--
-- A gap means no data was collected in that window: the Mac slept, lost network,
-- the feed was down, or the process was not running. Gaps are expected on a
-- laptop and are recorded rather than hidden, per ADR-0015.
select feed,
       previous_success,
       started_at as recovered_at,
       round(gap_seconds) as gap_seconds
from (
    select feed,
           lag(started_at) over (partition by feed order by started_at) as previous_success,
           started_at,
           extract(epoch from started_at
                   - lag(started_at) over (partition by feed order by started_at)) as gap_seconds
    from poll_log
    -- A skip is a success: the feed had nothing new, and we were still polling.
    where status in ('ok', 'skipped_unchanged')
      and started_at > now() - interval '24 hours'
) gaps
where gap_seconds > 120
order by gap_seconds desc
limit 20;

\echo ''
\echo '== change-only compression: predictions written vs polls =='
-- If predictions_written is close to polls times active stop times, the
-- change-only cache is not working. See ADR-0005.
select (select count(*) from poll_log
        where feed = 'trip_updates' and status = 'ok'
          and started_at > now() - interval '24 hours')        as successful_polls,
       (select count(*) from predictions
        where ingested_at > now() - interval '24 hours')       as predictions_written,
       (select count(*) from vehicle_positions
        where ingested_at > now() - interval '24 hours')       as positions_written;

\echo ''
\echo '== most recent poll per feed =='
select feed, max(started_at) as last_attempt,
       max(started_at) filter (where status = 'ok') as last_success
from poll_log
group by feed
order by feed;
