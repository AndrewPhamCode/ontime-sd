-- One off backfill of vehicle_positions.start_date for trips crossing midnight.
--
-- The real MTS feed omits start_date, so it was originally inferred from the
-- observation date. That is wrong for a trip scheduled across midnight: its pings
-- after 00:00 were filed under the following service day, lumping the tail of one
-- night's run together with the start of the next. See ADR-0035.
--
-- Safe to re-run: rows already correct are not matched. start_date is not part of
-- the vehicle_positions primary key, so this cannot collide.
--
-- predictions.start_date is deliberately NOT backfilled: it IS part of that
-- table's primary key, so moving a row can conflict with an existing one.

\echo '== before =='
select count(*) as trip_records,
       count(*) filter (where observed_min > scheduled_min + 60) as implausible
from (
  select v.start_date, v.trip_id,
         extract(epoch from max(v.ts) - min(v.ts))/60 as observed_min,
         (max(s.arrival_seconds) - min(s.arrival_seconds))/60.0 as scheduled_min
  from vehicle_positions v
  join stop_times s on s.trip_id = v.trip_id
  where v.trip_id is not null
  group by v.start_date, v.trip_id) x;

with w as (
    select trip_id,
           min(arrival_seconds) as s0,
           max(arrival_seconds) as s1
    from stop_times
    where arrival_seconds is not null
    group by feed_version, trip_id
),
candidates as (
    select v.vehicle_id,
           v.ts,
           extract(epoch from (v.ts at time zone 'America/Los_Angeles')
                   - date_trunc('day', v.ts at time zone 'America/Los_Angeles')) as secs_today,
           w.s0,
           w.s1
    from vehicle_positions v
    join w on w.trip_id = v.trip_id
    where v.trip_id is not null
)
update vehicle_positions v
set start_date = (v.ts at time zone 'America/Los_Angeles')::date - 1
from candidates c
where c.vehicle_id = v.vehicle_id
  and c.ts = v.ts
  -- Does not belong to today's service day...
  and c.secs_today not between c.s0 - 900 and c.s1 + 3600
  -- ...but does belong to yesterday's.
  and c.secs_today + 86400 between c.s0 - 900 and c.s1 + 3600;

\echo '== after =='
select count(*) as trip_records,
       count(*) filter (where observed_min > scheduled_min + 60) as implausible
from (
  select v.start_date, v.trip_id,
         extract(epoch from max(v.ts) - min(v.ts))/60 as observed_min,
         (max(s.arrival_seconds) - min(s.arrival_seconds))/60.0 as scheduled_min
  from vehicle_positions v
  join stop_times s on s.trip_id = v.trip_id
  where v.trip_id is not null
  group by v.start_date, v.trip_id) x;
