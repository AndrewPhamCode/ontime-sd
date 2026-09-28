# DESIGN

Architectural decisions for OnTime SD, with the alternatives that were rejected
and why. The goal of this file is that every choice in the codebase can be
defended, including the ones that are deliberately temporary.

The project metric is mean absolute error of arrival predictions against MTS's
official predictions, per horizon (1, 5, 10, 20 minutes). Decisions are judged
by whether they move toward being able to state that number honestly.

Each record uses the same shape: the decision, why, what was rejected, and what
breaks at scale. Status is one of Accepted, Provisional (works now, expected to
change), or Superseded.

---

## ADR-0001: Postgres comes from Docker Compose, on host port 5433

**Status:** Accepted

**Decision.** Development Postgres runs as `postgres:16` from `docker-compose.yml`,
published on host port 5433.

**Why.** CI runs a `postgres:16` service container and production will be RDS
Postgres. Using the same engine version in all three places means a migration
that works locally works everywhere. Port 5433 is deliberate: this machine
already runs Homebrew `postgresql@16` on 5432, and binding to 5432 would have
produced a confusing bind failure on the very first `compose up`.

**Rejected.** Using the Homebrew instance directly. It was already running and
would have been faster to start, but the local environment would then drift from
CI in version, extensions, and configuration, and there would be no reproducible
setup to hand to another machine. Also rejected: SQLite for early development,
which cannot express the `ON CONFLICT` and `COPY` behavior the ingest path
depends on, so the tests would not be testing the real thing.

**At scale.** Compose is a development convenience only. Production is managed
Postgres. TimescaleDB is optional per the project charter, so no decision here
may depend on a Timescale-only feature.

---

## ADR-0002: Plain SQL migrations with a small runner

**Status:** Accepted

**Decision.** Migrations are numbered `.sql` files in `migrations/`, applied in
order by a short runner that records applied versions in a `schema_migrations`
table.

**Why.** The schema is small, mostly DDL, and deliberately tuned at the SQL
level: composite primary keys, partial and composite indexes, `COPY` targets.
Writing that as SQL is direct and reviewable. The runner needs to do two things,
find pending files and apply each in a transaction, which is about forty lines.

**Rejected.** Alembic. It is the default answer for Python migrations but it is
built around SQLAlchemy model autogeneration, and there are no ORM models here
(see ADR-0003). It would add a dependency, a config file, and a migration
environment in exchange for features this project will not use. Also rejected:
applying schema at application startup, which makes the schema an implicit side
effect of running the collector and makes rollbacks untestable.

**At scale.** A linear version counter assumes a single author. With concurrent
contributors, numbered files collide and the fix is timestamped filenames plus a
merge check in CI.

---

## ADR-0003: asyncpg with raw SQL, no ORM

**Status:** Accepted

**Decision.** All database access goes through `asyncpg` with hand written SQL.

**Why.** Three reasons, in order of weight. First, the ingest path is bulk
insert with conflict handling, which is exactly where ORMs are slowest and least
expressive. Second, Phases 3 through 5 are analytical queries (interpolating
arrival times, joining predictions to actual arrivals within a time window) that
are clearer as SQL than as query builder chains. Third, asyncpg is already
required for its native async support and prepared statement handling, so adding
an ORM would mean carrying two abstractions.

**Rejected.** SQLAlchemy Core or ORM. It would give portability across engines,
but this project is Postgres specific on purpose and portability is not a goal.
The cost is losing direct control over the insert strategy documented in
ADR-0006.

**At scale.** Raw SQL means no compile time guarantee that a query matches the
schema. The mitigation is that every query runs in a test against a real
Postgres (ADR-0016), so a schema change that breaks a query fails CI rather
than production.

---

## ADR-0004: vehicle_positions is keyed on (vehicle_id, ts) with ON CONFLICT DO NOTHING

**Status:** Accepted

**Decision.** Primary key `(vehicle_id, ts)`, and inserts use
`ON CONFLICT DO NOTHING`.

**Why.** GTFS-Realtime feeds republish the same vehicle record across
consecutive polls, because the feed updates on the agency's cadence and not
ours. Polling every 30 seconds therefore sees each record more than once. Making
the natural key the primary key turns deduplication into a database guarantee
instead of application logic, and makes the whole ingest path idempotent: a
retry after a partial failure is safe, and replaying a poll cannot create
duplicates.

**Rejected.** A surrogate `bigserial` key with a unique index on
`(vehicle_id, ts)`. That costs an extra 8 bytes per row and an extra index for
no benefit, since nothing references a position row by id. Also rejected:
deduplicating in application memory before insert, which would work until the
process restarts and would not protect against two collectors running at once.

**At scale.** If a vehicle ever reports two genuinely different positions at the
same timestamp, the second is silently discarded. That is an accepted loss: the
timestamp is the vehicle's own report time, so two distinct positions sharing it
indicates upstream corruption rather than real movement. Also, a composite text
primary key means the index is larger than an integer index would be, which
matters once the table is in the hundreds of millions of rows, and is when
partitioning by day becomes the answer.

---

## ADR-0005: Predictions are stored change-only, against an in memory last written map

**Status:** Provisional

**Decision.** Keep the last written prediction per
`(start_date, trip_id, stop_sequence)` in a process local dictionary, and write
a new row only when the predicted time moves by at least
`PREDICTION_CHANGE_THRESHOLD_SECONDS` (default 30).

**Why.** This is the decision that makes the project storable. Trip updates
cover on the order of a thousand active trips with dozens of upcoming stops
each, republished every 30 seconds. Writing every prediction every poll is tens
of millions of rows per day, and almost all of them are byte for byte identical
to the row before. Storing only transitions keeps the table proportional to how
much MTS actually changes its mind, which is also the only part of the signal
Phase 4 cares about.

**Rejected.** Writing everything and deduplicating later in a batch job. It is
simpler to reason about and loses nothing, but it means provisioning for tens of
millions of writes per day from day one, and the dedupe job becomes a permanent
operational dependency. Also rejected: a database side trigger or a
`DISTINCT ON` materialized view, which moves the same work into Postgres and
makes the write path harder to reason about under load.

**Consequences.** The cache is process local, so a restart starts cold and the
first poll after startup writes one full burst of rows, roughly one per active
stop time. This is accepted for now because it is bounded and self correcting,
and because a restart is also exactly when a gap in `poll_log` needs explaining.
Warming the cache from the database on startup is the obvious follow up and is
deliberately not in Phase 1.

**At scale.** Memory is bounded by active trips times upcoming stops, on the
order of 100k small entries, which is tens of megabytes. Entries must be evicted
once a service day closes or the map grows without limit across days. Two
collectors running at once would each keep their own cache and write duplicate
transitions, which the primary key absorbs but which would distort any count of
how often predictions change.

---

## ADR-0006: Batch writes use executemany now, with COPY to a staging table as the documented scale path

**Status:** Provisional

**Decision.** Insert batches with `executemany` over a prepared statement.

**Why.** At roughly a thousand rows per poll every 30 seconds, this is single
digit milliseconds and is not close to being the bottleneck. Choosing the
simpler mechanism first keeps the ingest path readable.

**Rejected for now.** `copy_records_to_table`, which is substantially faster.
The obstacle is that `COPY` cannot express `ON CONFLICT DO NOTHING`, and
conflicts are the normal case here (ADR-0004), not an edge case. The real
version of this optimization is `COPY` into an unlogged staging table followed by
`INSERT ... SELECT ... ON CONFLICT DO NOTHING`, which is two statements and a
temporary table to manage. That complexity is not yet earned.

**At scale.** The trigger to switch is insert latency approaching a meaningful
fraction of the 30 second poll interval, which `poll_log.duration_ms` measures
directly. The staging table approach is the first move; partitioning
`vehicle_positions` by day is the second.

---

## ADR-0007: A poll with an unchanged feed header timestamp is skipped before parsing

**Status:** Accepted

**Decision.** Compare the incoming `FeedHeader.timestamp` to the last one seen
for that feed. If unchanged, record the poll as `skipped_unchanged` and do no
parsing and no writing.

**Why.** MTS publishes on its own schedule. Polling every 30 seconds against a
feed that refreshes less often means a meaningful share of polls carry data
already processed. The header timestamp is the agency's own statement of feed
freshness, so it is the cheapest correct signal available, and checking it
avoids both the protobuf parse and the round trip to the database.

**Rejected.** Hashing the response body. It catches the case of a feed whose
header is stale but whose contents changed, which would be an upstream bug, at
the cost of hashing every payload. Also rejected: trusting HTTP `ETag` or
`Last-Modified`, which depends on OneBusAway's caching behavior rather than on
the feed's own semantics, and has not been verified against the real endpoint.

**At scale.** Recording skipped polls rather than staying silent is what makes
the distinction between "the feed is stale" and "the collector is down"
visible, which is the whole purpose of `poll_log` (ADR-0018).

---

## ADR-0008: One independent asyncio task per feed, under a restarting supervisor

**Status:** Accepted

**Decision.** Vehicle positions and trip updates each get their own poll loop
task with its own backoff state. A supervisor restarts a task that dies.

**Why.** The two feeds fail independently and matter independently. Vehicle
positions are the ground truth input for Phase 3 arrival inference; trip updates
are the baseline to beat in Phase 4. An outage on one must not stop collection
of the other, because the lost data is unrecoverable in both cases. Separate
backoff state means a failing feed does not throttle a healthy one.

**Rejected.** A single loop polling both feeds in sequence. Simpler, but one
slow or failing feed delays the other, and a shared backoff timer would pause
collection of a feed that is perfectly healthy. Also rejected: separate OS
processes per feed, which gives true isolation but duplicates the connection
pool and the health endpoint, and makes graceful shutdown a multi process
problem for no gain at this size.

**At scale.** Two tasks in one process share a failure domain: an unhandled
error in shared code, or the process being killed, stops both. That is accepted
because the supervisor plus launchd `KeepAlive` (ADR-0015) covers crash restart,
and true redundancy would require solving duplicate writes first.

---

## ADR-0009: Full jitter exponential backoff, 1 second base, capped at 300 seconds

**Status:** Accepted

**Decision.** On failure, wait a random duration between zero and
`min(base * 2^attempt, 300)` seconds. Reset to the base on the next success.

**Why.** Exponential growth stops a hammering loop during a sustained outage.
The 300 second cap comes from the charter and bounds worst case staleness: after
any outage, collection resumes within five minutes, which is the same threshold
the health endpoint uses. Full jitter rather than fixed delay matters because
both feed tasks fail together during a shared outage (network down, MTS down)
and would otherwise retry in lockstep forever, producing synchronized bursts.

**Rejected.** A fixed retry interval, which either hammers a down service or
recovers slowly, with no setting that is good at both. Also rejected: retrying
inside the HTTP client. Keeping retry policy in the poll loop means one place
decides, and every attempt gets a `poll_log` row, so retries are observable
rather than hidden inside a library.

**At scale.** Capped backoff means a long outage produces a steady low rate of
failing requests rather than silence, which is intentional: the recovery is
detected within five minutes without external orchestration.

---

## ADR-0010: Fixed cadence measured from poll start, with overlapping polls skipped

**Status:** Accepted

**Decision.** Each iteration targets a fixed 30 second grid measured from when
the poll started, not 30 seconds after it finished. If a poll is still running
when the next is due, the next is skipped rather than queued.

**Why.** Sleeping a fixed interval after each poll makes the real cadence
`interval + response time`, so the collector drifts, and it drifts most exactly
when the feed is slowest and the data matters most. Anchoring to the start keeps
the sampling interval stable, which matters because Phase 3 interpolates
positions between pings and Phase 4 looks up predictions at specific horizons.
Even sampling makes both less biased. Skipping rather than queueing prevents a
slow feed from building an unbounded backlog of pending polls that would all
fire at once on recovery.

**Rejected.** Sleep after work, which is one line simpler and drifts. Also
rejected: an external scheduler such as cron, which cannot hold the in memory
prediction cache (ADR-0005) across invocations and would reload it every run.

**At scale.** A skipped poll is a real gap in sampling, so it is recorded as
such rather than passed over quietly.

---

## ADR-0011: The health endpoint is a stdlib asyncio HTTP server, not FastAPI

**Status:** Accepted

**Decision.** `GET /healthz` is served by a roughly twenty line handler over
`asyncio.start_server`. It returns 200 when a successful poll happened within
`HEALTH_STALE_AFTER_SECONDS` (default 300) and 503 otherwise.

**Why.** The collector is the one component that must never stop, and its
dependency surface should stay as small as its job. FastAPI and uvicorn are
already planned for Phase 6, where a real API with routing, validation, and
serialization makes them worth their weight. Pulling them in now to serve one
endpoint with no parameters means the process that must not crash carries a web
framework it does not need.

**Rejected.** FastAPI plus uvicorn now, for consistency with Phase 6. Rejected
because consistency is not worth the dependency in the process with the
strictest reliability requirement. Also rejected: no HTTP endpoint at all, with
health inferred by querying `poll_log` externally. That works for a dashboard
but gives no readiness signal for a process manager or, later, an ECS or ALB
health check.

**At scale.** A hand written HTTP handler supports exactly one route and no TLS.
If the collector ever needs to expose more, this decision is superseded by the
Phase 6 API rather than extended.

---

## ADR-0012: The mock feed mirrors the real MTS URL shape, including the key parameter

**Status:** Accepted

**Decision.** The mock server serves valid GTFS-Realtime protobuf at
`/api/api/gtfs_realtime/vehicle-positions-for-agency/MTS.pb` and
`.../trip-updates-for-agency/MTS.pb`, accepts a `key` query parameter, and
supports `.pbtext` for human readable output. Mock and production differ only by
`MTS_FEED_BASE_URL`.

**Why.** The API key has been requested but has not arrived, and the charter is
explicit that the collector cannot wait for it. Matching the real URL structure
means the code path exercised in tests, including how the key is attached to the
request, is the same code path that will run against MTS. The alternative is
discovering the difference on the day the key arrives, which is the worst
possible time.

Failure injection is part of this decision, not an extra: random 500s, slow
responses, truncated protobuf, and a deliberately frozen header timestamp. Those
four cases are what make ADR-0007, ADR-0009, and the error branches of
`poll_log` testable at all. A feed that only ever behaves correctly cannot
validate a collector whose main job is surviving a feed that does not.

**Rejected.** Recorded fixture files replayed from disk. They give byte exact
real data, which the mock cannot, but they are static: they cannot produce
moving vehicles over time, so arrival inference in Phase 3 has nothing to work
with, and they cannot inject failures. The intent is to add real recorded
fixtures alongside the mock once the key exists, not instead of it.

**At scale.** Simulated vehicles move along a synthetic shape with none of the
GPS noise, dropped pings, or detours that make Phase 3 hard. The mock proves
the pipeline runs; it cannot validate inference quality. That work waits for
real data, and pretending otherwise would produce a Phase 3 that only works on
clean input.

---

## ADR-0013: All realtime timestamps are timestamptz; service day seconds are a Phase 2 concern

**Status:** Accepted

**Decision.** Every time column in the realtime schema is `timestamptz`. GTFS
static times stored as seconds past service day midnight are Phase 2 only.

**Why.** Realtime feed times are POSIX timestamps, which are absolute instants,
and `timestamptz` stores them without imposing a local interpretation. The
service day representation exists to solve a different problem: static GTFS
times legitimately exceed 24:00:00 for trips running past midnight, so they
cannot be times of day, and they are relative to a service day whose length
varies across daylight saving transitions. Mixing the two representations in one
table would invite exactly the bug where a trip near midnight is attributed to
the wrong service day.

**Rejected.** Storing epoch integers, which is what the feed provides and would
avoid a conversion. Rejected because every later query would need to convert for
comparison and for time of day bucketing in Phases 4 and 5, and Postgres already
does this correctly. Also rejected: naive `timestamp` without a zone, which
would silently be read as whatever the session timezone happened to be.

**At scale.** `start_date` is carried as a separate `date` column alongside
absolute timestamps, because trip identity in GTFS-Realtime is
`(start_date, trip_id)` and not `trip_id` alone. Service day boundaries are
America/Los_Angeles per the charter.

---

## ADR-0014: Config is a frozen dataclass read from the environment, with secrets only in .env

**Status:** Accepted

**Decision.** A frozen dataclass reads settings from environment variables once
at startup. `python-dotenv` loads `.env` for local development. `.env` is
gitignored from the first commit and `.env.example` is committed with the key
blank.

**Why.** Reading config once, into an immutable object, means every setting has
one documented name and no code path can mutate it mid run. A frozen dataclass
gives that with no dependency. Loading from the environment is what the eventual
deployment target expects.

**Rejected.** `pydantic-settings`, which adds validation and type coercion and
is genuinely good. It arrives with FastAPI in Phase 6, and at that point
migrating is reasonable. Rejected now for the same reason as ADR-0011: the
collector carries only what it needs. Also rejected: a committed YAML or TOML
config file, because the one setting that must never be committed is the API key,
and a config file next to the code is a standing invitation to paste it in.

**At scale.** Reading config only at startup means a change requires a restart,
which is correct for this process: a restart is cheap and cleanly logged, and
live reload would interact badly with the in memory prediction cache.

---

## ADR-0015: 24/7 operation via launchd on the development Mac, with gaps recorded rather than hidden

**Status:** Provisional

**Decision.** A launchd agent with `RunAtLoad` and `KeepAlive` runs the
collector on this machine. Sleep and offline gaps are accepted.

**Why.** Phase 5 needs weeks of history and that history can only be gathered in
real time, so every day of delay is training data that cannot be recovered
later. Waiting for the Phase 7 AWS deployment before collecting anything would
trade weeks of irreplaceable data for infrastructure polish. launchd gives
restart on crash and start at login with no new infrastructure.

**Rejected.** A cloud VM now, which is the correct long term answer and has no
sleep gaps, but adds deployment and cost work ahead of the collector being
proven correct. Also rejected: cron, which cannot hold the prediction cache
across runs (ADR-0005) and has no supervision. Also rejected: running only
during development sessions, which would produce history biased toward waking
hours, and time of day is a feature in Phase 5.

**Consequences.** The collector depends on Docker Desktop being up for the
database, so Docker Desktop must be set to start at login. If it is not, the
collector starts, fails to connect, backs off, and recovers once Docker is
running, which is the correct behavior but shows up as a startup gap.

**At scale.** Gaps from sleep, network loss, and Docker not being ready are
real and will bias any naive analysis of collection coverage. They are
mitigated, not solved, by recording every poll outcome in `poll_log` so coverage
is measurable rather than assumed. Moving to an always on host is the next
operational step after the collector is verified.

---

## ADR-0016: Tests run against a real Postgres, in a temporary database

**Status:** Accepted

**Decision.** A session scoped fixture creates a temporary database, runs all
migrations into it, and drops it afterward. No database mocks or fakes. The same
code path runs locally against compose and in CI against a service container.

**Why.** Required by the project charter, and the reason is specific rather than
general: the correctness of this system lives in behavior only a real Postgres
exhibits. `ON CONFLICT DO NOTHING` semantics, composite primary key
enforcement, `timestamptz` conversion, and index behavior are the actual
subjects of the tests. A mock would assert that the code called the functions
the test author expected, which is a test of the test.

**Rejected.** SQLite in memory for speed. It has different conflict syntax, no
real `timestamptz`, and different type affinity, so passing tests would carry no
information about production. Also rejected: a shared persistent test database,
which makes tests order dependent and leaves state behind on failure.

**At scale.** Test runtime grows with migration count, since every session
replays the full schema. The fix when that hurts is a template database created
once and cloned per test, which Postgres supports directly.

---

## ADR-0017: The 30 second threshold is accepted lossy compression, and raw protobuf is not archived

**Status:** Accepted

**Decision.** Prediction changes smaller than the threshold are discarded and
never recoverable. Raw `.pb` payloads are not written to disk.

**Why.** This is the honest statement of what ADR-0005 costs. At two feeds
polled every 30 seconds, archiving raw payloads is roughly 5,800 responses per
day at a few hundred kilobytes each, which is gigabytes per day and grows
without bound, for data that is mostly redundant with what the schema already
stores. Vehicle positions are already stored losslessly, so the irreversible
loss is limited to sub threshold prediction jitter, which is noise rather than
signal for horizons measured in minutes.

**Rejected.** Archiving gzipped raw payloads for a rolling window, which would
allow reprocessing if the ingest logic turns out to have a bug. This is the
strongest argument against this decision and it is a real risk: a parsing
mistake discovered in Phase 4 cannot be repaired retroactively. It is rejected
for Phase 1 on cost, and it is cheap to add later behind a config flag if the
ingest path proves to need it.

**At scale.** If Phase 4 ever needs prediction resolution finer than 30 seconds,
this decision has to be revisited before that analysis, not after, because the
data to support it will not exist. The threshold is configurable precisely so
that lowering it does not require a code change.

---

## ADR-0018: Every poll attempt writes a poll_log row

**Status:** Accepted

**Decision.** Each poll of each feed inserts one row recording feed, start time,
duration, outcome status, HTTP code, feed header timestamp, entity count, rows
written, and error text. Statuses are `ok`, `skipped_unchanged`, `http_error`,
`parse_error`, and `db_error`.

**Why.** Without this, a feed outage and a collector outage look identical, and
so do a stale feed and a working one. Recording every attempt including the
skips and the failures means coverage and feed health are queryable facts rather
than inferences from missing rows. It is also what the health endpoint reads and
what the Phase 6 dashboard will show, so one write serves monitoring,
alerting, and debugging.

Separating the status values matters: `skipped_unchanged` is a healthy outcome
and must not be counted as a failure, while `http_error` and `parse_error`
distinguish "MTS is down" from "MTS changed its output", which need different
responses.

**Rejected.** Logging only failures. Half the size, but then there is no
denominator: with no record of successes, the failure rate and the collection
coverage that Phase 5 needs are both uncomputable. Also rejected: relying on the
JSON application logs alone, which are on local disk, rotate, and cannot be
joined to the collected data in SQL.

**At scale.** This table grows at a fixed, predictable rate of about 5,800 rows
per day, which is trivial, and it is the first thing to check when the data
looks wrong.

---

## ADR-0019: The mock emits both possible trip update shapes

**Status:** Accepted

**Decision.** The mock serves trip updates in either of two shapes, selected by
`MOCK_TRIP_UPDATE_STYLE`. `per_stop` emits a `stop_time_update` per upcoming
stop, each with an absolute arrival time. `single_delay` emits exactly one
`stop_time_update` carrying only a `delay`, with no arrival time.

**Why.** CLAUDE.md records an explicit unverified assumption: the OneBusAway feed
behind MTS may only include one `stop_time_update` with a `delay`, in which case
the official ETA for downstream stops is scheduled time plus delay. That
assumption cannot be checked until the key arrives, and the collector cannot wait
for it. Building for one shape and guessing means a 50 percent chance of
discovering the guess was wrong at the moment the key lands, with the schema
already committed.

Supporting both means the ingest path is exercised against either outcome now,
and the `predictions` table holds both: `arrival_time` is nullable and
`delay_seconds` is a separate column, so a `single_delay` feed produces a valid
row with a null arrival and a populated delay.

**Rejected.** Implementing only `per_stop`, which is the richer shape and the one
the schema is designed around. Rejected because the cost of supporting both is
one branch in the simulator and one test, while the cost of guessing wrong is
reworking the schema after collection has already started, which means either
discarding history or migrating it.

**At scale.** This decision expires the moment the real `.pbtext` is fetched. At
that point one shape is confirmed and the other becomes dead code that should be
deleted rather than maintained, though the nullable columns stay because a real
feed can legitimately omit an arrival time for an individual stop.

---

## ADR-0020: Simulated output is a pure function of the timestamp, seeded by sha256

**Status:** Accepted

**Decision.** Every value the simulator produces, including position, speed
variation, standing delay bias, and prediction drift, is derived from the
timestamp and a stable key, using sha256 rather than Python's builtin `hash` or
a stateful random generator.

**Why.** Two reasons. First, tests can assert on feed contents without
controlling a clock or injecting a fake random source: the same instant always
produces the same bytes. Second, `hash()` is salted per process in Python, so a
mock built on it would emit different feeds after every restart, which would make
a restart indistinguishable from a real change in the data and would break any
test that compared across processes.

The drift period is deliberately longer than one poll interval, and its amplitude
deliberately spans the collector's 30 second write threshold. A mock where every
poll changed every prediction would make change-only storage look useless; one
where nothing changed would make it look perfect. Both would be the mock lying
about the thing it exists to test, so there is a test asserting that revisions
land on both sides of the threshold.

**Rejected.** A seeded `random.Random` advanced per call. Simpler to write, but
output then depends on call order, so generating one feed changes the next, and a
test that fetches the same instant twice gets different answers.

**At scale.** Determinism is the reason this mock cannot substitute for real data
in Phase 3. Real GPS is noisy in ways that are not a pure function of anything,
and inference tuned against smooth synthetic movement will not survive contact
with the real feed. Recorded real fixtures are the intended complement, not a
replacement for the simulator.
