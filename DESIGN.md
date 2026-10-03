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

**Measured, and worse than expected.** Over the first three days of real
collection, coverage was 64.4%, then 37.4%, then 35.3%, losing about 15 hours of
irreplaceable data per day. The `pmset` log showed the cause: the machine was on
battery and cycling through Maintenance Sleep. Recording coverage in `poll_log`
is what made this visible at all, and it only became visible after the gap query
was corrected to measure successful polls rather than attempts.

**Mitigation.** The launchd agent now wraps the collector in `caffeinate -si`, so
the process holds both `PreventUserIdleSystemSleep` and `PreventSystemSleep` for
its entire life rather than depending on someone running `caffeinate` by hand.
`-i` applies on battery; `-s` is honoured only on AC power. Display sleep is
deliberately left alone, since the screen is irrelevant to collection.

This is a mitigation and not a fix. Closing the lid still sleeps the machine and
no user space assertion can override that, so the laptop must stay open and
plugged in to achieve full coverage. An always on host remains the real answer,
and these numbers are the argument for doing it before Phase 5 rather than after.

**At scale.** Gaps bias any naive analysis of coverage, so a model trained on this
data must account for which hours are actually represented. Reinstalling the
agents is itself a risk: `launchctl bootout` returns before the job is unloaded,
and bootstrapping into that window fails and leaves the collector down. The
install script now waits for the old job to disappear, retries once, and verifies
registration before reporting success, because a silent failure there stops
collection entirely.

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

---

## ADR-0034: The mock defaults to the shape the real feed actually sends

**Status:** Accepted

**Decision.** `MOCK_FEED_SHAPE` selects the mock's output. The default, `mts`,
emits only what the real MTS feed was measured to send. `rich` populates every
optional field, keeping the parser's fallback paths under test. The `single_delay`
style is deleted.

**Why.** The mock was more generous than reality, and that cost real damage twice.

It emitted `stop_sequence` on every `stop_time_update`. The real feed never sends
it. So the collector passed every test while dropping 100% of real predictions,
and `poll_log` showed healthy `ok` polls throughout. The failure was invisible
until the parser was run against the real feed by hand.

It also modelled prediction drift on a 120 second cycle, which made change-only
storage look about 99% effective. Measured against the real feed it is about 78%,
which is the difference between the "thousands of rows per day" claimed in
ADR-0005 and the roughly 3.3 million per day actually observed.

Both mistakes share one cause: a mock written from the specification rather than
from the feed, used as evidence about the feed. The default now mirrors
measurement. Specifically absent in `mts` shape, because the real feed omits them:
`stop_sequence`, `start_date`, `schedule_relationship`, `bearing`, `speed`,
`current_stop_sequence`, `current_status`, and `occupancy_status`. `delay` appears
on about 0.4% of stop time updates, matching the 30 of 7,750 observed.

**Rejected.** Replacing the output with the real shape only, deleting the richer
variant. Simpler and more honest about MTS, but it drops coverage of the parser's
optional field handling, which a conforming feed may exercise and which already
contains real fallback logic. Keeping both, with the realistic one as the default,
costs one branch.

Also rejected: leaving the mock alone now that the real feed is available. The mock
is what runs in CI, where no API key exists, so it remains the thing most tests
actually exercise.

**Consequences.** Predictions from the default shape now require a loaded schedule
to resolve, exactly as in production. Three collector tests that assert predictions
land therefore use `rich`, because the simulator's synthetic trip ids do not appear
in any GTFS fixture. The resolution path itself is tested against a real loaded
schedule in `tests/test_trip_stops.py`.

**Known gap.** The mock describes a world the GTFS fixture does not: its route,
trips, and stops share no identifiers with the static fixture, so the full
realtime-plus-schedule pipeline cannot be exercised end to end offline. The fix is
for the simulator to emit a matching static GTFS archive from its own route. That
is worth doing before Phase 4, since Phase 4 joins the two together and would
otherwise be testable only against live data.

---

## ADR-0035: The service day is derived from the schedule, not the observation clock

**Status:** Accepted

**Decision.** For each observation, candidate service days are the local date and
the day before. The instant is expressed as seconds past each candidate's midnight
and the candidate whose scheduled window contains it wins, with grace margins of
15 minutes early and 60 minutes late. The local date is preferred when both fit and
is the fallback when the trip has no schedule.

**Why.** The real MTS feed omits `start_date` on every entity, so it has to be
derived, and trip identity in GTFS-Realtime is `(start_date, trip_id)`. The
original fallback used the observation date, which is correct for the great
majority of pings and silently wrong for every trip that crosses midnight.

Measured: trip 19627988 is scheduled 23:35 to 24:01. Its pings after midnight were
filed under the following service day, together with the next night's run of the
same trip id, producing a single trip record spanning 23.8 hours. 199 trip records
were affected, covering 18,918 pings. Arrival inference cannot do anything sensible
with a track that appears to jump a day, so this had to be fixed before Phase 3
rather than worked around inside it.

The margins are asymmetric because buses run late far more readily than early.

**Rejected.** Having inference skip trips whose observed window far exceeds their
scheduled duration. Cheap, and it would have hidden fewer than 1% of trips, but it
leaves wrong data in the database for every later phase to rediscover. Also
rejected: using the vehicle's own report time without reference to the schedule,
which is what was already wrong.

**Consequences.** Prediction ingest must resolve the service day before grouping by
trip, since `start_date` is part of both the grouping key and the primary key:
grouping first would split one run across two days.

`vehicle_positions` was backfilled, moving 11,352 rows and reducing implausible
trip records from 210 to 67. That column is not part of its primary key so the
update cannot collide. `predictions.start_date` **is** part of its primary key, so
a backfill there risks conflicts and was deliberately not done.

**At scale.** The 67 remaining implausible records are buses running more than an
hour behind, or still reporting a finished trip while sitting at a layover: median
61 minutes past the scheduled end. Those correctly belong to the day they started.
Daylight saving means a service day can be 23 or 25 hours long, which this handles
because the comparison is against the candidate's own midnight; both transitions
are covered by tests.

---

## ADR-0036: Arrivals are reconstructed by projecting GPS onto the route shape

**Status:** Accepted

**Decision.** Arrival inference runs in four stages, each a pure function: project
each ping onto the route shape to get a distance along the route, build a
monotonic distance versus time track, find the ping pair bracketing each stop and
interpolate the crossing time, and detect dwell where a vehicle was seen stationary
at a stop.

**Why this approach.** It is the only one the data supports. The real feed sends a
vehicle id, a trip id, a coordinate and a timestamp. There is no speed, no bearing,
and no `current_stop_sequence` or `current_status`, so there is no shortcut such as
"the feed says it is approaching stop 7". Geometry is all that is left.

What makes it tractable is that MTS populates `shape_dist_traveled` on both shapes
and stop times, so every stop already has a known distance along its route. The
hard geometric problem reduces to projecting a point onto a polyline and comparing
one number against another.

**Key choices, and why.**

*Forward bounded search rather than global nearest point.* A loop route passes the
same coordinate twice, so a global search would snap a late fix back to the earlier
pass. The search window runs from slightly behind the previous fix to as far ahead
as the elapsed time allows at 35 m/s. This also bounds the cost: a global search
would be 40 pings times 2,000 shape points per trip.

*Distance is clamped non-decreasing.* GPS noise makes the raw projection wobble
backwards, and a wobble would otherwise produce an arrival earlier than the one
before it. Clamping is counted rather than silent, so noise stays measurable:
18,980 clamps over one day of real data.

*Fixes more than 150 m off route are dropped.* A detour, a deadheading vehicle, or
a bad fix. Snapping it would invent a position on a route the vehicle was not on.
12,397 pings dropped over one day.

*Stops with no bracketing pair produce no arrival.* Nothing is extrapolated. With
ping gaps reaching 843 seconds at p99, extrapolation would manufacture
plausible-looking times that are badly wrong, and Phase 5 would train on them. A
missing label costs one row; a wrong label corrupts the model. 40,884 stops were
skipped over one day, against 176,781 arrivals produced.

*Dwell takes precedence over interpolation.* A vehicle waiting at a stop reports
several fixes in one place, and interpolating across that cluster would place the
arrival in the middle of the wait rather than at its start. The start is what a
rider experiences as the arrival. 16,776 of 176,781 arrivals were dwell detected.

**Validated on real data,** one service day, 7,142 trips in 25 seconds:

- **0 monotonicity violations** across 176,781 arrivals, so no projection ran
  backwards.
- Implied segment speeds: median 5.8 m/s, p95 12.6 m/s, which is right for urban
  buses including stops. 91 of 169,879 segments implausible.
- Delay against schedule: p10 -1.3 min, median +1.3 min, p90 +5.9 min. A credible
  transit lateness distribution rather than something symmetric or wild.
- **Cross-checked against MTS.** For 170,961 arrivals, comparing against MTS's own
  final prediction for the same stop gives a **median absolute difference of 35
  seconds**, p90 137 seconds. Two independent estimates of the same event agreeing
  that closely is the strongest evidence available that the inference is sound.

**Important caveat on that last figure.** 35 seconds is a measure of *this
inference's* credibility, not of MTS's accuracy. It compares against MTS's final
prediction, made moments before the arrival, which is trivially easy to get right.
The project metric is error at 1, 5, 10 and 20 minutes out, which is Phase 4 and
will be far larger.

**At scale.** 1% of cross-checked arrivals disagree with MTS by more than 10
minutes, and the mean signed difference of 302 seconds is driven by that tail
rather than by any central bias. Those cases are worth examining before Phase 5
trains on them, and `ping_gap_seconds` is the first thing to filter on.

A trip served by two vehicles is a mid route swap, and mixing two buses' GPS would
produce a track that teleports. The vehicle with more fixes is taken as the one
that ran it and the other is skipped: 32 over one day.

---

## ADR-0037: How MTS prediction error is defined and measured

**Status:** Accepted

**Decision.** For an arrival that actually happened at `A` and a horizon `h`, the
prediction scored is the most recent `predictions` row for
`(start_date, trip_id, stop_sequence)` whose `observed_at` falls in
`[A - h - 2 hours, A - h]`. Error is `predicted arrival minus A`, signed, with the
absolute value stored alongside. The headline figure uses only arrivals that have a
prediction at every horizon, and only arrivals whose Phase 3 ping gap was 3 minutes
or less.

**The horizon is measured back from the actual arrival, not the predicted one.**
"Twenty minutes before the bus really came, what was MTS saying" is what a rider
experiences. Measuring back from the predicted arrival is self-referential: the
worse a prediction, the further the evaluation point drifts from the real event, so
bad predictions would be graded at a more forgiving moment.

**The most recent prediction at or before the cutoff is the one in force.** Because
predictions are stored change-only (ADR-0005), that row may have been written much
earlier, and no row in between means MTS did not change its mind. This is the
lookup the Phase 2 tests already proved runs as a backward scan of the
`predictions` primary key.

**A prediction more than two hours stale is rejected.** Legitimate pairs have a
median prediction age of 4.1 minutes and p95 of 23.4 minutes, so this excludes
nothing real. It exists because a prediction MTS has not revised in hours is a
leftover rather than a live estimate, and because of the bug below.

**Horizons are compared on a common subset.** Availability falls from 94.7% at one
minute to 58.4% at twenty, so scoring each horizon over whatever it has would
compare different populations and make the horizon trend meaningless. A second
table reports all available pairs, which answers "how wrong is MTS overall" rather
than "how does error grow with lead time".

**The headline filters to well observed arrivals.** 24.6% of Phase 3 arrivals were
interpolated across ping gaps over 10 minutes and carry real uncertainty of their
own. Including them measures this project's GPS coverage as if it were MTS's error.

### The result

Comparable subset, well observed arrivals, N = 198,878 per horizon, five service
days:

| Horizon | MAE | Median | p90 | Bias |
| --- | --- | --- | --- | --- |
| 1 min | **0.90 min** | 0.62 | 1.65 | -0.12 |
| 5 min | **1.36 min** | 1.02 | 2.80 | -0.24 |
| 10 min | **1.70 min** | 1.27 | 3.60 | -0.45 |
| 20 min | **2.23 min** | 1.62 | 4.85 | -0.94 |

This is the baseline Phase 5 has to beat.

### A bug worth recording, because of how it presented

The first run produced an MAE of 6.45 minutes at the one minute horizon, with a p90
of 1.70 minutes. **A mean above the 90th percentile is structurally impossible for a
well behaved distribution**, and that inconsistency is what exposed the fault
rather than any individual number looking wrong. Had the mean been merely high it
would have been easy to accept.

The cause was a residual service-day misalignment. Phase 3 backfilled
`vehicle_positions.start_date` but deliberately skipped `predictions.start_date`,
on the grounds that it is part of that table's primary key and a moved row might
collide. So for the 88 trips scheduled entirely past midnight, arrivals sat on the
corrected service day while predictions still sat on the wrong one, and the join
matched the previous night's run of the same trip id. 808 pairs picked up errors of
roughly 24 hours, which destroyed the mean while leaving the median untouched.

The deferred caution turned out to be unfounded: 44,870 rows needed moving and
**zero would have collided**, because `observed_at` is also in the key and the two
runs are 24 hours apart. Had the backfill been done when the rest was, this would
never have arisen. The lesson is that deferring a fix on an unmeasured risk is
itself a risk, and measuring it took one query.

### What the data says beyond the headline

- **Error grows with lead time**, roughly 0.9 to 2.2 minutes from one to twenty
  minutes out. That is the shape the project predicted and is the room a model has
  to work in.
- **MTS runs slightly optimistic, increasingly so with horizon**: bias moves from
  -0.12 to -0.94 minutes, meaning it predicts arrivals a little earlier than they
  happen. A consistent bias is the cheapest thing for a model to correct.
- **Rail is predicted far better than road.** The best routes are 530 (Green Line,
  0.90 min) and 510 (Blue Line, 1.02 min), both trolley lines. The worst is 894
  (Morena/Campo to El Cajon, 4.48 min), a long rural bus route. Trolleys do not sit
  in traffic.
- **The afternoon peak is the hardest window**, 1.89 minutes MAE for 15:00 to
  18:00, against 1.30 minutes after midnight.
- **Label noise is material at short horizons and not at long ones.** At one minute,
  MAE is 0.90 on tight labels and 1.95 on poor ones; at twenty minutes it is 2.23
  against 2.73. Close in, this project's own GPS gaps dominate the measurement;
  further out, MTS's error does. That is an argument for improving collection
  coverage before trusting any short horizon result.

**At scale, and what this number is not.** Five service days at 35 to 70%
collection coverage. The method is sound and the figures are real, but they are a
preliminary sample, not a published statistic. Routes with few observed trips are
noisy, which is why the per route tables require at least 200 pairs. There is **no
weekend data at all**, because collection began on a Monday, so the weekday versus
weekend split the charter asks for cannot yet be computed. Re-running over several
weeks of good coverage is the first thing to do before quoting these numbers
anywhere.

---

## ADR-0038: Three predictors, one table, and the leak that nearly produced a false result

**Status:** Accepted

**Decision.** Three predictors are scored against MTS by writing into
`prediction_errors` with a `source` column, so the head-to-head is one `GROUP BY`
over one population scored by one definition:

- `persist_delay`: scheduled arrival plus however late the vehicle already is
- `segment_mean`: anchor arrival plus the historical mean travel time of each
  segment ahead, the charter's specified baseline
- `lgbm`: gradient boosting on features known at the cutoff, L1 objective to match
  the metric being reported

All three forecast from the same **anchor**, the last stop whose arrival was
knowable at the cutoff. The split is time-based: train on 2026-09-28 to 09-30, test
on 10-01 to 10-02.

`persist_delay` exists because it is the honest obvious thing. A gradient boosted
model that beats MTS but loses to "assume it stays as late as it is now" has
demonstrated nothing, and a single headline number would hide that.

### The leak, which is the most important thing in this record

The first honest-looking run said the model beat MTS at every horizon: **0.63
against 0.93 minutes at one minute out, a 32% reduction.** That is a suspicious
result, because at one minute out MTS can see the vehicle in real time while this
model sees only the previous stop. So it was tested rather than published.

The anchor was being selected on `arrived_at <= cutoff`. But an arrival time is
**inferred**, by interpolating between the two pings that bracket the stop (Phase
3, ADR-0036), so it only becomes knowable once the *later* ping has been received.
Selecting on `arrived_at` alone let a prediction use a timestamp that was itself
computed from GPS received after the cutoff.

Restricting to anchors whose interpolation window had closed before the cutoff
changed the picture completely:

| Horizon | MTS | lgbm, loose anchor | MTS | lgbm, strict anchor |
| --- | --- | --- | --- | --- |
| 1 min | 0.93 | **0.63** | 0.84 | 0.79 |
| 5 min | 1.39 | **1.18** | 1.32 | 1.34 |
| 10 min | 1.71 | **1.58** | 1.63 | 1.74 |
| 20 min | 2.19 | **2.20** | 2.16 | 2.43 |

The apparent win was the leak. The anchor condition is now
`arrived_at + ping_gap_seconds <= cutoff`, enforced in the one query that selects
anchors, with a test that builds an anchor whose window crosses the cutoff and
asserts it is refused.

**The general lesson.** The leak was not a careless mistake like feeding the model
its own label. It came from the labels being *derived* rather than observed, which
made "available at time T" a subtler question than it looks. Any pipeline that
infers its own ground truth has this hazard. The only reason it was caught is that
the result was too good for the information the model had, and that was treated as
evidence rather than success.

### The honest result

Test window only, comparable subset, well observed arrivals, N = 50,591 per
horizon. MAE in minutes:

| Horizon | MTS | persist_delay | segment_mean | **lgbm** |
| --- | --- | --- | --- | --- |
| 1 min | 0.93 | 1.23 | 0.96 | **0.89** |
| 5 min | 1.40 | 2.00 | 1.58 | **1.38** |
| 10 min | 1.77 | 2.77 | 2.10 | **1.73** |
| 20 min | 2.31 | 4.35 | 2.98 | **2.31** |

p90, where the model does rather better:

| Horizon | MTS | **lgbm** |
| --- | --- | --- |
| 5 min | 2.85 | **2.75** |
| 10 min | 3.78 | **3.55** |
| 20 min | 5.08 | **4.84** |

**This is parity, not a win.** The MAE improvements of 2 to 4% on five days of data
are inside the noise, and the project should not claim to beat MTS on this
evidence. The p90 improvement at longer horizons is the more interesting signal,
since it suggests the model handles the bad cases somewhat better.

### What the baselines show

- **`persist_delay` degrades fast with horizon**, 1.23 to 4.35 minutes. Current
  lateness is strong information for the next stop and decays quickly, which is
  exactly the behaviour expected and a useful sanity check on the anchor logic.
- **`segment_mean` is worse than MTS at every horizon.** It throws away the
  vehicle's current lateness entirely, which the comparison with `persist_delay` at
  one minute makes obvious. The charter specified it as the baseline; the data says
  it is the weaker of the two simple approaches except at long horizons.
- **`lgbm` beats both baselines at every horizon**, which is the minimum bar for the
  model being worth having at all.
- Bias: MTS -0.46, lgbm -0.34 minutes. Both predict slightly early; the model
  slightly less so.

**At scale.** Five service days at 35 to 70% collection coverage, and 1.8M training
rows drawn from only three of those days. The pipeline reruns with
`make model && make compare`, so the number improves as the collector keeps going,
which is the real deliverable here rather than today's figure. `model_runs` records
the window, features and parameters behind every run so a quoted number is
traceable. There is still no weekend data, so the weekday split remains uncomputable.

## ADR-0039: Viewer settings, and the cache key invariant they exposed

**Status:** Accepted

**Decision.** The constants that governed every figure are exposed to the viewer
in two groups. The prediction group carries the decisions this project argues
about: which horizon's measured bias drives the live correction, how many
observed arrivals are required before correcting at all, which arrivals count as
well observed, and which predictor sits beside MTS. The map group is
presentation only and changes no number.

Settings live in the viewer's browser, not on the server. They are a property of
who is looking, not of the deployment, and a server-side store would mean one
visitor's experiment changed what the next visitor saw.

Defaults are exactly the constants the API applies when no parameter is sent, so
a fresh viewer, a reset viewer, and the figures quoted in the README and in
ADR-0038 all agree.

### The bug this uncovered, which is the real content of this record

Three endpoints cached their aggregates under keys containing none of their
parameters:

```
cache.get("headline", ...)    cache.get("bias", ...)    cache.get("quality", ...)
```

With no parameters, that was correct. The moment any of them took a setting it
became a silent lie: changing the setting returned the *previous* setting's
numbers for up to the sixty second TTL, with nothing on screen to indicate it.
The failure is invisible by construction, because a plausible number appears and
the page looks like it responded.

This was confirmed rather than assumed. With the bare key restored, a request at
a 15 minute label filter returns the 3 minute filter's body verbatim, including
its `label_filter_seconds: 180` field, which is the tell.

**The invariant:** every cache key contains every parameter that changes the
result. A test asserts two different parameter values never share an entry, and
it fails if a key is ever shortened again.

**At scale.** The cache is per process and not shared between workers, so a
second API process would simply hold its own copy. The keys are low cardinality
because every parameter is drawn from a fixed choice list, which is also why the
choice lists are validated rather than free numeric input: an unbounded
`max_ping_gap` would let a visitor mint unbounded cache entries.

### An unknown predictor is rejected, not silently empty

`compare` is validated against the four known sources and refused with a 422.
Returning an empty column instead would read on screen as "the model has no data
here", which is a very different claim from "you asked for a predictor that does
not exist".

### Honesty markers

Exposing these knobs is only defensible if the page says when one has been moved
somewhere flattering. Two settings can improve the numbers while meaning less:

- **loosening the label filter** admits arrivals whose true time is barely known,
  which charges this project's own GPS interpolation error to every predictor,
  including MTS
- **lowering the minimum sample** fits a correction to a handful of observations

Either one raises a caution in the settings panel, on the evidence page and in
the stop arrivals panel, naming the specific reason. Any change at all shows a
banner and marks the settings control, so a screenshot of a configured app
cannot be mistaken for a screenshot of the project's actual result.

The point of exposing these is to let someone interrogate the result, not to let
the result be configured into looking good.

### Rejected alternatives

- **Leaving the constants hardcoded.** Simplest, and the honest figures were
  already published. Rejected because the interesting question about this project
  is how sensitive the result is to those choices, and the only convincing answer
  is to let someone move them and watch.
- **Server-side settings.** Rejected above: per viewer, not per deployment.
- **A shorter cache TTL instead of keyed entries.** Rejected because it narrows
  the window in which the page lies rather than closing it, and the lie is silent.
- **Free numeric inputs instead of choice lists.** Rejected for cache cardinality
  and because the offered values are the ones that mean something.
- **Two horizon controls**, one in settings and the evidence page's own selector.
  Rejected as two controls for one concept, which would diverge; the page now
  reads its horizon from settings and links to the panel.

### Verified

Against the live database rather than by inspection. Tightening the label filter
from 15 to 3 minutes moves MTS from 1.87 to 1.77 minutes MAE as the sample falls
from 79,188 to 50,591, because it stops scoring arrivals this project only knows
roughly. Raising the minimum sample to 2000 takes every correction to `none`.
At a one minute horizon the corrections shrink to a few seconds, which is the
expected shape: short horizons are easy, so there is little bias to correct.

## ADR-0040: A watchdog, because the outage that happened was silent

**Status:** Accepted

**Context.** Collection stopped for nine hours and nothing reported it. Docker
Desktop was not running, so Postgres was unreachable, so the collector exited at
startup and launchd restarted it every thirty seconds into the same failure,
writing 2.5 MB of identical tracebacks to stderr. The supervision worked exactly
as designed. The problem was that the design had no way to say "I have been
failing since 21:50".

This matters more here than in most systems, because the input is a live feed.
A web service that is down for nine hours serves errors and then recovers; a
collector that is down for nine hours loses nine hours of data that no retry can
ever recover.

**Decision.** Three changes, in increasing order of how much they actually fix.

1. **Docker Desktop `AutoStart` set to `true`.** It was `false`, and Docker was
   not a login item, so the database never returned after a reboot. With the
   container's existing `restart: unless-stopped`, the whole chain now recovers
   unattended: Docker starts at login, Postgres starts with it, and the
   collector's `KeepAlive` reconnects. This is the direct fix for the outage that
   occurred.

2. **A watchdog agent**, `sd.ontime.watchdog`, running every five minutes. It
   probes `/healthz` and posts a macOS notification when collection stops and
   again when it recovers.

   It deliberately does **not** restart anything. launchd already restarts the
   collector, and a second thing restarting it would race with the first. Its
   only job is to convert a silent failure into a visible one.

   It alerts only after two consecutive failed probes. A single failure is not an
   outage: the collector is restarted on reinstall and on crash, and `/healthz`
   is briefly unanswered each time. This was not theoretical, the first version
   fired a false alarm against its own installation. Two probes at a five minute
   interval means roughly ten minutes down before anyone is told, which is well
   inside the tolerance for a feed that publishes every thirty seconds.

3. **System sleep on AC.** The machine slept after one minute idle on AC as well
   as battery. The collector holds a `caffeinate` assertion for its lifetime
   (ADR-0015), but only for its lifetime, so a database outage drops the
   assertion, the machine sleeps a minute later, and nothing retries until
   someone opens the lid. A failure that puts the machine to sleep is a failure
   that cannot self heal. This requires `sudo pmset -c sleep 0` and is left to
   the operator rather than changed by the project.

**Rejected alternatives.**

- **Making the watchdog restart things.** Rejected above: it would race launchd,
  and auto remediation that papers over a fault makes the fault harder to see.
  The one exception considered was starting Docker when the engine is down, which
  `AutoStart` now covers more reliably and at the right moment.
- **Email or push alerts.** Rejected for now as infrastructure the project does
  not otherwise need. A local notification is enough on the machine the collector
  runs on, and Phase 7 replaces this with CloudWatch alarms once collection moves
  off the laptop.
- **Having the collector retry instead of exiting when the database is
  unreachable.** Tempting, and it would have kept the process alive and the
  `caffeinate` assertion with it. Rejected because launchd's restart is the
  simpler supervisor and the exit is honest; the real problem was not the exit
  but that nobody was told. Worth revisiting if the spool-to-disk idea below is
  ever built.

**What this does not fix.** Closing the lid still sleeps the machine, and no
user space assertion can override it. A laptop that travels cannot be a 24/7
collector, so these changes raise the ceiling rather than reach it. The real fix
is Phase 7: move collection to an always on host. A complementary idea, not
built, is to have the collector spool to local disk when Postgres is unreachable
and replay on reconnect, which would have saved the nine hours outright.

**At scale.** The watchdog is a five minute shell probe with no dependencies
beyond curl and osascript, and costs nothing. Its state file holds three fields
so that notifications fire on transitions rather than on every run, which is the
difference between an alert and a nuisance that gets muted.

## ADR-0041: The collector moves to AWS, with Postgres on the same box

**Status:** Accepted

**Context.** ADR-0040 raised the laptop's collection ceiling but could not reach
it. Closing the lid sleeps the machine and no user space assertion overrides
that, so coverage stays bounded by how often the laptop is open and docked.
Measured coverage over the first five service days was 35 to 67%. The model needs
weeks of continuous history, and the headline metric is computed over whatever
was collected, so coverage is the binding constraint on the whole project rather
than an operational inconvenience.

**Decision.** One `t4g.small` EC2 instance in `us-west-2`, running the collector
under systemd and Postgres in Docker on the same host, provisioned by Terraform
in `infra/`.

### Postgres on the instance rather than RDS

RDS is the production shape and gives automated backups and point in time
recovery for free. It was rejected for this project at this size:

- It is roughly 50% more per month, about $29 against $23, for a database with
  one writer and no availability requirement beyond "do not lose the data".
- The collector writes continuously and would pay a network hop per write
  instead of talking to localhost.
- The backup story is replaceable: a DLM policy takes daily snapshots of the
  data volume and keeps seven, which is the recovery objective this project
  actually has.

This is a defensible trade rather than the obviously correct one, and the thing
to be able to say about it is what would change the answer: a second writer, a
real availability requirement, or anyone else depending on the data.

### The data volume is separate from the instance

The database lives on its own encrypted EBS volume with `prevent_destroy`, not on
the root volume. The instance is disposable, and `user_data_replace_on_change`
means editing the bootstrap rebuilds it. The data is not disposable, because it
cannot be recollected. Keeping those two on separate volumes is what makes
"rebuild the box" a safe operation.

### The API key never enters Terraform state

The SSM parameter is created by hand and read through a data source rather than
managed as a resource. A `aws_ssm_parameter` resource would put the secret in
plaintext into `terraform.tfstate`, which is a file on the laptop and would then
have to be treated as a secret itself. The instance reads it at boot through its
IAM role, and the bootstrap script turns off shell tracing around the only line
that holds it.

### Alerting moves off the host

The local watchdog from ADR-0040 is not copied across. A watchdog running on the
host it watches cannot report that the host is gone. Instead a systemd timer
publishes seconds since the last successful poll to CloudWatch every minute, and
the alarm lives in CloudWatch where it survives the instance. Missing data is
treated as breaching rather than unknown, because a metric that stops arriving is
exactly what a dead collector looks like.

### Both collectors run during the cutover

The migration script merges rather than restores. Stopping the laptop first would
leave a gap, and a gap is permanent, so the cloud collector starts on an empty
database and the laptop's history is merged in underneath it afterwards. Every
table moved has a natural primary key, so the merge is `on conflict do nothing`
and is safe to run more than once.

Only the irreplaceable tables move: `feed_versions`, `poll_log`,
`vehicle_positions` and `predictions`. `prediction_errors`, `arrivals` and
`segment_stats` are recomputed on the far side, and the static GTFS tables are
re-downloaded, which avoids shipping 1.6 GB that can be regenerated.

**Rejected alternatives.**

- **A cheap VPS**, Hetzner or DigitalOcean, at a third of the price. Rejected
  because the charter specifies AWS with infrastructure as code, and for a
  portfolio project the deployment target is part of the artifact.
- **ECS or Fargate.** Rejected as the wrong shape: this is one long lived process
  with a local database, not a scalable service, and the container orchestration
  would be ceremony around a single task.
- **Keeping the laptop as a second collector after cutover.** Rejected because
  two writers to two databases produce two partial histories, and the merge only
  makes sense as a one time operation.
- **A NAT gateway and a private subnet.** Rejected on cost: a NAT gateway alone
  costs more per month than the entire rest of this stack, to protect one host
  whose only inbound rule is SSH from a single address.

**What this does not solve.** The API and the map are still local. Nothing here
deploys them, and the map is the thing a visitor actually sees, so Phase 7 is not
finished by this record. Retention is also unaddressed: 100 GB is four to five
months of runway at full coverage, and the alarm on the volume is a warning
rather than a plan.

**At scale.** Full coverage roughly triples the daily write volume relative to
the laptop's measured 35 to 67%, which is the point. Growth is about 0.55 GB a
day of raw capture. The first thing to break is disk, and the first mitigation is
a rollup of `vehicle_positions` older than the training window, since the model
reads segment aggregates rather than individual pings.
