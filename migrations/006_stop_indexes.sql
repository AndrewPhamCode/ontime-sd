-- migrate: no-transaction
--
-- Indexes for the two queries behind a stop popup, which were both sequential
-- scans. /api/stops/{id}/upcoming read every row of predictions (22.6M rows,
-- 8.4s on a laptop) to find a few hundred, and timed out as a 504 on the
-- t4g.small. See DESIGN.md ADR-0051.
--
-- CONCURRENTLY so the collector keeps inserting while the index builds. That
-- forbids a transaction, so this file is not atomic. A failed concurrent build
-- leaves an INVALID index behind that `if not exists` would then skip forever,
-- so each index is dropped before it is created. A retry rebuilds rather than
-- trusting what a failed run left.

-- Upcoming arrivals: one stop, arrival after now.
drop index concurrently if exists predictions_stop_arrival_idx;
create index concurrently predictions_stop_arrival_idx
    on predictions (stop_id, arrival_time);

-- Recent history: one stop and horizon, newest first.
drop index concurrently if exists prediction_errors_stop_idx;
create index concurrently prediction_errors_stop_idx
    on prediction_errors (stop_id, horizon_minutes, arrived_at desc);
