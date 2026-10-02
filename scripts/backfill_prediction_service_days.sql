-- Backfill predictions.start_date for trips that run past midnight.
--
-- Companion to backfill_service_days.sql, which did the same for
-- vehicle_positions. This one was initially deferred because start_date is part
-- of the predictions primary key, so a moved row could in principle collide with
-- an existing one. Measured: 44,870 rows across 88 trips need moving and ZERO
-- would collide, because observed_at is also in the key and the two runs are 24
-- hours apart. The caution was unfounded.
--
-- Leaving it unfixed misaligned predictions and arrivals by one service day for
-- these trips, so the Phase 4 join matched the previous night's run and produced
-- errors of exactly 24 hours. See ADR-0037.

\echo '== rows to move =='
with w as (
    select trip_id, min(arrival_seconds) s0, max(arrival_seconds) s1
    from stop_times where arrival_seconds is not null group by feed_version, trip_id
)
select count(*) as rows_to_move, count(distinct p.trip_id) as trips
from predictions p join w on w.trip_id = p.trip_id
where extract(epoch from (p.observed_at at time zone 'America/Los_Angeles')
              - date_trunc('day', p.observed_at at time zone 'America/Los_Angeles'))
      not between w.s0 - 900 and w.s1 + 3600
  and extract(epoch from (p.observed_at at time zone 'America/Los_Angeles')
              - date_trunc('day', p.observed_at at time zone 'America/Los_Angeles')) + 86400
      between w.s0 - 900 and w.s1 + 3600;

with w as (
    select trip_id, min(arrival_seconds) s0, max(arrival_seconds) s1
    from stop_times where arrival_seconds is not null group by feed_version, trip_id
),
target as (
    select p.start_date, p.trip_id, p.stop_sequence, p.observed_at
    from predictions p join w on w.trip_id = p.trip_id
    where extract(epoch from (p.observed_at at time zone 'America/Los_Angeles')
                  - date_trunc('day', p.observed_at at time zone 'America/Los_Angeles'))
          not between w.s0 - 900 and w.s1 + 3600
      and extract(epoch from (p.observed_at at time zone 'America/Los_Angeles')
                  - date_trunc('day', p.observed_at at time zone 'America/Los_Angeles')) + 86400
          between w.s0 - 900 and w.s1 + 3600
)
update predictions p
set start_date = (p.observed_at at time zone 'America/Los_Angeles')::date - 1
from target t
where t.start_date = p.start_date and t.trip_id = p.trip_id
  and t.stop_sequence = p.stop_sequence and t.observed_at = p.observed_at;

\echo '== verify: predictions and arrivals now agree on the service day =='
select count(*) as arrivals_whose_predictions_are_a_day_off
from arrivals a
where exists (
    select 1 from predictions p
    where p.start_date = a.start_date and p.trip_id = a.trip_id
      and p.stop_sequence = a.stop_sequence
      and abs(extract(epoch from p.observed_at - a.arrived_at)) > 72000
);
