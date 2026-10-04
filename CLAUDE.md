# OnTime SD

Real-time San Diego MTS arrival predictions that beat the official ETAs.

**The one metric that matters:** mean absolute error (MAE) of arrival predictions, ours vs. MTS's official predictions, broken down by prediction horizon (1, 5, 10, 20 minutes out). Every phase should move toward being able to state: "cut average ETA error from X to Y minutes."

## Owner and working rules

This is Andrew's portfolio project. He has to defend every design decision in interviews, so:

- **Plan before code.** For any non-trivial change, propose the approach and tradeoffs first and wait for approval.
- **Explain design choices** in plain terms as you go: why this data structure, why this query, what breaks at scale.
- **Claude may write the model (Phase 5) directly**, including features, training and tuning. Andrew asked for this on 2026-10-04 to move faster on beating the MTS baseline. Explain every modelling choice as you go, because he still has to defend it in interviews. Arrival inference (Phase 3) and the evaluation metric (Phase 4) stay Andrew's: for those, explain the approach, write tests and scaffolding, review his code, but don't write the core algorithm unless he explicitly asks.
- **Never buy a win.** No feature derived from MTS's own prediction, no loosening the label-quality filter to flatter a number, no tuning against the test window, and no reporting a cherry-picked subset as the headline. A result that cannot survive ADR-0038's standard is worth less than no result.
- Small, focused commits. One concern per PR.
- Never commit secrets. The API key lives only in `.env` (gitignored). Never use API keys found in other people's repos online.
- Don't use em dashes in docs or comments.

## Data sources

- **Static GTFS (schedule):** http://www.sdmts.com/google_transit_files/google_transit.zip
- **GTFS-Realtime (needs key, env var `MTS_API_KEY`):**
  - Vehicle positions: `https://realtime.sdmts.com/api/api/gtfs_realtime/vehicle-positions-for-agency/MTS.pb?key=KEY`
  - Trip updates (official predictions): `https://realtime.sdmts.com/api/api/gtfs_realtime/trip-updates-for-agency/MTS.pb?key=KEY`
  - Swap `.pb` for `.pbtext` to see a human-readable version. Do this first when the key arrives.
- The realtime feed is served by OneBusAway. **Unverified assumption:** OBA trip updates may only include one `stop_time_update` with a `delay` rather than per-stop arrival times, meaning the official ETA for downstream stops = scheduled time + delay. Confirm from real `.pbtext` output before building Phase 4.
- MTS terms: https://www.sdmts.com/business-center/app-developers/terms-and-conditions

## Stack

- Python 3.11+, `httpx` (async), `asyncpg`, `gtfs-realtime-bindings`, `pytest`
- Postgres (TimescaleDB optional; schema must work on plain Postgres too)
- Later: FastAPI (API), React + Vite + MapLibre (map UI), LightGBM (model)
- Docker Compose for local dev, GitHub Actions for CI (tests run against a real Postgres service container, not mocks)
- Deploy: AWS (EC2 or ECS + RDS), infra as code with CDK or Terraform

## Phases

### Phase 1: Realtime collector (URGENT, must run 24/7 ASAP)
The model needs weeks of history, so the collector ships first and never stops.
- Poll vehicle positions and trip updates every 30s, each feed as an independent async task.
- Skip a poll if the feed header timestamp hasn't changed.
- `vehicle_positions`: primary key `(vehicle_id, ts)`, insert with `ON CONFLICT DO NOTHING` (feeds repeat the same record across polls).
- `predictions`: **change-only storage.** Keep the last written prediction per `(start_date, trip_id, stop_sequence)` in memory and only write when it changes by >= 30s. Storing every prediction every poll would be tens of millions of rows per day.
- `poll_log` table: every poll records status, HTTP code, entity count, rows written, latency, error. This is how we detect feed outages and how the dashboard shows health.
- Exponential backoff with jitter on failures, capped at 5 min. Graceful shutdown on SIGTERM. JSON logs. `/healthz` endpoint that returns 503 if no successful poll in 5 min.
- A **mock feed server** that generates fake moving buses in GTFS-RT format, so everything runs before the API key arrives. Supports failure injection (random 500s, slow responses) to test backoff.

### Phase 2: Static GTFS loader
- Download the zip, hash it (sha256) as `feed_version`, skip if already loaded.
- Load stops, routes, trips, stop_times, shapes, calendar using `COPY`, not row-by-row inserts.
- Store GTFS times as **seconds past service-day midnight** (integers). GTFS times can exceed 24:00:00 for trips running past midnight.
- Service day timezone is America/Los_Angeles.

### Phase 3: Arrival inference (Andrew writes core logic)
Derive the ground truth: when did each vehicle actually arrive at each stop?
- Snap GPS points to the route shape (distance along route).
- Interpolate the time the vehicle crossed each stop's position.
- Handle: GPS noise, vehicles sitting at a stop, missing pings, detours, trips that never finish.

### Phase 4: Baseline evaluation (Andrew writes core logic)
- For each actual arrival, look up what MTS predicted at 1/5/10/20 min before. Compute MAE and p90 error per horizon, per route, per time of day.
- This produces the number to beat.

### Phase 5: Model
- Baseline first: historical mean travel time per stop-to-stop segment by time-of-day bin and weekday/weekend.
- Then LightGBM with features like segment, time bin, recent observed speeds on the segment, current delay.
- Evaluate on a time-based split (train on earlier weeks, test on later). Never random splits.

### Phase 6: API + map
- FastAPI endpoint: arrivals for a stop (ours, official, scheduled).
- React map with live vehicles, click a stop to see ours vs. official vs. what actually happened.

### Phase 7: Production
- Deploy on AWS with CDK or Terraform, CloudWatch alarms on collector staleness.
- `DESIGN.md` (decisions and rejected alternatives) and `RUNBOOK.md` (alerts and what to do).

## Commands

`make help` lists everything. The ones used most:

| Command | What it does |
| --- | --- |
| `make install` | Sync the venv from pyproject.toml (uv) |
| `make up` / `make down` | Start / stop Postgres 16 in Docker on host port **5433** |
| `make migrate` | Apply pending SQL migrations |
| `make mock` | Run the mock GTFS-Realtime feed server |
| `make run` | Run the collector against whatever `MTS_FEED_BASE_URL` points at |
| `make test` | Full suite against a real Postgres |
| `make lint` / `make fmt` | ruff check and format |
| `make psql` | psql shell on the compose database |
| `make health` | Hit the collector `/healthz` endpoint |
| `make coverage` | Poll outcomes, collection gaps, and compression ratio from `poll_log` |
| `make service-install` | Install and start the launchd agent for 24/7 collection |
| `make service-uninstall` | Stop and remove the launchd agent |
| `make service-status` | Whether launchd is running the collector |
| `make service-logs` | Follow the JSON collector log |

Port 5433, not 5432, because a Homebrew `postgresql@16` instance already owns
5432 on the dev machine. See DESIGN.md ADR-0001.

Docs: `DESIGN.md` for why each decision was made and what was rejected,
`RUNBOOK.md` for what to do when something breaks.
