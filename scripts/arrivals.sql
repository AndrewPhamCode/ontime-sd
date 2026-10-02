-- Inferred arrivals: how many, how they were derived, and how good they are.
-- See RUNBOOK.md and DESIGN.md ADR-0036.

\echo '== arrivals by service day =='
select start_date,
       count(*)                                        as arrivals,
       count(distinct trip_id)                          as trips,
       count(*) filter (where method = 'dwell')         as dwell,
       count(*) filter (where method = 'interpolated')  as interpolated,
       count(*) filter (where method = 'at_ping')       as at_ping
from arrivals
group by start_date
order by start_date desc;

\echo ''
\echo '== label quality: how wide was the ping gap behind each arrival? =='
-- Phase 4 should report the metric over all arrivals and over well observed
-- ones. An arrival interpolated across a 14 minute gap is close to a guess.
select case
         when ping_gap_seconds is null then 'unknown'
         when ping_gap_seconds <= 60  then 'tight (<= 60s)'
         when ping_gap_seconds <= 180 then 'ok (<= 3min)'
         when ping_gap_seconds <= 600 then 'loose (<= 10min)'
         else 'poor (> 10min)'
       end as label_quality,
       count(*) as arrivals,
       round(100.0 * count(*) / sum(count(*)) over (), 1) as pct
from arrivals
group by 1
order by 2 desc;

\echo ''
\echo '== sanity: arrival times must increase with stop_sequence =='
select count(*) as monotonicity_violations
from (select arrived_at,
             lag(arrived_at) over (partition by start_date, trip_id order by stop_sequence) as prev
      from arrivals) x
where prev is not null and arrived_at < prev;

\echo ''
\echo '== inference coverage per run =='
select status, count(*) as trips, sum(arrivals_written) as arrivals,
       sum(stops_skipped) as stops_skipped
from inference_log
group by status
order by trips desc;
