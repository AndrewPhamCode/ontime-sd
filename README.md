# OnTime SD

[![CI](https://github.com/AndrewPhamCode/ontime-sd/actions/workflows/ci.yml/badge.svg)](https://github.com/AndrewPhamCode/ontime-sd/actions/workflows/ci.yml)

Real-time arrival predictions for San Diego MTS buses and trolleys, built to beat
the agency's own ETAs.

## The metric

One number decides whether this project worked: **mean absolute error of arrival
predictions, ours against MTS's official predictions**, broken down by how far
ahead the prediction was made (1, 5, 10, and 20 minutes out).

**The baseline now exists.** Measured over five service days, on 198,878 arrivals
reconstructed from GPS, MTS's own predictions have a mean absolute error of:

| Horizon | MAE | p90 |
| --- | --- | --- |
| 1 minute | **0.90 min** | 1.65 min |
| 5 minutes | **1.36 min** | 2.80 min |
| 10 minutes | **1.70 min** | 3.60 min |
| 20 minutes | **2.23 min** | 4.85 min |

That is the number to beat, and nothing here beats it yet: the model is Phase 5.

Read these as a preliminary sample rather than a published statistic. They come
from five days at 35 to 70% collection coverage, there is no weekend data, and
error at short horizons is partly this project's own GPS gaps rather than MTS's.
All of that is quantified in [DESIGN.md](DESIGN.md) ADR-0037 rather than hidden.

Being specific about what is unproven matters more here than a good demo. A
prediction system that cannot state its error honestly is not a prediction system.

## Status

| Phase | State | Needs the API key |
| --- | --- | --- |
| 1. Realtime collector | **Done**, running | No, built against a mock feed |
| 2. Static GTFS loader | **Done**, real feed loaded | No, the schedule is a public download |
| 3. Arrival inference | **Done**, 613k arrivals | Needs real GPS, mock is too clean |
| 4. Baseline evaluation | **Done**, number measured | This is where the metric comes from |
| 5. Model | Not started | Needs weeks of real history |
| 6. API and map | Not started | Yes |
| 7. Production deploy | Not started | Yes |

The collector runs continuously because the model needs weeks of history and
history cannot be backfilled. Realtime data not captured is gone.

## How it works

```
  MTS GTFS-Realtime                      MTS static GTFS
  (vehicle positions,                    (schedule, stops,
   trip updates)                          shapes, calendar)
         |                                       |
         | every 30s, two independent            | weekly, one transaction
         | async tasks, jittered backoff         | 1.62M rows via COPY
         v                                       v
  +--------------------------------------------------------+
  |                      Postgres 16                       |
  |                                                        |
  |  vehicle_positions   lossless GPS pings                |
  |  predictions         MTS ETAs, change-only storage     |
  |  poll_log            every poll attempt, incl. skips   |
  |  stop_times/shapes   schedule, keyed by feed_version   |
  |  service_dates       which services run on which day   |
  +--------------------------------------------------------+
         |
         v
  Phase 3: snap GPS to route shape, interpolate stop crossings
  Phase 4: compare MTS predictions to what actually happened  <- the metric
  Phase 5: model that beats it
```

Two things in that diagram are load bearing and worth explaining.

**Change-only prediction storage.** MTS republishes its predictions for every
upcoming stop of every active trip every 30 seconds. Storing all of them would be
tens of millions of near identical rows per day. Instead the collector keeps the
last written prediction per `(start_date, trip_id, stop_sequence)` in memory and
writes only when it moves by 30 seconds or more. Observed in practice: consecutive
polls writing 244, then 167, then 15, then 3 rows as the cache warms.

**`poll_log` records every attempt, including the skips and failures.** Without
the successes there is no denominator, so collection coverage and feed failure rate
would both be uncomputable. This is how a feed outage is told apart from a
collector outage.

## Quickstart

Requires Docker Desktop, [uv](https://docs.astral.sh/uv/), and Python 3.11+.

```bash
cp .env.example .env      # the API key stays here, never committed
make install              # sync the virtualenv
make up                   # Postgres 16 in Docker, on host port 5433
make migrate              # apply the schema
make test                 # 287 tests against a real Postgres

make load-gtfs            # load the real MTS schedule, ~17s for 1.6M rows

make mock                 # terminal 2: fake moving buses in GTFS-RT format
make run                  # terminal 3: the collector
make health               # is it healthy
make coverage             # what the data says actually happened
```

`make help` lists everything. Port 5433 rather than 5432 because the development
machine already runs a Homebrew Postgres.

No API key is needed for any of the above. The mock feed server generates
plausible moving vehicles in real GTFS-Realtime protobuf, mirrors the MTS URL
shape so that switching to production is a single environment variable, and can
inject failures (HTTP 500s, slow responses, truncated protobuf, frozen feed
timestamps) to exercise the error paths.

## What has actually been verified

Measured, not assumed:

- **1,623,412 rows loaded in 16.6 seconds** from the real MTS feed, with every
  table's count matching the raw files exactly.
- **11,432 `stop_times` rows are at hour 24 or later**, maximum 27:36:00. This is
  why schedule times are stored as integer seconds past service-day midnight
  rather than as times of day.
- **`shape_dist_traveled` is in miles**, determined by summing haversine distance
  along the longest shapes and matching the declared value to 0.0%. It is
  converted to metres on load, because Phase 3 compares it against GPS distances
  and the error would otherwise be a silent factor of 1609.
- **All 682 route shapes are strictly monotonic** in distance along the shape,
  asserted at load time because arrival interpolation depends on it.
- **Backoff behaves as designed** during a real feed outage: gaps of 0.05, 0.81,
  2.40, 5.87, 7.04, 24.60 and 9.49 seconds, each inside its doubling ceiling, with
  the two feeds recovering independently 36 seconds apart.
- **287 tests**, run against a real Postgres service container in CI rather than
  against mocks, because the behavior under test is `ON CONFLICT` semantics,
  composite key enforcement, and `timestamptz` handling.

## Known limitations

Stated plainly because they affect how far the results can be trusted:

- **Collection coverage is currently 32%.** The collector runs on a laptop that
  sleeps, producing 64 gaps in a recent 24 hour window. Gaps are recorded in
  `poll_log` rather than hidden, so coverage stays measurable, but moving to an
  always on host is a prerequisite for trusting any model trained on this data.
- **All data collected before the key arrived is synthetic.** It proves the
  pipeline works and is worthless as training data, so it is deleted before real
  collection starts rather than mixed in.
- **The trip update format needs confirming against the real feed.** The feed is
  served by OneBusAway, which may report a single delay rather than per stop
  arrival times. The collector handles both shapes, so collection is not blocked
  either way, but Phase 4's lookup depends on which one is real.
- **Service day inference is approximate** when the feed omits `start_date`, which
  is wrong for a trip observed after midnight. Phase 2 provides what is needed to
  fix this; the fix is deferred to Phase 3.

## Stack

Python 3.11+, `httpx`, `asyncpg` with raw SQL and no ORM, `gtfs-realtime-bindings`,
`pytest`. Postgres 16, working on plain Postgres with no required extensions.
Docker Compose for local development, GitHub Actions for CI. FastAPI, React with
MapLibre, and LightGBM arrive in later phases.

## Documentation

- **[DESIGN.md](DESIGN.md)** records all 32 architectural decisions, each with its
  reasoning, the alternatives that were rejected, and what breaks at scale. Start
  here to understand why anything is the way it is.
- **[RUNBOOK.md](RUNBOOK.md)** covers what to do when something breaks, what is
  deliberately not an alert, and the steps to take the day the API key arrives.
- **[CLAUDE.md](CLAUDE.md)** is the project charter and working rules.

Data from [San Diego MTS](https://www.sdmts.com/business-center/app-developers/terms-and-conditions).
