"""Loading a static GTFS feed into Postgres.

Reading the feed is gtfs_static.py. This module maps rows to table tuples, loads
them with COPY, and derives service_dates.

The whole feed version is one transaction (ADR-0029). A load either represents the
feed completely or leaves the previous version untouched, so a failure partway
through cannot produce trips without their stop times, which would look like a
data problem rather than a loader problem months later.
"""

from __future__ import annotations

import json
import logging
import tempfile
import time
from collections.abc import Callable, Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path

import asyncpg
import httpx

from ontime_sd.config import Settings
from ontime_sd.gtfs_static import (
    GtfsArchive,
    GtfsError,
    GtfsHTTPError,
    GtfsParseError,
    GtfsTransportError,
    download_feed,
    gtfs_date,
    gtfs_time_to_seconds,
    head_feed,
    miles_to_metres,
    optional_float,
    optional_int,
    optional_text,
    service_flag,
)

log = logging.getLogger(__name__)

# Rows per COPY call. Bounds memory regardless of feed size: the generator
# feeding it stays lazy, so only one chunk is ever held. See ADR-0028.
COPY_CHUNK_ROWS = 50_000

STATUS_OK = "ok"
STATUS_SKIPPED_UNCHANGED = "skipped_unchanged"
STATUS_SKIPPED_ALREADY_LOADED = "skipped_already_loaded"
STATUS_HTTP_ERROR = "http_error"
STATUS_PARSE_ERROR = "parse_error"
STATUS_DB_ERROR = "db_error"

_MAX_ERROR_CHARS = 1000


def _required(row: dict[str, str], key: str, source: str) -> str:
    """Read a column GTFS requires, refusing to substitute a placeholder.

    These end up in primary keys, so an empty value cannot be tolerated: it would
    either fail the insert confusingly or, worse, create a row nothing can join
    to.
    """
    value = (row.get(key) or "").strip()
    if not value:
        raise GtfsParseError(f"{source}: required column {key} is missing or empty")
    return value


# --- row mappers --------------------------------------------------------------
#
# Each returns the tuple for its table without feed_version, which the loader
# prepends. Every optional column is read with .get() so a feed that omits the
# column entirely yields null rather than raising.


def _agency(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "agency_id", "agency.txt"),
        optional_text(row.get("agency_name")),
        optional_text(row.get("agency_url")),
        optional_text(row.get("agency_timezone")),
        optional_text(row.get("agency_lang")),
        optional_text(row.get("agency_phone")),
    )


def _route(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "route_id", "routes.txt"),
        optional_text(row.get("agency_id")),
        optional_text(row.get("route_short_name")),
        optional_text(row.get("route_long_name")),
        optional_int(row.get("route_type")),
        optional_text(row.get("route_color")),
        optional_text(row.get("route_text_color")),
    )


def _stop(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "stop_id", "stops.txt"),
        optional_text(row.get("stop_code")),
        optional_text(row.get("stop_name")),
        optional_float(row.get("stop_lat")),
        optional_float(row.get("stop_lon")),
        optional_int(row.get("location_type")),
        optional_text(row.get("parent_station")),
        optional_int(row.get("wheelchair_boarding")),
    )


def _trip(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "trip_id", "trips.txt"),
        _required(row, "route_id", "trips.txt"),
        _required(row, "service_id", "trips.txt"),
        optional_text(row.get("shape_id")),
        optional_text(row.get("trip_headsign")),
        optional_int(row.get("direction_id")),
        optional_text(row.get("block_id")),
    )


def _stop_time(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "trip_id", "stop_times.txt"),
        int(_required(row, "stop_sequence", "stop_times.txt")),
        _required(row, "stop_id", "stop_times.txt"),
        gtfs_time_to_seconds(row.get("arrival_time")),
        gtfs_time_to_seconds(row.get("departure_time")),
        miles_to_metres(row.get("shape_dist_traveled")),
        optional_int(row.get("pickup_type")),
        optional_int(row.get("drop_off_type")),
        optional_int(row.get("timepoint")),
        optional_text(row.get("stop_headsign")),
    )


def _shape(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "shape_id", "shapes.txt"),
        int(_required(row, "shape_pt_sequence", "shapes.txt")),
        float(_required(row, "shape_pt_lat", "shapes.txt")),
        float(_required(row, "shape_pt_lon", "shapes.txt")),
        miles_to_metres(row.get("shape_dist_traveled")),
    )


def _calendar(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "service_id", "calendar.txt"),
        service_flag(row.get("monday")),
        service_flag(row.get("tuesday")),
        service_flag(row.get("wednesday")),
        service_flag(row.get("thursday")),
        service_flag(row.get("friday")),
        service_flag(row.get("saturday")),
        service_flag(row.get("sunday")),
        gtfs_date(_required(row, "start_date", "calendar.txt")),
        gtfs_date(_required(row, "end_date", "calendar.txt")),
    )


def _calendar_date(row: dict[str, str]) -> tuple[object, ...]:
    return (
        _required(row, "service_id", "calendar_dates.txt"),
        gtfs_date(_required(row, "date", "calendar_dates.txt")),
        optional_int(_required(row, "exception_type", "calendar_dates.txt")),
    )


@dataclass(frozen=True, slots=True)
class TableSpec:
    table: str
    source_file: str
    columns: tuple[str, ...]
    mapper: Callable[[dict[str, str]], tuple[object, ...]]
    required: bool = True


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec(
        "agencies",
        "agency.txt",
        (
            "agency_id",
            "agency_name",
            "agency_url",
            "agency_timezone",
            "agency_lang",
            "agency_phone",
        ),
        _agency,
    ),
    TableSpec(
        "routes",
        "routes.txt",
        (
            "route_id",
            "agency_id",
            "route_short_name",
            "route_long_name",
            "route_type",
            "route_color",
            "route_text_color",
        ),
        _route,
    ),
    TableSpec(
        "stops",
        "stops.txt",
        (
            "stop_id",
            "stop_code",
            "stop_name",
            "stop_lat",
            "stop_lon",
            "location_type",
            "parent_station",
            "wheelchair_boarding",
        ),
        _stop,
    ),
    TableSpec(
        "trips",
        "trips.txt",
        (
            "trip_id",
            "route_id",
            "service_id",
            "shape_id",
            "trip_headsign",
            "direction_id",
            "block_id",
        ),
        _trip,
    ),
    TableSpec(
        "stop_times",
        "stop_times.txt",
        (
            "trip_id",
            "stop_sequence",
            "stop_id",
            "arrival_seconds",
            "departure_seconds",
            "shape_dist_traveled_m",
            "pickup_type",
            "drop_off_type",
            "timepoint",
            "stop_headsign",
        ),
        _stop_time,
    ),
    TableSpec(
        "shapes",
        "shapes.txt",
        ("shape_id", "shape_pt_sequence", "shape_pt_lat", "shape_pt_lon", "shape_dist_traveled_m"),
        _shape,
    ),
    TableSpec(
        "calendar",
        "calendar.txt",
        (
            "service_id",
            "monday",
            "tuesday",
            "wednesday",
            "thursday",
            "friday",
            "saturday",
            "sunday",
            "start_date",
            "end_date",
        ),
        _calendar,
    ),
    # Optional in GTFS: a feed may express all service through calendar.txt alone.
    TableSpec(
        "calendar_dates",
        "calendar_dates.txt",
        ("service_id", "service_date", "exception_type"),
        _calendar_date,
        required=False,
    ),
)


# --- service_dates ------------------------------------------------------------

_WEEKDAY_FIELDS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

EXCEPTION_ADDED = 1
EXCEPTION_REMOVED = 2


def expand_service_dates(
    calendar_rows: Iterable[dict[str, str]],
    calendar_date_rows: Iterable[dict[str, str]] = (),
) -> list[tuple[date, str]]:
    """Work out which service_id runs on which date.

    Walks each calendar row's weekday pattern across its date range, then applies
    calendar_dates exceptions. Materializing this once per load keeps the weekday
    and exception logic in one place instead of in every caller that asks what ran
    on a given day. See ADR-0027.

    Returns (service_date, service_id) pairs sorted, so a COPY of them is
    deterministic and two loads of the same feed produce identical rows.
    """
    running: set[tuple[date, str]] = set()

    for row in calendar_rows:
        service_id = _required(row, "service_id", "calendar.txt")
        start = gtfs_date(_required(row, "start_date", "calendar.txt"))
        end = gtfs_date(_required(row, "end_date", "calendar.txt"))
        if start is None or end is None:
            raise GtfsParseError(f"calendar.txt: {service_id} has no date range")
        if end < start:
            raise GtfsParseError(f"calendar.txt: {service_id} ends {end} before it starts {start}")

        weekdays = [service_flag(row.get(name)) for name in _WEEKDAY_FIELDS]
        if not any(weekdays):
            # Legal GTFS: a service that runs only on dates listed as exceptions.
            continue

        day = start
        while day <= end:
            if weekdays[day.weekday()]:
                running.add((day, service_id))
            day += timedelta(days=1)

    for row in calendar_date_rows:
        service_id = _required(row, "service_id", "calendar_dates.txt")
        day = gtfs_date(_required(row, "date", "calendar_dates.txt"))
        exception = optional_int(_required(row, "exception_type", "calendar_dates.txt"))
        if day is None:
            continue

        if exception == EXCEPTION_ADDED:
            # Added even when the service has no calendar.txt row at all, which is
            # how a feed can express service purely through exceptions.
            running.add((day, service_id))
        elif exception == EXCEPTION_REMOVED:
            running.discard((day, service_id))
        else:
            raise GtfsParseError(
                f"calendar_dates.txt: unknown exception_type {exception!r} "
                f"for {service_id} on {day}"
            )

    return sorted(running)


# --- COPY --------------------------------------------------------------------


def _chunks(records: Iterable[tuple[object, ...]], size: int) -> Iterator[list[tuple]]:
    """Group a lazy iterable into lists, without materializing the whole thing.

    itertools.batched would do this, but it needs Python 3.12 and the charter
    targets 3.11.
    """
    batch: list[tuple[object, ...]] = []
    for record in records:
        batch.append(record)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


async def copy_chunked(
    conn: asyncpg.Connection,
    table: str,
    columns: Sequence[str],
    records: Iterable[tuple[object, ...]],
    chunk_rows: int = COPY_CHUNK_ROWS,
) -> int:
    """COPY records in bounded chunks, returning how many rows were written."""
    written = 0
    for batch in _chunks(records, chunk_rows):
        await conn.copy_records_to_table(table, records=batch, columns=list(columns))
        written += len(batch)
    return written


# --- orchestration ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LoadResult:
    status: str
    feed_version: str | None = None
    rows_loaded: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0
    bytes_downloaded: int | None = None
    http_code: int | None = None
    error: str | None = None
    skipped: bool = False


async def _latest_download_headers(
    conn: asyncpg.Connection,
) -> tuple[str | None, int | None]:
    row = await conn.fetchrow(
        """
        select last_modified, content_length
        from feed_versions
        where loaded_at is not null
        order by downloaded_at desc
        limit 1
        """
    )
    if row is None:
        return None, None
    return row["last_modified"], row["content_length"]


async def _already_loaded(conn: asyncpg.Connection, feed_version: str) -> bool:
    return bool(
        await conn.fetchval(
            "select 1 from feed_versions where feed_version = $1 and loaded_at is not null",
            feed_version,
        )
    )


async def record_load(pool: asyncpg.Pool, result: LoadResult) -> None:
    """Write one gtfs_load_log row.

    Same reasoning as poll_log in ADR-0018: a loader that quietly stopped running
    looks identical to a feed that never changed, unless the skips are recorded
    too. A failure here must not fail the load.
    """
    error = result.error
    if error is not None and len(error) > _MAX_ERROR_CHARS:
        error = error[:_MAX_ERROR_CHARS] + "..."

    try:
        await pool.execute(
            """
            insert into gtfs_load_log (started_at, duration_ms, status, http_code,
                                       feed_version, bytes, rows_loaded, error)
            values (now(), $1, $2, $3, $4, $5, $6::jsonb, $7)
            """,
            result.duration_ms,
            result.status,
            result.http_code,
            result.feed_version,
            result.bytes_downloaded,
            json.dumps(result.rows_loaded) if result.rows_loaded else None,
            error,
        )
    except (asyncpg.PostgresError, OSError):
        log.exception("could not write gtfs_load_log")


async def load_archive(
    conn: asyncpg.Connection,
    archive: GtfsArchive,
    feed_version: str,
    *,
    source_url: str,
    size_bytes: int | None = None,
    last_modified: str | None = None,
    chunk_rows: int = COPY_CHUNK_ROWS,
    replace: bool = False,
) -> dict[str, int]:
    """Load one archive inside a single transaction.

    The feed_versions row is inserted first so the foreign keys have a parent to
    validate against, and loaded_at is set last, inside the same transaction. A
    reader therefore never sees a feed version that is missing rows, and a crash
    leaves no trace of the attempt at all.
    """
    archive.validate()

    info = archive.feed_info() or {}
    counts: dict[str, int] = {}

    async with conn.transaction():
        if replace:
            # Reloading the same bytes, so the old copy has to go first. The
            # cascade takes its rows with it, and being inside the transaction
            # means a failure leaves the original in place.
            await conn.execute("delete from feed_versions where feed_version = $1", feed_version)

        await conn.execute(
            """
            insert into feed_versions (feed_version, source_url, content_length,
                                       last_modified, mts_feed_version,
                                       feed_start_date, feed_end_date)
            values ($1, $2, $3, $4, $5, $6, $7)
            """,
            feed_version,
            source_url,
            size_bytes,
            last_modified,
            optional_text(info.get("feed_version")),
            gtfs_date(info.get("feed_start_date")),
            gtfs_date(info.get("feed_end_date")),
        )

        for spec in TABLE_SPECS:
            if not archive.has(spec.source_file):
                if spec.required:
                    raise GtfsParseError(f"archive is missing {spec.source_file}")
                counts[spec.table] = 0
                continue

            records = ((feed_version, *spec.mapper(row)) for row in archive.rows(spec.source_file))
            counts[spec.table] = await copy_chunked(
                conn, spec.table, ("feed_version", *spec.columns), records, chunk_rows
            )
            log.info(
                "loaded table",
                extra={
                    "table": spec.table,
                    "rows": counts[spec.table],
                    "feed_version": feed_version[:12],
                },
            )

        calendar_dates = (
            list(archive.rows("calendar_dates.txt")) if archive.has("calendar_dates.txt") else []
        )
        pairs = expand_service_dates(archive.rows("calendar.txt"), calendar_dates)
        counts["service_dates"] = await copy_chunked(
            conn,
            "service_dates",
            ("feed_version", "service_date", "service_id"),
            ((feed_version, day, service_id) for day, service_id in pairs),
            chunk_rows,
        )

        await conn.execute(
            "update feed_versions set loaded_at = now(), row_counts = $2::jsonb "
            "where feed_version = $1",
            feed_version,
            json.dumps(counts),
        )

    return counts


async def load_feed(
    settings: Settings,
    pool: asyncpg.Pool,
    *,
    force: bool = False,
    client: httpx.AsyncClient | None = None,
) -> LoadResult:
    """Check, download, and load the static feed. Returns what happened."""
    started = time.monotonic()
    url = settings.gtfs_static_url

    def elapsed_ms() -> int:
        return int((time.monotonic() - started) * 1000)

    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0))

    try:
        async with pool.acquire() as conn:
            previous = await _latest_download_headers(conn)

        try:
            if not force:
                meta = await head_feed(client, url)
                if meta.looks_unchanged_from(*previous):
                    log.info(
                        "feed unchanged, nothing downloaded",
                        extra={"last_modified": meta.last_modified, "url": url},
                    )
                    return LoadResult(
                        status=STATUS_SKIPPED_UNCHANGED,
                        duration_ms=elapsed_ms(),
                        http_code=200,
                        skipped=True,
                    )

            with tempfile.TemporaryDirectory(prefix="ontime-gtfs-") as tmp:
                destination = Path(tmp) / "google_transit.zip"
                downloaded = await download_feed(client, url, destination)
                log.info(
                    "feed downloaded",
                    extra={
                        "bytes": downloaded.size_bytes,
                        "feed_version": downloaded.sha256[:12],
                    },
                )

                async with pool.acquire() as conn:
                    if not force and await _already_loaded(conn, downloaded.sha256):
                        log.info(
                            "feed already loaded",
                            extra={"feed_version": downloaded.sha256[:12]},
                        )
                        return LoadResult(
                            status=STATUS_SKIPPED_ALREADY_LOADED,
                            feed_version=downloaded.sha256,
                            duration_ms=elapsed_ms(),
                            http_code=200,
                            bytes_downloaded=downloaded.size_bytes,
                            skipped=True,
                        )

                    with GtfsArchive(destination) as archive:
                        counts = await load_archive(
                            conn,
                            archive,
                            downloaded.sha256,
                            source_url=url,
                            size_bytes=downloaded.size_bytes,
                            last_modified=downloaded.meta.last_modified,
                            replace=force,
                        )

            return LoadResult(
                status=STATUS_OK,
                feed_version=downloaded.sha256,
                rows_loaded=counts,
                duration_ms=elapsed_ms(),
                http_code=200,
                bytes_downloaded=downloaded.size_bytes,
            )

        except GtfsHTTPError as exc:
            return LoadResult(
                status=STATUS_HTTP_ERROR,
                duration_ms=elapsed_ms(),
                http_code=exc.status_code,
                error=f"{type(exc).__name__}: {exc}",
            )
        except GtfsTransportError as exc:
            return LoadResult(
                status=STATUS_HTTP_ERROR,
                duration_ms=elapsed_ms(),
                error=f"{type(exc).__name__}: {exc}",
            )
        except (GtfsParseError, GtfsError, ValueError) as exc:
            return LoadResult(
                status=STATUS_PARSE_ERROR,
                duration_ms=elapsed_ms(),
                error=f"{type(exc).__name__}: {exc}",
            )
        except (asyncpg.PostgresError, OSError) as exc:
            return LoadResult(
                status=STATUS_DB_ERROR,
                duration_ms=elapsed_ms(),
                error=f"{type(exc).__name__}: {exc}",
            )
    finally:
        if owns_client:
            await client.aclose()


async def run(settings: Settings | None = None, *, force: bool = False) -> LoadResult:
    from ontime_sd.db import create_pool

    settings = settings or Settings.from_env()
    pool = await create_pool(settings)
    try:
        result = await load_feed(settings, pool, force=force)
        await record_load(pool, result)
        return result
    finally:
        await pool.close()


def main() -> None:
    """Entry point for `make load-gtfs`."""
    import argparse
    import asyncio

    from ontime_sd.logging_setup import configure_logging

    parser = argparse.ArgumentParser(description="Load the static MTS GTFS feed")
    parser.add_argument(
        "--force",
        action="store_true",
        help="load even if the feed looks unchanged or was already loaded",
    )
    args = parser.parse_args()

    settings = Settings.from_env()
    configure_logging(settings.log_level)
    result = asyncio.run(run(settings, force=args.force))

    if result.status == STATUS_OK:
        total = sum(result.rows_loaded.values())
        print(
            f"loaded {result.feed_version[:12] if result.feed_version else '?'} "
            f"({total:,} rows in {result.duration_ms / 1000:.1f}s)"
        )
        for table, count in sorted(result.rows_loaded.items()):
            print(f"  {table:<16} {count:>9,}")
    elif result.skipped:
        print(f"{result.status}: nothing to do")
    else:
        raise SystemExit(f"{result.status}: {result.error}")
