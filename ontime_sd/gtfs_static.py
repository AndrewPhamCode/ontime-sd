"""Reading the static GTFS schedule feed.

This module gets bytes from MTS and rows out of the zip. Putting those rows into
Postgres is gtfs_load.py.

Parsing is strict rather than forgiving. A malformed time or distance means the
feed is not what this project believes it is, and storing a guessed value would
corrupt Phase 4 silently, months later, in a way that looks like model error. A
load either represents the feed faithfully or it fails and says why. Failing is
cheap because the load is a single transaction (ADR-0029), so a rejected feed
leaves the previous version in place and untouched.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import httpx

log = logging.getLogger(__name__)

# The source feed expresses shape_dist_traveled in miles. Verified by summing
# haversine distance along the longest shapes and comparing to the declared
# value, which matched to 0.0%. See ADR-0026.
MILES_TO_METRES = 1609.344

_DOWNLOAD_CHUNK_BYTES = 1 << 16

# Files this project reads. The fare_*, transfers, networks, route_networks and
# fare_capping files are deliberately ignored: none affect arrival prediction.
# See ADR-0030.
REQUIRED_FILES = (
    "agency.txt",
    "routes.txt",
    "trips.txt",
    "stop_times.txt",
    "stops.txt",
    "shapes.txt",
    "calendar.txt",
)
OPTIONAL_FILES = ("calendar_dates.txt", "feed_info.txt")


class GtfsError(Exception):
    """Base class for anything that makes a load fail."""


class GtfsHTTPError(GtfsError):
    def __init__(self, status_code: int, url: str) -> None:
        super().__init__(f"GTFS feed returned HTTP {status_code}")
        self.status_code = status_code
        self.url = url


class GtfsTransportError(GtfsError):
    """Connection refused, DNS failure, timeout: no HTTP status exists."""


class GtfsParseError(GtfsError):
    """The archive or one of its values was not what GTFS requires."""


# --- scalar conversions -------------------------------------------------------


def gtfs_time_to_seconds(value: str | None) -> int | None:
    """Convert a GTFS time to seconds past service-day midnight.

    GTFS times are relative to noon minus twelve hours on the service day and may
    exceed 24:00:00 for a trip that runs past midnight. In the feed current at the
    time of writing, 11,432 stop_times rows are at hour 24 or later and the
    largest hour is 27, so this is the normal case and not an edge case. See
    ADR-0025.

    >>> gtfs_time_to_seconds("05:05:00")
    18300
    >>> gtfs_time_to_seconds("27:15:00")
    98100
    """
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    parts = text.split(":")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise GtfsParseError(f"expected HH:MM:SS, got {value!r}")

    hours, minutes, seconds = (int(part) for part in parts)
    if minutes > 59 or seconds > 59:
        raise GtfsParseError(f"minutes and seconds must be under 60, got {value!r}")

    # No upper bound on hours on purpose. 27:15:00 is valid GTFS.
    return hours * 3600 + minutes * 60 + seconds


def miles_to_metres(value: str | float | None) -> float | None:
    """Convert a shape_dist_traveled value from miles to metres."""
    if value is None or value == "":
        return None
    try:
        miles = float(value)
    except (TypeError, ValueError) as exc:
        raise GtfsParseError(f"expected a number of miles, got {value!r}") from exc

    if miles < 0:
        raise GtfsParseError(f"distance cannot be negative, got {value!r}")
    return miles * MILES_TO_METRES


def gtfs_date(value: str | None) -> date | None:
    """Parse a GTFS YYYYMMDD date."""
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None

    # strptime is lenient about field widths, so "202666" would otherwise parse
    # as 2026-06-06. GTFS requires exactly eight digits.
    if len(text) != 8 or not text.isdigit():
        raise GtfsParseError(f"expected 8 digit YYYYMMDD, got {value!r}")

    try:
        return datetime.strptime(text, "%Y%m%d").date()
    except ValueError as exc:
        raise GtfsParseError(f"expected YYYYMMDD, got {value!r}") from exc


def optional_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise GtfsParseError(f"expected an integer, got {value!r}") from exc


def optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError as exc:
        raise GtfsParseError(f"expected a number, got {value!r}") from exc


def service_flag(value: str | None) -> bool:
    """Parse a calendar.txt weekday column, where 1 means the service runs."""
    return (value or "").strip() == "1"


def optional_text(value: str | None) -> str | None:
    """Empty strings become null, so absent data is absent rather than blank."""
    if value is None:
        return None
    text = value.strip()
    return text or None


# --- fetching -----------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class RemoteFeedMeta:
    """What a HEAD request can tell us without downloading the feed."""

    last_modified: str | None
    content_length: int | None

    def looks_unchanged_from(self, last_modified: str | None, content_length: int | None) -> bool:
        """Whether this matches a previously recorded download.

        Both values have to be present and equal. A missing header means we
        cannot tell, and in that case downloading and hashing is the honest
        answer rather than assuming nothing changed.
        """
        if self.last_modified is None or last_modified is None:
            return False
        if self.content_length is None or content_length is None:
            return False
        return self.last_modified == last_modified and self.content_length == content_length


@dataclass(frozen=True, slots=True)
class DownloadedFeed:
    path: Path
    sha256: str
    size_bytes: int
    meta: RemoteFeedMeta


async def head_feed(client: httpx.AsyncClient, url: str) -> RemoteFeedMeta:
    """Ask whether the feed changed, without transferring 8 MB."""
    try:
        response = await client.head(url, follow_redirects=True)
    except httpx.HTTPError as exc:
        raise GtfsTransportError(str(exc)) from exc

    if response.status_code != 200:
        raise GtfsHTTPError(response.status_code, url)

    length = response.headers.get("content-length")
    return RemoteFeedMeta(
        last_modified=response.headers.get("last-modified"),
        content_length=int(length) if length and length.isdigit() else None,
    )


async def download_feed(client: httpx.AsyncClient, url: str, destination: Path) -> DownloadedFeed:
    """Stream the feed to disk, hashing as it goes.

    Hashing during the download rather than reading the file again afterwards
    means the 8 MB is only ever handled once, and the hash is of exactly the
    bytes that landed on disk.
    """
    digest = hashlib.sha256()
    size = 0

    try:
        async with client.stream("GET", url, follow_redirects=True) as response:
            if response.status_code != 200:
                raise GtfsHTTPError(response.status_code, url)

            with destination.open("wb") as handle:
                async for chunk in response.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                    digest.update(chunk)
                    size += len(chunk)
                    handle.write(chunk)

            length = response.headers.get("content-length")
            meta = RemoteFeedMeta(
                last_modified=response.headers.get("last-modified"),
                content_length=int(length) if length and length.isdigit() else None,
            )
    except httpx.HTTPError as exc:
        raise GtfsTransportError(str(exc)) from exc

    return DownloadedFeed(path=destination, sha256=digest.hexdigest(), size_bytes=size, meta=meta)


# --- reading the archive ------------------------------------------------------


class GtfsArchive:
    """Streaming reader for a GTFS zip.

    Rows are yielded one at a time rather than materialized. stop_times.txt is
    74 MB uncompressed and 1.37M rows in the current feed, so holding it in memory
    would be a self inflicted scaling limit for no benefit.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        try:
            self._zip = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            raise GtfsParseError(f"{path} is not a readable zip: {exc}") from exc

    def __enter__(self) -> GtfsArchive:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._zip.close()

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(self._zip.namelist())

    def has(self, name: str) -> bool:
        return name in self._zip.namelist()

    def validate(self) -> None:
        """Fail before loading anything if a required file is absent."""
        missing = [name for name in REQUIRED_FILES if not self.has(name)]
        if missing:
            raise GtfsParseError(f"archive is missing required files: {', '.join(missing)}")

    def rows(self, name: str) -> Iterator[dict[str, str]]:
        """Yield rows of one file as dicts.

        Decoded as utf-8-sig so a byte order mark, which some GTFS publishers
        emit, does not end up glued to the first column name. The current MTS
        feed has no BOM, but a future publication is free to add one and that
        should not break the loader.
        """
        if not self.has(name):
            raise GtfsParseError(f"archive has no {name}")

        with self._zip.open(name) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
            reader = csv.DictReader(text)
            if reader.fieldnames is None:
                raise GtfsParseError(f"{name} has no header row")
            yield from reader

    def feed_info(self) -> dict[str, str] | None:
        """The publisher's own version metadata, if the feed carries any."""
        if not self.has("feed_info.txt"):
            return None
        for row in self.rows("feed_info.txt"):
            return row
        return None
