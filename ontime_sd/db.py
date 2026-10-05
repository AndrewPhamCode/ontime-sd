"""Database access and schema migration.

Raw SQL over asyncpg with no ORM. The ingest path is bulk insert with conflict
handling and Phases 3 through 5 are analytical queries, which are both places an
ORM costs more than it gives. See DESIGN.md ADR-0003.

Migrations are numbered .sql files applied in filename order, each in its own
transaction, with applied versions recorded in schema_migrations. See ADR-0002.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import asyncpg

from ontime_sd.config import MIGRATIONS_DIR, Settings

log = logging.getLogger(__name__)

_MIGRATIONS_TABLE = """
create table if not exists schema_migrations (
    version    text        primary key,
    applied_at timestamptz not null default now()
)
"""


# A serving query that takes longer than this is stuck, and failing fast is
# better than holding a connection. Right for the collector and the API.
SERVING_COMMAND_TIMEOUT = 30

# Batch jobs are the opposite case: their queries legitimately run for minutes
# because they aggregate the whole history, so a 30 second limit is a bug rather
# than a safeguard. `ontime-evaluate --days 8` died on this against 7.4M
# predictions on a t4g.small, having worked on a faster laptop, which is exactly
# the shape of failure a shared default produces. Still bounded, because a batch
# job that runs for half an hour is also wrong and should say so.
BATCH_COMMAND_TIMEOUT = 1800


async def create_pool(settings: Settings, **kwargs: object) -> asyncpg.Pool:
    """Open the connection pool.

    The pool is small on purpose. The collector has one writer task per feed
    plus the health endpoint, so a large pool would only mask a stuck query.

    Batch entry points pass `command_timeout=BATCH_COMMAND_TIMEOUT`. The default
    here stays the serving one, so a new caller that forgets inherits the
    conservative value rather than an unbounded wait.
    """
    kwargs.setdefault("min_size", 1)
    kwargs.setdefault("max_size", 4)
    kwargs.setdefault("command_timeout", SERVING_COMMAND_TIMEOUT)
    return await asyncpg.create_pool(settings.database_url, **kwargs)


def pending_migrations(applied: set[str], migrations_dir: Path = MIGRATIONS_DIR) -> list[Path]:
    """Migration files not yet applied, in filename order.

    Filenames are zero padded so lexical order is numeric order.
    """
    return sorted(p for p in migrations_dir.glob("*.sql") if p.stem not in applied)


async def run_migrations(
    conn: asyncpg.Connection, migrations_dir: Path = MIGRATIONS_DIR
) -> list[str]:
    """Apply pending migrations and return the versions applied.

    Each file runs inside a transaction, so a migration that fails partway
    leaves the schema untouched rather than half applied. Postgres supports
    transactional DDL, which is what makes this safe.
    """
    await conn.execute(_MIGRATIONS_TABLE)
    rows = await conn.fetch("select version from schema_migrations")
    applied = {row["version"] for row in rows}

    pending = pending_migrations(applied, migrations_dir)
    if not pending:
        log.info("no pending migrations", extra={"applied_count": len(applied)})
        return []

    versions: list[str] = []
    for path in pending:
        sql = path.read_text()
        async with conn.transaction():
            await conn.execute(sql)
            await conn.execute("insert into schema_migrations (version) values ($1)", path.stem)
        log.info("applied migration", extra={"version": path.stem})
        versions.append(path.stem)
    return versions


async def migrate(settings: Settings | None = None) -> list[str]:
    settings = settings or Settings.from_env()
    conn = await asyncpg.connect(settings.database_url)
    try:
        return await run_migrations(conn)
    finally:
        await conn.close()


def migrate_main() -> None:
    """Entry point for `make migrate`."""
    from ontime_sd.logging_setup import configure_logging

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    applied = asyncio.run(migrate(settings))
    if applied:
        print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("schema already up to date")
