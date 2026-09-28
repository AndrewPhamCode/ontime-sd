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

## ADR-0006: Batch writes use a single INSERT over unnested arrays

**Status:** Accepted (revised during implementation)

**Decision.** Each batch is written with one `INSERT ... SELECT * FROM unnest($1::text[], $2::timestamptz[], ...)` carrying one array per column, with
`ON CONFLICT DO NOTHING`.

**Why.** This was planned as `executemany` and changed while building it, because
unnest turned out to be better on every axis that matters here. It is a single
round trip rather than one per row. More importantly, Postgres reports the
number of rows the statement actually affected, so the insert returns the count
genuinely inserted rather than the count offered. That number is what `poll_log`
records, which means the gap between rows offered and rows written is a direct
measurement of deduplication doing its job. `executemany` cannot report that,
which would have left `rows_written` as a count of attempts and made the
change-only compression claim unmeasurable from the data.

**Rejected.** `executemany` over a prepared statement, the original plan. At a
thousand rows per poll it is fast enough, but it reports nothing useful about
what it did. Also rejected: `copy_records_to_table`, which is faster still but
cannot express `ON CONFLICT DO NOTHING`, and conflicts are the normal case here
rather than an edge case. The real version of that optimization is `COPY` into an
unlogged staging table followed by `INSERT ... SELECT ... ON CONFLICT DO
NOTHING`, which is two statements plus a temporary table to manage, and is not
yet earned.

**Consequences.** Duplicate keys within a single batch are collapsed in Python
before the insert, so the reported count reflects distinct rows offered. A feed
repeating a vehicle inside one response is an anomaly rather than an expected
case, but it must not break the write.

**At scale.** The trigger to move to the staging table approach is insert latency
approaching a meaningful fraction of the poll interval, which
`poll_log.duration_ms` measures directly. Partitioning `vehicle_positions` by day
is the move after that. One array per column also means the parameter payload
grows with batch size, so a very large batch would need chunking.

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

**Resolved.** The real feed was fetched and it sends **per stop arrival times**,
averaging 17.1 stop time updates per trip. The `single_delay` shape does not occur
and is dead code to be removed rather than maintained. The nullable columns stay,
because the real feed does legitimately omit an arrival time for some stops: 54 of
7,750 carried a departure only. See ADR-0033.

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

---

## ADR-0021: Unusable entities are dropped and counted, never written under a guessed key

**Status:** Accepted

**Decision.** When a feed entity is missing something required to key it, the
entity is skipped, a counter is incremented, and the reason is recorded in the
JSON log line for that poll. Where a missing field has a safe substitute, the
substitute is used and the substitution is itself reported.

Specifically: a vehicle with no id falls back to the entity id, and is dropped if
that is empty too. A vehicle with no timestamp falls back to the feed header
timestamp, reported as `ts_from_header`. A prediction with no `stop_sequence` is
dropped, because GTFS-Realtime permits identifying a stop by `stop_id` alone and
resolving that to a sequence requires the static schedule, which is Phase 2.

**Why.** Every one of these fields is optional in the specification, so a
conforming feed may omit them, and the collector has to keep running when it
does. The alternative to dropping is inventing a key, and an invented
`stop_sequence` would corrupt the Phase 4 horizon lookup in a way that is
invisible: the query would return a prediction for the wrong stop rather than
returning nothing. Silent wrong answers are worse than a recorded gap.

Counting rather than merely skipping is the other half of the decision. A drop
that leaves no trace is indistinguishable from a feed that simply had less data,
which would make a parser regression impossible to notice.

**Rejected.** Failing the whole poll when any entity is unusable, which would let
one malformed record discard several hundred good ones. Also rejected: writing
partial rows with nulls in the key columns, which the primary key forbids anyway,
and which would have meant dropping the primary key.

**Update after the key arrived.** The `no_stop_sequence` case turned out to be
the norm rather than an edge case: the real feed never sends `stop_sequence`, so
this fallback was discarding 100% of predictions. It is now resolved at ingest by
ADR-0033. The `start_date` fallback also applies universally, since the real feed
omits `start_date` on every entity, so every service day is currently inferred from
the observation time. That remains wrong for a trip observed after midnight and is
Phase 3 work.

**At scale.** `ts_from_header` is the fallback that most deserves watching: it is
safe for deduplication but it collapses every vehicle in a poll onto a single
instant, which would bias the Phase 3 interpolation that assumes per vehicle
report times. If it ever appears against the real feed it needs investigating
rather than tolerating. The `no_stop_sequence` counter is the direct signal for
whether Phase 2 is a prerequisite for Phase 4 or merely useful to it.

---

## ADR-0022: The health endpoint reports unhealthy only when every feed is stale

**Status:** Accepted

**Decision.** `/healthz` returns 503 when no feed has succeeded within
`HEALTH_STALE_AFTER_SECONDS`, and 200 when at least one has. A single failing
feed does not make the process unhealthy.

**Why.** This endpoint answers one question: should a supervisor restart this
process. One feed succeeding proves the process is alive and that its event loop,
network, and database all work, so a restart would fix nothing and would throw
away the in memory prediction cache plus any poll in flight. A single feed
failing is a data quality problem, and `poll_log` already records it per feed
with a status and an HTTP code, which is the right place to notice it.

**Rejected.** Requiring every feed to be healthy. That conflates two different
questions, and under it a sustained outage on one MTS endpoint would cause a
restart loop that damages collection on the endpoint that is still working.

**At scale.** This means the endpoint cannot be the only alert. Per feed
staleness has to be alerted on separately from `poll_log`, which is what the
CloudWatch alarms in Phase 7 and the RUNBOOK entries are for. The endpoint is a
liveness probe, not a monitoring system.

---

## ADR-0023: The API key is kept out of logs, not just out of git

**Status:** Accepted

**Decision.** Three measures, together. `Settings.mts_api_key` is declared with
`repr=False` so it cannot appear in a dataclass repr or a traceback. Poll failure
logs use `redacted_feed_url`, which substitutes the key with `REDACTED`. The
`httpx` and `httpcore` loggers are pinned to WARNING regardless of the configured
log level.

**Why.** The charter says never commit secrets, and `.env` is gitignored from the
first commit, but git is only one of the ways a key escapes. The MTS feed passes
the key as a URL query parameter, and httpx logs every request URL at INFO. With
the collector running continuously under launchd and appending to
`~/Library/Logs/ontime-sd/collector.log`, that would have written the key in
plaintext to disk twice every 30 seconds, indefinitely, in a file no one thinks
of as sensitive.

This was found by running the collector end to end and reading its actual log
output, not by reading the code. The lines were visible in the mock run, where
there is no key, which is exactly why it would have been easy to ship: nothing
looks wrong until the real key is configured.

**Rejected.** Turning the log level up to WARNING globally, which would also
silence the poll lines that are the point of having logs. Also rejected: scrubbing
the key from log output in the formatter, which sounds thorough but is a blocklist
of one known secret applied after the fact, and would silently stop working if the
key were ever passed a different way.

**At scale.** A URL query parameter is a poor place for a credential and this is
mitigation, not a fix, since the key can still surface in anything else that sees
the URL, such as an HTTP proxy or a crash reporter. Any future library that logs
requests needs adding to the silenced list, so there is a test asserting a real
request does not put the key in captured log output.

---

## ADR-0024: Every static row is tagged with feed_version, and versions are retained

**Status:** Accepted

**Decision.** Every static GTFS table is keyed on `feed_version` first, where
`feed_version` is the sha256 of the downloaded zip. Old versions are never deleted
by the loader. A `feed_versions` registry holds the download metadata and
`loaded_at`.

**Why.** MTS republishes this feed periodically, and a trip's stops and times can
change between publications. Phase 4 compares what MTS predicted against what
happened, which only means something if the schedule used is the one that was in
effect at the time. With a single current schedule, an evaluation run today and
the same run next month would silently produce different numbers from the same
collected data, and there would be no way to tell which was right. The project
metric has to be reproducible or it is not a metric.

The sha256 is the key rather than MTS's own `feed_info.feed_version` string, which
is free text ("Generated on 20260521 @ 1307142 merged with...") and carries no
uniqueness guarantee. MTS's string is stored alongside it, because that is what
MTS support would ask about.

**Rejected.** Replacing the whole schedule on each load. Simpler queries, no
`feed_version` in any join, smaller database. Rejected because it makes historical
analysis irreproducible, which is the one thing Phase 4 cannot give up. Also
rejected: keeping versions but pruning old ones on a schedule, which adds a
retention job to build and reason about before there is any evidence growth is a
problem.

**At scale.** A full load is about 1.62M rows, measured. At MTS's roughly monthly
cadence that is around 20M rows a year, which is unremarkable for Postgres but
does mean every Phase 3 and 4 query must filter on `feed_version` or it will read
every version at once. Foreign keys cascade from `feed_versions`, so pruning a
version later is a single delete.

---

## ADR-0025: GTFS times are stored as integer seconds past service-day midnight

**Status:** Accepted

**Decision.** `stop_times.arrival_seconds` and `departure_seconds` are integers
counting from service-day midnight, and may exceed 86400.

**Why.** GTFS times are relative to the service day, not the calendar day, so a
trip that departs at 11:30pm and arrives after midnight has an arrival time of
24:30:00 or later. This is not a theoretical edge case: in the feed measured while
building this, **11,432 `stop_times` rows are at hour 24 or later** and the
largest value is 27:36:00. A `time` column cannot represent any of them, and a
`timestamp` would require inventing a date before the service day is known.

Integers also make the arithmetic Phase 3 and 4 need trivial. Travel time between
two stops is a subtraction, with no timezone or daylight saving handling in the
hot path.

**Rejected.** `time` columns, which cannot hold 27:36:00 at all. `interval`, which
can, but is heavier and invites accidental mixing with timestamps. Storing the
original `HH:MM:SS` text and parsing at query time, which pushes a parse into
every consumer and makes indexing useless.

**At scale.** Converting a service-day offset to an absolute instant still needs
the service date and the agency timezone, which is why `service_dates` exists and
why the conversion belongs in one place rather than in each caller. Daylight
saving means a service day is not always 86400 seconds long, and that correction
is Phase 3's problem to handle explicitly rather than something this
representation can hide.

---

## ADR-0026: shape_dist_traveled is converted from miles to metres at load time

**Status:** Accepted

**Decision.** `shape_dist_traveled` from both `shapes.txt` and `stop_times.txt` is
multiplied by 1609.344 and stored as `shape_dist_traveled_m`.

**Why.** The GTFS specification does not mandate a unit for this field, so it had
to be determined rather than assumed. Summing haversine distance along the six
longest shapes and comparing against the declared value matched to 0.0%: shape
`891_2_11` measures 142.00 km, or 88.23 miles, and declares 88.22. It is miles.

Converting at load means one unit exists in the database. Phase 3 computes
distances between GPS points, which is naturally metres, and then compares them to
stop positions along the shape. A unit mismatch there is a factor of 1609 and it
would not look like a unit error, it would look like vehicles teleporting or never
reaching their stops, discovered deep inside inference rather than at the boundary.

**Rejected.** Storing the source value unchanged and converting in each consumer,
which is the same decision made once per caller instead of once in total, with the
failure mode being silent. Also rejected: storing both, which doubles the chance
of a query picking the wrong column.

**At scale.** This assumes MTS keeps publishing miles. The `network` marked test
checks the real feed still has the column, but not the unit, because that needs the
geometric comparison. If MTS ever switched to metres the loader would silently
inflate every distance by 1609, so the geometry check belongs in the verification
step of any future feed format change.

---

## ADR-0027: service_dates is materialized at load rather than computed per query

**Status:** Accepted

**Decision.** At load time, expand `calendar.txt` weekday patterns across their
date ranges, apply `calendar_dates.txt` exceptions, and store the result as
explicit `(feed_version, service_date, service_id)` rows.

**Why.** "Which trips ran on this date" is the single most common question Phases 3
and 4 ask, and answering it from the raw GTFS tables means reimplementing weekday
bitmap expansion plus two kinds of exception in every query that asks. That logic
is easy to get subtly wrong in ways nothing notices: an exclusive range boundary,
or exceptions applied in the wrong order. Doing it once, in one tested function,
means every caller gets the same answer.

It is also cheap. The real feed expands to 3,582 rows, which is nothing, and the
work happens once per load instead of once per query.

Correctness was verified against the real feed rather than only against fixtures:
a Sunday-only service expanded to 13 dates, all Sundays, and independently MTS's
own `service_name` field for that service reads "13 Su". Every
`exception_type=2` date is absent from the result and every `exception_type=1`
date is present.

**Rejected.** A SQL view, which keeps the logic in one place but re-runs the
expansion on every query and cannot be indexed usefully. A helper function in
Python that callers must remember to use, which is a convention rather than a
guarantee.

**At scale.** These rows are derived, so they must be rebuilt whenever a feed
version is loaded and never edited by hand. A service defined purely through
`calendar_dates` with no `calendar.txt` row is handled, because a feed is allowed
to express service that way.

---

## ADR-0028: Bulk loading uses copy_records_to_table over a lazy generator, in chunks

**Status:** Accepted

**Decision.** Rows are transformed in Python generators and loaded with
`asyncpg.copy_records_to_table` in batches of 50,000.

**Why.** `stop_times.txt` is 74 MB and 1.37M rows, so nothing may hold the file in
memory. `zipfile` plus `csv.DictReader` streams it, the mapper is a generator
expression, and the chunker pulls only enough rows to fill one batch, so peak
memory is a function of the chunk size and not of the feed. The measured load is
1.62M rows in 16.6 seconds.

Chunking rather than handing the whole generator to one COPY call bounds memory
explicitly rather than depending on the driver's internal buffering, and gives a
natural place to report progress per table.

**Rejected.** `executemany`, which is one round trip per row and would take
minutes rather than seconds at this size. COPY of the raw CSV into a staging table
followed by a SQL transform, which is fast and keeps the work in the database, but
puts the time and unit conversions in SQL where they cannot be unit tested. Those
two conversions are the only real logic in the loader and they carry the
consequences described in ADR-0025 and ADR-0026, so they belong in tested Python
functions.

**At scale.** One array of records per chunk means the parameter payload is bounded
by chunk size, so a much larger feed needs no change. If load time ever matters,
the next move is dropping the secondary indexes before the load and rebuilding
them after, which is a bigger change than it sounds because it must stay inside the
one transaction.

---

## ADR-0029: A feed version loads in a single transaction

**Status:** Accepted

**Decision.** The `feed_versions` insert, every table's COPY, the `service_dates`
expansion, and the final `loaded_at` update all happen in one transaction.

**Why.** A partially loaded schedule is worse than no schedule, because it looks
like data rather than like a failure. Trips present without their stop times would
make Phase 3 silently skip trips, and the symptom would appear as missing arrivals
rather than as a loader error. All or nothing removes that state from existing.

It also makes retries free. A failed load leaves the previous version in place and
untouched, so the scheduled job can simply try again next week, and a malformed
feed from MTS costs nothing but a `gtfs_load_log` row.

**Consequences.** `loaded_at` is set inside the same transaction, so it is never
observably null: a crash leaves no row at all rather than a row marked unloaded.
That is stronger than the half loaded detection originally planned, and the
column's remaining value is recording when the load completed. This differs from
the Phase 2 plan, which expected to observe a null `loaded_at` after a kill, and
the plan was wrong rather than the implementation.

**At scale.** One transaction inserting 1.62M rows holds a snapshot and generates
WAL for its duration, measured at under 17 seconds. That is acceptable weekly. If
a feed ever grew large enough that the transaction duration interfered with
autovacuum or replication, loading into per version partitions and attaching them
would be the way out.

---

## ADR-0030: Only the GTFS files the metric needs are loaded

**Status:** Accepted

**Decision.** `agency`, `routes`, `stops`, `trips`, `stop_times`, `shapes`,
`calendar`, and `calendar_dates` are loaded. `fare_attributes`, `fare_rules`,
`fare_media`, `fare_products`, `fare_leg_rules`, `fare_transfer_rules`,
`fare_capping`, `rider_categories`, `transfers`, `networks`, and
`route_networks` are ignored.

**Why.** None of them affect when a bus arrives, which is the only question this
project answers. Loading them would add tables to migrate, map, and test for no
movement toward the metric.

**Rejected.** Loading the whole archive for completeness. There is a real argument
for it, that the data is already downloaded and a future feature might want fares,
but that is speculative and the cost is paid now.

**At scale.** Adding one later is a migration plus a `TableSpec` entry, because the
loader is driven by a table of specs rather than by hand written code per file.
`transfers.txt` is the most likely future addition, since transfer time matters for
trip planning, though not for arrival prediction.

---

## ADR-0031: The schedule refresh is a separate scheduled agent, not part of the collector

**Status:** Accepted

**Decision.** A second launchd agent runs the loader on a weekly schedule, with
its own `gtfs_load_log` table, separate from the collector process.

**Why.** The collector is the process that must never stop, because the realtime
data it captures cannot be recovered later. The loader runs once a week, handles an
8 MB download, and holds a long transaction. Putting that work inside the collector
would mean a loader bug, a malformed feed from MTS, or a long running transaction
could disturb collection, and there is no upside to the coupling: the two have
nothing in common except the database.

Separate logs for the same reason as ADR-0018. A loader that quietly stopped
running looks exactly like a feed that never changed, unless the skipped runs are
recorded as well as the successful ones.

**Rejected.** A periodic task inside the collector, which shares a failure domain
for no benefit. A manual command only, which is simplest and was the original
recommendation, but leaves the schedule silently stale as soon as anyone forgets.
Cron, which has no supervision and no logging story.

**At scale.** Two unattended agents on a development laptop is the real cost here,
and both inherit the sleep gap problem from ADR-0015. A missed weekly run matters
far less than a missed poll, since the schedule changes monthly at most, and
launchd fires a missed calendar interval once the machine wakes.

---

## ADR-0032: Phase 2 is a pure loader and changes nothing about realtime interpretation

**Status:** Accepted

**Decision.** Phase 2 downloads, parses, and loads. It does not change the
collector, and it does not reinterpret any already collected row.

**Why.** Loading the schedule makes two documented gaps fixable, both from
ADR-0021: a trip's service day can now be resolved properly instead of inferred
from the observation time, and a `stop_id` with no `stop_sequence` can now be
looked up in `stop_times`. Both are tempting to fix here and both are the wrong
thing to do in this phase.

They are not mechanical lookups, they are judgment calls about real feed behaviour
that cannot be made until the API key arrives and the actual trip update shape is
known. They also belong to arrival inference and evaluation, which are Phases 3 and
4, and which Andrew writes himself. Doing them here would mean guessing now and
constraining those phases to the guess.

**Rejected.** Wiring the lookups into the collector's parse path immediately, which
would reduce dropped rows from the moment the key arrives, at the cost of deciding
inference semantics inside a loader PR.

**Outcome.** The real feed always omits `stop_sequence`, so the second case is
what happened: Phase 2 is a hard prerequisite, and the repair became urgent rather
than deferred. It is implemented in ADR-0033. The service day repair is still
deferred to Phase 3 as originally intended.

---

## ADR-0033: stop_sequence is recovered by aligning the feed's stop order against the schedule

**Status:** Accepted

**Decision.** The real MTS trip update feed never sends `stop_sequence`. It is
recovered at ingest by walking the feed's ordered `stop_id` list against that
trip's scheduled `stop_times` in order, never going backwards, and taking the next
matching stop at or after the current position. Rows that cannot be aligned are
dropped and counted by reason.

**Why.** This was measured against the real feed the day the API key arrived, and
the measurement inverted an assumption the project had been carrying.

CLAUDE.md recorded an unverified assumption that the OneBusAway feed might send a
single `stop_time_update` with a `delay`. It does not: it sends per stop arrival
times, averaging 17.1 stop time updates per trip across 452 trips. That part was
good news and confirmed the `predictions` schema.

The bad news was that `stop_sequence` appears zero times in the entire feed, and it
is part of the `predictions` primary key. Running the existing parser against the
real feed extracted **0 rows and dropped 7,751**, every one for `no_stop_sequence`.
Every prediction MTS publishes was being discarded. This is the risk ADR-0021
flagged and ADR-0032 said would determine whether Phase 2 was merely useful or a
hard prerequisite. It is a hard prerequisite.

`stop_id` alone cannot substitute for the sequence. Measured against the loaded
schedule, **3,350 trips (7.2%) call at the same stop twice**: trip 19261672 calls
at stop 94031 at both sequence 1 and sequence 4. Keying on `stop_id` would merge
two genuinely different arrivals into one row, and Phase 4 would then compare a
prediction against the wrong arrival, silently.

Ordered alignment resolves that, because the feed lists a trip's remaining stops in
order. Verified on live data: trip 19585343 resolved stop 91039 to sequences 1 and
53, and trip 19591500 resolved stop 98088 to 47 and 51. Every one of 8,243 stored
predictions matched the schedule exactly on `(trip_id, stop_sequence, stop_id)`,
with zero mismatches.

**Rejected.** A plain `stop_id` to `stop_sequence` lookup, which is simpler but
ambiguous for those 7.2% of trips. Re-keying `predictions` on `stop_id`, which
needs no schedule at ingest but collapses repeated visits and corrupts the Phase 4
comparison for the affected trips. Inferring the sequence from the position within
the feed's list, which is wrong the moment a trip is under way, because the feed
only carries remaining stops.

**Consequences.** Prediction ingest now depends on a loaded schedule, so Phase 2 is
a prerequisite for collecting anything from the trip updates feed. When no schedule
covers the service day, rows are dropped as `no_schedule_loaded` rather than
silently, because that is an operator problem.

A per trip lookup is cached in memory. The real feed repeats the same 450 or so
trips every 30 seconds, so without the cache this would be 450 queries per poll for
data that does not change within a service day. Measured at one lookup per trip per
poll cycle, served from cache thereafter.

The active `feed_version` is resolved once per process. A schedule loaded by the
weekly agent is therefore not picked up until the collector restarts. That is
accepted: schedules change monthly at most, and re-querying every poll to catch a
monthly event is the wrong trade.

**At scale.** 93.8% of live predictions resolve. Of the 6.2% that do not, 487 of
500 are stops the realtime feed reports which the static schedule does not list for
that trip at all, and only 13 are ordering failures. That residue is a disagreement
between MTS's own realtime and static data, not something this alignment can fix,
and those stops will simply have no baseline in Phase 4. It is worth re-measuring
after each schedule reload, since a fresher schedule may agree more closely.

The alignment also assumes the feed's stop list is a subsequence of the scheduled
list. A genuinely re-routed trip breaks that assumption, and the 13 out of order
cases are most likely exactly that.
