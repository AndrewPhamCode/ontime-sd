-- Phase 4: the baseline. How wrong are MTS's arrival predictions?
--
-- N is shown everywhere on purpose. These figures come from 5 service days at
-- 35 to 70% collection coverage, so they are a real measurement of a preliminary
-- sample, not a stable published statistic. See DESIGN.md ADR-0037.

\echo '== HEADLINE: MTS error by horizon =='
\echo '   comparable subset (prediction available at all four horizons)'
\echo '   well observed arrivals only (ping gap <= 3 min), so this measures MTS'
select horizon_minutes || ' min'                                        as horizon,
       count(*)                                                         as n,
       round(avg(abs_error_seconds)/60.0, 2)                            as mae_min,
       round((percentile_cont(0.5) within group
              (order by abs_error_seconds))::numeric/60, 2)             as median_min,
       round((percentile_cont(0.9) within group
              (order by abs_error_seconds))::numeric/60, 2)             as p90_min,
       round(avg(error_seconds)/60.0, 2)                                as bias_min
from prediction_errors
where has_all_horizons and ping_gap_seconds <= 180
group by horizon_minutes
order by horizon_minutes;

\echo ''
\echo '== all available pairs per horizon (more data, rows not comparable) =='
select horizon_minutes || ' min' as horizon,
       count(*) as n,
       round(avg(abs_error_seconds)/60.0, 2) as mae_min,
       round((percentile_cont(0.9) within group
              (order by abs_error_seconds))::numeric/60, 2) as p90_min
from prediction_errors
group by horizon_minutes
order by horizon_minutes;

\echo ''
\echo '== how much of the error is OUR label noise, not MTS? =='
-- If these columns differ a lot, GPS coverage is the bottleneck rather than MTS,
-- which matters before Phase 5 trains on these labels.
select horizon_minutes || ' min' as horizon,
       round(avg(abs_error_seconds) filter (where ping_gap_seconds <= 180)/60.0, 2)
         as mae_tight_labels,
       round(avg(abs_error_seconds) filter (where ping_gap_seconds > 600)/60.0, 2)
         as mae_poor_labels,
       count(*) filter (where ping_gap_seconds <= 180) as n_tight,
       count(*) filter (where ping_gap_seconds > 600) as n_poor
from prediction_errors
where has_all_horizons
group by horizon_minutes
order by horizon_minutes;

\echo ''
\echo '== worst routes at the 10 minute horizon =='
select route_id, count(*) as n,
       round(avg(abs_error_seconds)/60.0, 2) as mae_min,
       round(avg(error_seconds)/60.0, 2) as bias_min
from prediction_errors
where horizon_minutes = 10 and ping_gap_seconds <= 180
group by route_id having count(*) >= 200
order by avg(abs_error_seconds) desc
limit 8;

\echo ''
\echo '== best routes at the 10 minute horizon =='
select route_id, count(*) as n,
       round(avg(abs_error_seconds)/60.0, 2) as mae_min
from prediction_errors
where horizon_minutes = 10 and ping_gap_seconds <= 180
group by route_id having count(*) >= 200
order by avg(abs_error_seconds) asc
limit 8;

\echo ''
\echo '== by time of day, 10 minute horizon =='
select (service_minute / 180) * 3 || ':00-' || ((service_minute / 180) * 3 + 3) || ':00'
         as service_hours,
       count(*) as n,
       round(avg(abs_error_seconds)/60.0, 2) as mae_min
from prediction_errors
where horizon_minutes = 10 and ping_gap_seconds <= 180
group by service_minute / 180
order by service_minute / 180;

\echo ''
\echo '== weekday vs weekend, 10 minute horizon =='
select case when is_weekend then 'weekend' else 'weekday' end as day_type,
       count(*) as n,
       round(avg(abs_error_seconds)/60.0, 2) as mae_min
from prediction_errors
where horizon_minutes = 10 and ping_gap_seconds <= 180
group by is_weekend;
