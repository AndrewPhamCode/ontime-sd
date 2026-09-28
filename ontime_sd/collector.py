"""The collector: two independent poll loops that never stop.

Phase 5 needs weeks of history and that history can only be gathered in real
time, so this process is the one that must keep running. Everything here is
shaped by that: per feed isolation, jittered backoff, cadence that does not
drift, and a shutdown that does not lose a poll in flight.

See DESIGN.md ADR-0008 through ADR-0010 for the loop design.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

import asyncpg
import httpx

from ontime_sd import poll_log
from ontime_sd.config import SERVICE_TZ, TRIP_UPDATES, VEHICLE_POSITIONS, Settings
from ontime_sd.db import create_pool
from ontime_sd.feeds import (
    FeedError,
    FeedHTTPError,
    FeedParseError,
    FeedTransportError,
    ParsedFeed,
    extract_positions,
    extract_predictions,
    fetch_feed,
    parse_message,
)
from ontime_sd.logging_setup import configure_logging
from ontime_sd.sinks import PredictionCache, write_positions, write_predictions

log = logging.getLogger(__name__)

# Service days kept in the prediction cache before being pruned. Two is enough
# to cover a trip that began yesterday and is still running past midnight.
CACHE_RETENTION_DAYS = 2


class Backoff:
    """Full jitter exponential backoff.

    Jitter matters because both feeds fail together during a shared outage and
    would otherwise retry in lockstep forever. See ADR-0009.
    """

    def __init__(self, base_seconds: float, max_seconds: float, rng: random.Random | None = None):
        self.base_seconds = base_seconds
        self.max_seconds = max_seconds
        self.failures = 0
        self._rng = rng or random.Random()

    @property
    def ceiling(self) -> float:
        """The current upper bound, before jitter."""
        if self.failures == 0:
            return 0.0
        # 2 ** (failures - 1) so the first failure waits up to base, not 2x base.
        return min(self.base_seconds * (2 ** (self.failures - 1)), self.max_seconds)

    def record_failure(self) -> float:
        self.failures += 1
        return self._rng.uniform(0.0, self.ceiling)

    def reset(self) -> None:
        self.failures = 0


@dataclass(slots=True)
class FeedHealth:
    """What the health endpoint needs to know about one feed."""

    feed: str
    last_success: datetime | None = None
    last_attempt: datetime | None = None
    consecutive_failures: int = 0
    polls: int = 0
    rows_written: int = 0


@dataclass(slots=True)
class CollectorState:
    """Shared, read by the health endpoint. See health.py."""

    feeds: dict[str, FeedHealth] = field(default_factory=dict)
    started_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    def health(self, feed: str) -> FeedHealth:
        return self.feeds.setdefault(feed, FeedHealth(feed=feed))

    def is_stale(self, now: datetime, stale_after_seconds: int) -> bool:
        """True when no feed has succeeded recently enough.

        Deliberately "any feed", not "all feeds": one healthy feed means the
        process, its network, and its database are all working, which is what
        this signal is for. A single feed failing is a data quality problem that
        poll_log surfaces, not a reason to report the process unhealthy and have
        a supervisor kill it.
        """
        successes = [h.last_success for h in self.feeds.values() if h.last_success]
        if not successes:
            return True
        return (now - max(successes)).total_seconds() > stale_after_seconds


class FeedPoller:
    """One feed, one loop, one backoff state."""

    def __init__(
        self,
        feed: str,
        settings: Settings,
        client: httpx.AsyncClient,
        pool: asyncpg.Pool,
        state: CollectorState,
        rng: random.Random | None = None,
    ) -> None:
        self.feed = feed
        self.settings = settings
        self.client = client
        self.pool = pool
        self.state = state
        self.backoff = Backoff(settings.backoff_base_seconds, settings.backoff_max_seconds, rng=rng)
        self.last_feed_timestamp: datetime | None = None
        self.cache = (
            PredictionCache(threshold_seconds=settings.prediction_change_threshold_seconds)
            if feed == TRIP_UPDATES
            else None
        )
        self._last_prune: date | None = None

    # --- one poll ---

    async def poll_once(self) -> poll_log.PollOutcome:
        """Run exactly one poll and record its outcome.

        Never raises for an expected failure: the outcome is the return value, so
        the loop can decide about backoff without exception handling in two
        places.
        """
        started_at = datetime.now(tz=UTC)
        started_monotonic = time.monotonic()
        health = self.state.health(self.feed)
        health.last_attempt = started_at
        health.polls += 1

        url = self.settings.feed_url(self.feed)

        def elapsed_ms() -> int:
            return int((time.monotonic() - started_monotonic) * 1000)

        try:
            payload = await fetch_feed(self.client, url)
            message = parse_message(payload)
        except FeedHTTPError as exc:
            return self._failure(
                started_at, elapsed_ms(), poll_log.STATUS_HTTP_ERROR, exc, http_code=exc.status_code
            )
        except FeedTransportError as exc:
            return self._failure(started_at, elapsed_ms(), poll_log.STATUS_HTTP_ERROR, exc)
        except FeedParseError as exc:
            return self._failure(started_at, elapsed_ms(), poll_log.STATUS_PARSE_ERROR, exc)

        parsed = (
            extract_positions(message)
            if self.feed == VEHICLE_POSITIONS
            else extract_predictions(message)
        )

        # The feed's own freshness statement. Unchanged means there is nothing
        # new to parse or write. See ADR-0007.
        if parsed.feed_timestamp is not None and parsed.feed_timestamp == self.last_feed_timestamp:
            self.backoff.reset()
            health.last_success = started_at
            health.consecutive_failures = 0
            log.info(
                "feed unchanged, skipped",
                extra={
                    "feed": self.feed,
                    "feed_timestamp": parsed.feed_timestamp.isoformat(),
                    "duration_ms": elapsed_ms(),
                },
            )
            return poll_log.PollOutcome(
                feed=self.feed,
                started_at=started_at,
                status=poll_log.STATUS_SKIPPED,
                duration_ms=elapsed_ms(),
                http_code=200,
                feed_timestamp=parsed.feed_timestamp,
                entity_count=parsed.entity_count,
                rows_written=0,
            )

        try:
            rows_written = await self._write(parsed)
        except (asyncpg.PostgresError, OSError) as exc:
            return self._failure(
                started_at, elapsed_ms(), poll_log.STATUS_DB_ERROR, exc, http_code=200
            )

        # Only advance the watermark once the data is durably written, so a
        # failed write is retried on the next poll instead of being skipped.
        self.last_feed_timestamp = parsed.feed_timestamp
        self.backoff.reset()
        health.last_success = started_at
        health.consecutive_failures = 0
        health.rows_written += rows_written

        log.info(
            "poll ok",
            extra={
                "feed": self.feed,
                "entity_count": parsed.entity_count,
                "rows_written": rows_written,
                "dropped": parsed.dropped,
                "drop_reasons": list(parsed.drop_reasons),
                "duration_ms": elapsed_ms(),
                "cache_size": len(self.cache) if self.cache else None,
            },
        )
        return poll_log.PollOutcome(
            feed=self.feed,
            started_at=started_at,
            status=poll_log.STATUS_OK,
            duration_ms=elapsed_ms(),
            http_code=200,
            feed_timestamp=parsed.feed_timestamp,
            entity_count=parsed.entity_count,
            rows_written=rows_written,
        )

    async def _write(self, parsed: ParsedFeed) -> int:
        if self.feed == VEHICLE_POSITIONS:
            async with self.pool.acquire() as conn:
                return await write_positions(conn, parsed.positions)

        assert self.cache is not None
        changed = self.cache.select_changed(parsed.predictions)
        async with self.pool.acquire() as conn:
            written = await write_predictions(conn, changed)

        # Remembered only after the write succeeded. Remembering a row that
        # failed to insert would suppress it from every later poll.
        self.cache.remember(changed)
        self._maybe_prune()
        return written

    def _maybe_prune(self) -> None:
        """Evict closed service days, at most once per service day."""
        today = datetime.now(tz=SERVICE_TZ).date()
        if self._last_prune == today or self.cache is None:
            return
        removed = self.cache.prune(before=today - timedelta(days=CACHE_RETENTION_DAYS))
        self._last_prune = today
        if removed:
            log.info(
                "pruned prediction cache",
                extra={"feed": self.feed, "removed": removed, "remaining": len(self.cache)},
            )

    def _failure(
        self,
        started_at: datetime,
        duration_ms: int,
        status: str,
        exc: FeedError | Exception,
        http_code: int | None = None,
    ) -> poll_log.PollOutcome:
        health = self.state.health(self.feed)
        health.consecutive_failures += 1
        log.warning(
            "poll failed",
            extra={
                "feed": self.feed,
                "status": status,
                "http_code": http_code,
                "consecutive_failures": health.consecutive_failures,
                "duration_ms": duration_ms,
                "error": str(exc),
                # Never the raw URL: it carries the API key.
                "url": self.settings.redacted_feed_url(self.feed),
            },
        )
        return poll_log.PollOutcome(
            feed=self.feed,
            started_at=started_at,
            status=status,
            duration_ms=duration_ms,
            http_code=http_code,
            error=f"{type(exc).__name__}: {exc}",
        )

    # --- the loop ---

    async def run(self, stop: asyncio.Event) -> None:
        """Poll on a fixed cadence until stopped.

        The next poll is scheduled from this poll's start, not its end, so a slow
        response does not make the cadence drift. Even sampling matters because
        Phase 3 interpolates between pings. See ADR-0010.
        """
        interval = self.settings.poll_interval_seconds

        while not stop.is_set():
            cycle_started = time.monotonic()

            outcome = await self.poll_once()
            await poll_log.record(self.pool, outcome)

            if outcome.status in (poll_log.STATUS_OK, poll_log.STATUS_SKIPPED):
                delay = interval - (time.monotonic() - cycle_started)
                if delay < 0:
                    # The poll outran its own interval, so the next one is due
                    # immediately. Skipping rather than queueing keeps a slow
                    # feed from building a backlog that all fires at once.
                    log.warning(
                        "poll exceeded its interval",
                        extra={"feed": self.feed, "duration_ms": outcome.duration_ms},
                    )
                    delay = 0.0
            else:
                delay = self.backoff.record_failure()
                log.info(
                    "backing off",
                    extra={
                        "feed": self.feed,
                        "delay_seconds": round(delay, 2),
                        "ceiling_seconds": round(self.backoff.ceiling, 2),
                        "consecutive_failures": self.backoff.failures,
                    },
                )

            # Waiting on the stop event rather than sleeping means SIGTERM is
            # acted on immediately instead of after the interval elapses.
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=delay)


def http_timeout(settings: Settings) -> httpx.Timeout:
    """HTTP timeouts derived from the poll interval.

    A hung request must not stall the cadence, so the read timeout stays well
    inside the interval. Clamped at both ends: subtracting a constant from the
    interval goes negative for short intervals, which httpx rejects and which
    made every request fail, and an unbounded timeout would let one slow
    response block a whole cycle.
    """
    read = max(1.0, min(20.0, settings.poll_interval_seconds * 0.8))
    return httpx.Timeout(read, connect=min(5.0, read))


async def supervise(poller: FeedPoller, stop: asyncio.Event) -> None:
    """Restart a poll loop that dies from an unexpected error.

    Expected failures are already handled inside poll_once. This is the guard
    against a bug in our own code taking one feed offline silently, which would
    otherwise be invisible until someone noticed missing data. See ADR-0008.
    """
    while not stop.is_set():
        try:
            await poller.run(stop)
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("poll loop crashed, restarting", extra={"feed": poller.feed})
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=poller.settings.backoff_base_seconds)


async def run(settings: Settings, stop: asyncio.Event | None = None) -> CollectorState:
    """Run the collector until stopped. Returns the final state."""
    stop = stop or asyncio.Event()
    state = CollectorState()

    pool = await create_pool(settings)
    timeout = http_timeout(settings)

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            pollers = [
                FeedPoller(feed, settings, client, pool, state)
                for feed in (VEHICLE_POSITIONS, TRIP_UPDATES)
            ]
            log.info(
                "collector starting",
                extra={
                    "feeds": [p.feed for p in pollers],
                    "poll_interval_seconds": settings.poll_interval_seconds,
                    "using_mock_feed": settings.using_mock_feed,
                    "feed_base_url": settings.mts_feed_base_url,
                },
            )

            tasks = [
                asyncio.create_task(supervise(p, stop), name=f"poll-{p.feed}") for p in pollers
            ]
            try:
                await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
    finally:
        await pool.close()
        log.info(
            "collector stopped",
            extra={
                "polls": {f: h.polls for f, h in state.feeds.items()},
                "rows_written": {f: h.rows_written for f, h in state.feeds.items()},
            },
        )

    return state


def install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM and SIGINT request a graceful stop rather than killing the loop.

    launchd sends SIGTERM on stop and on restart, so this is the normal path,
    not an edge case. See ADR-0015.
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)


async def main_async(settings: Settings) -> None:
    stop = asyncio.Event()
    install_signal_handlers(stop)
    await run(settings, stop)


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main_async(settings))
