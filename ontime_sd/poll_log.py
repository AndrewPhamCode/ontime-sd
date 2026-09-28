"""Recording the outcome of every poll.

Every attempt gets a row, including skips and failures. Without the successes
there is no denominator, so neither the failure rate nor the collection coverage
Phase 5 depends on would be computable. See ADR-0018.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

import asyncpg

log = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped_unchanged"
STATUS_HTTP_ERROR = "http_error"
STATUS_PARSE_ERROR = "parse_error"
STATUS_DB_ERROR = "db_error"

_INSERT = """
insert into poll_log (
    feed, started_at, duration_ms, status, http_code, feed_timestamp,
    entity_count, rows_written, error
)
values ($1, $2, $3, $4, $5, $6, $7, $8, $9)
"""

# Long error text is truncated. The full traceback goes to the JSON log; this
# column exists to make failures queryable, not to store stack traces.
_MAX_ERROR_CHARS = 1000


@dataclass(frozen=True, slots=True)
class PollOutcome:
    feed: str
    started_at: datetime
    status: str
    duration_ms: int | None = None
    http_code: int | None = None
    feed_timestamp: datetime | None = None
    entity_count: int | None = None
    rows_written: int | None = None
    error: str | None = None


async def record(pool: asyncpg.Pool, outcome: PollOutcome) -> None:
    """Write one poll_log row.

    A failure here must never take down the collector. Losing observability is
    bad; losing unrecoverable realtime data because the observability table was
    unwritable is worse.
    """
    error = outcome.error
    if error is not None and len(error) > _MAX_ERROR_CHARS:
        error = error[:_MAX_ERROR_CHARS] + "..."

    try:
        await pool.execute(
            _INSERT,
            outcome.feed,
            outcome.started_at,
            outcome.duration_ms,
            outcome.status,
            outcome.http_code,
            outcome.feed_timestamp,
            outcome.entity_count,
            outcome.rows_written,
            error,
        )
    except (asyncpg.PostgresError, OSError):
        log.exception("could not write poll_log", extra={"feed": outcome.feed})
