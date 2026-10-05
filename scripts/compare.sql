-- Phase 5: us against MTS, on identical rows.
--
-- Every predictor is scored by the same definition over the same arrivals, which
-- is why they sit in one table distinguished only by `source`. The test window is
-- 2026-10-01 to 10-02; the model and the segment statistics saw nothing after
-- 2026-09-30. See DESIGN.md ADR-0038.

-- Averaging each source over its own rows is not a head to head. The model only
-- predicts where an anchor exists, 77% of rows at the 20 minute horizon, and the
-- rows it skips are the hard ones near the start of a trip. Matching the
-- population is what makes the comparison mean anything. See ADR-0043.
\echo '== HEAD TO HEAD: mean absolute error in minutes, test window only =='
\echo '   matched rows only: every source predicted the same arrivals'
with matched as (
    select start_date, trip_id, stop_sequence, horizon_minutes
    from prediction_errors
    where start_date between '2026-10-01' and '2026-10-02'
      and has_all_horizons and ping_gap_seconds <= 180
    group by 1, 2, 3, 4
    having count(distinct source) = 4
)
select horizon_minutes || ' min' as horizon,
       round(avg(abs_error_seconds) filter (where source='mts')/60.0, 2)           as mts,
       round(avg(abs_error_seconds) filter (where source='persist_delay')/60.0, 2) as persist_delay,
       round(avg(abs_error_seconds) filter (where source='segment_mean')/60.0, 2)  as segment_mean,
       round(avg(abs_error_seconds) filter (where source='lgbm')/60.0, 2)          as lgbm,
       count(*) filter (where source='mts')                                        as n
from prediction_errors pe
join matched using (start_date, trip_id, stop_sequence, horizon_minutes)
group by horizon_minutes
order by horizon_minutes;

\echo ''
\echo '== coverage: rows each source predicted, before matching =='
select horizon_minutes || ' min' as horizon,
       count(*) filter (where source='mts')  as mts_rows,
       count(*) filter (where source='lgbm') as lgbm_rows,
       round(100.0 * count(*) filter (where source='lgbm')
             / nullif(count(*) filter (where source='mts'), 0), 1) as lgbm_coverage_pct
from prediction_errors
where start_date between '2026-10-01' and '2026-10-02'
  and has_all_horizons and ping_gap_seconds <= 180
group by horizon_minutes
order by horizon_minutes;

\echo ''
\echo '== p90 absolute error in minutes =='
select horizon_minutes || ' min' as horizon,
       round((percentile_cont(0.9) within group (order by abs_error_seconds)
              filter (where source='mts'))::numeric/60, 2)           as mts,
       round((percentile_cont(0.9) within group (order by abs_error_seconds)
              filter (where source='persist_delay'))::numeric/60, 2) as persist_delay,
       round((percentile_cont(0.9) within group (order by abs_error_seconds)
              filter (where source='segment_mean'))::numeric/60, 2)  as segment_mean,
       round((percentile_cont(0.9) within group (order by abs_error_seconds)
              filter (where source='lgbm'))::numeric/60, 2)          as lgbm
from prediction_errors
where start_date between '2026-10-01' and '2026-10-02'
  and has_all_horizons and ping_gap_seconds <= 180
group by horizon_minutes
order by horizon_minutes;

\echo ''
\echo '== bias in minutes (negative means predicting too early) =='
select source,
       round(avg(error_seconds)/60.0, 2) as bias_min,
       round(avg(abs_error_seconds)/60.0, 2) as mae_min,
       count(*) as n
from prediction_errors
where start_date between '2026-10-01' and '2026-10-02'
  and has_all_horizons and ping_gap_seconds <= 180
group by source
order by avg(abs_error_seconds);

\echo ''
\echo '== where do we beat MTS, and where not? 10 minute horizon by route =='
with per_route as (
  select route_id,
         avg(abs_error_seconds) filter (where source='mts') as mts,
         avg(abs_error_seconds) filter (where source='lgbm') as lgbm,
         count(*) filter (where source='mts') as n
  from prediction_errors
  where horizon_minutes = 10 and start_date between '2026-10-01' and '2026-10-02'
    and has_all_horizons and ping_gap_seconds <= 180
  group by route_id having count(*) filter (where source='mts') >= 150)
select route_id, n,
       round(mts/60.0, 2) as mts_min,
       round(lgbm/60.0, 2) as lgbm_min,
       round((mts - lgbm)/60.0, 2) as improvement_min
from per_route order by (mts - lgbm) desc limit 6;

\echo ''
\echo '== provenance of the latest run =='
select trained_at::timestamp(0), train_from, train_to, test_from, test_to,
       train_rows, test_rows, notes
from model_runs order by trained_at desc limit 1;
