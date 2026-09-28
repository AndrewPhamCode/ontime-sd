"""Reading the static GTFS feed: conversions, archive streaming, and fetching.

Parsing is strict on purpose. A guessed time or distance would corrupt Phase 4
silently and months later, in a way that looks like model error rather than a
loader bug, so these tests pin what is accepted and what is refused.
"""

from __future__ import annotations

import email.utils
import hashlib
import zipfile
from collections.abc import AsyncIterator, Iterator
from datetime import date
from pathlib import Path

import httpx
import pytest
import pytest_asyncio

from ontime_sd.gtfs_static import (
    MILES_TO_METRES,
    REQUIRED_FILES,
    DownloadedFeed,
    GtfsArchive,
    GtfsHTTPError,
    GtfsParseError,
    GtfsTransportError,
    RemoteFeedMeta,
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
from ontime_sd.tiny_http import Request, Response, bound_port, serve
from tests.gtfs_fixtures import write_feed

# --- gtfs_time_to_seconds: ADR-0025 -------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("00:00:00", 0),
        ("05:05:00", 18300),
        ("05:12:30", 18750),
        ("23:59:59", 86399),
        # The cases a time column could not hold. 11,432 rows in the real feed
        # are at hour 24 or later.
        ("24:00:00", 86400),
        ("25:30:00", 91800),
        ("27:15:00", 98100),
        # Single digit hours are legal GTFS.
        ("5:05:00", 18300),
        # Absent values are absent, not zero.
        ("", None),
        ("   ", None),
        (None, None),
    ],
)
def test_time_conversion(value: str | None, expected: int | None) -> None:
    assert gtfs_time_to_seconds(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "05:05",  # too few parts
        "05:05:00:00",  # too many
        "aa:bb:cc",  # not numeric
        "05:60:00",  # minutes out of range
        "05:00:60",  # seconds out of range
        "-1:00:00",  # negative
        "+5:00:00",  # signed
        "05:5a:00",  # partially numeric
        "05.05.00",  # wrong separator
    ],
)
def test_malformed_time_is_refused(value: str) -> None:
    """Refusing beats guessing: a bad time here becomes bogus Phase 4 error."""
    with pytest.raises(GtfsParseError):
        gtfs_time_to_seconds(value)


def test_time_error_names_the_offending_value() -> None:
    with pytest.raises(GtfsParseError, match="05:05"):
        gtfs_time_to_seconds("05:05")


# --- miles_to_metres: ADR-0026 ------------------------------------------------


def test_one_mile_is_1609_metres() -> None:
    assert miles_to_metres("1.0") == pytest.approx(1609.344)


def test_conversion_matches_the_measured_real_shape() -> None:
    """Shape 891_2_11 declares 88.22 miles and measures 142.00 km geometrically.

    This is the check that catches a unit mistake, which would otherwise be a
    silent 1609x error where Phase 3 mixes these with GPS distances.
    """
    metres = miles_to_metres("88.22")
    assert metres is not None
    assert metres / 1000 == pytest.approx(142.0, abs=0.1)


@pytest.mark.parametrize(("value", "expected"), [("0.000000", 0.0), ("", None), (None, None)])
def test_distance_edge_values(value: str | None, expected: float | None) -> None:
    assert miles_to_metres(value) == expected


@pytest.mark.parametrize("value", ["-1.0", "not-a-number", "1.0 mi"])
def test_bad_distance_is_refused(value: str) -> None:
    with pytest.raises(GtfsParseError):
        miles_to_metres(value)


def test_metres_constant_is_the_exact_international_mile() -> None:
    assert MILES_TO_METRES == 1609.344


# --- other scalars ------------------------------------------------------------


def test_gtfs_date_parsing() -> None:
    assert gtfs_date("20260607") == date(2026, 6, 7)
    assert gtfs_date("") is None
    assert gtfs_date(None) is None


@pytest.mark.parametrize("value", ["2026-06-07", "20261301", "June 7", "202666"])
def test_bad_date_is_refused(value: str) -> None:
    with pytest.raises(GtfsParseError):
        gtfs_date(value)


def test_optional_scalars() -> None:
    assert optional_int("3") == 3
    assert optional_int("") is None
    assert optional_float("32.715") == pytest.approx(32.715)
    assert optional_float("") is None
    with pytest.raises(GtfsParseError):
        optional_int("three")


def test_service_flag_only_accepts_one_as_true() -> None:
    assert service_flag("1") is True
    assert service_flag("0") is False
    assert service_flag("") is False
    assert service_flag(None) is False


def test_empty_text_becomes_null_not_blank() -> None:
    """Absent data should read as absent, so a join does not match on ''."""
    assert optional_text("") is None
    assert optional_text("  ") is None
    assert optional_text(" La Mesa ") == "La Mesa"


# --- GtfsArchive --------------------------------------------------------------


@pytest.fixture
def feed(tmp_path: Path) -> Path:
    return write_feed(tmp_path / "google_transit.zip")


def test_archive_lists_its_files(feed: Path) -> None:
    with GtfsArchive(feed) as archive:
        assert archive.has("stop_times.txt")
        assert not archive.has("nonsense.txt")
        assert set(REQUIRED_FILES) <= set(archive.names)


def test_archive_yields_rows_as_dicts(feed: Path) -> None:
    with GtfsArchive(feed) as archive:
        rows = list(archive.rows("stop_times.txt"))

    assert len(rows) == 7
    assert rows[0]["trip_id"] == "trip_day"
    assert rows[0]["arrival_time"] == "05:05:00"
    assert rows[0]["shape_dist_traveled"] == "0.000000"


def test_archive_rows_are_streamed_not_materialized(feed: Path) -> None:
    """stop_times.txt is 74 MB in the real feed, so this must stay lazy."""
    with GtfsArchive(feed) as archive:
        rows = archive.rows("stop_times.txt")
        assert isinstance(rows, Iterator)
        first = next(rows)
        assert first["trip_id"] == "trip_day"


def test_archive_handles_a_byte_order_mark(tmp_path: Path) -> None:
    """Some GTFS publishers emit a BOM. The current MTS feed does not, but a
    future publication may, and a BOM glued to the first column name would break
    every row silently.
    """
    path = tmp_path / "bom.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("stops.txt", "﻿stop_id,stop_name\nstop_a,First & Main\n")

    with GtfsArchive(path) as gtfs:
        rows = list(gtfs.rows("stops.txt"))

    assert rows[0]["stop_id"] == "stop_a", "BOM must not corrupt the first column"


def test_missing_file_is_an_error(feed: Path) -> None:
    with GtfsArchive(feed) as archive, pytest.raises(GtfsParseError, match=r"no missing\.txt"):
        list(archive.rows("missing.txt"))


def test_validate_passes_on_a_complete_feed(feed: Path) -> None:
    with GtfsArchive(feed) as archive:
        archive.validate()


def test_validate_names_every_missing_required_file(tmp_path: Path) -> None:
    """Fail before loading anything, and say what is wrong in one message."""
    path = write_feed(tmp_path / "partial.zip", omit=("shapes.txt", "calendar.txt"))

    with GtfsArchive(path) as archive, pytest.raises(GtfsParseError) as exc:
        archive.validate()

    assert "shapes.txt" in str(exc.value)
    assert "calendar.txt" in str(exc.value)


def test_a_file_that_is_not_a_zip_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "garbage.zip"
    path.write_bytes(b"this is not a zip archive")

    with pytest.raises(GtfsParseError, match="not a readable zip"):
        GtfsArchive(path)


def test_feed_info_is_read_when_present(feed: Path) -> None:
    with GtfsArchive(feed) as archive:
        info = archive.feed_info()

    assert info is not None
    assert info["feed_version"] == "Generated on 20260901 @ 1200000"
    assert info["feed_start_date"] == "20260907"


def test_feed_info_absent_is_none_not_an_error(tmp_path: Path) -> None:
    """feed_info.txt is optional in GTFS."""
    path = write_feed(tmp_path / "no_info.zip", omit=("feed_info.txt",))

    with GtfsArchive(path) as archive:
        assert archive.feed_info() is None


# --- RemoteFeedMeta comparison ------------------------------------------------


def test_identical_headers_look_unchanged() -> None:
    meta = RemoteFeedMeta(last_modified="Fri, 21 Aug 2026 16:28:42 GMT", content_length=8804780)
    assert meta.looks_unchanged_from("Fri, 21 Aug 2026 16:28:42 GMT", 8804780) is True


@pytest.mark.parametrize(
    ("last_modified", "length"),
    [
        ("Sat, 22 Aug 2026 16:28:42 GMT", 8804780),  # republished
        ("Fri, 21 Aug 2026 16:28:42 GMT", 8804781),  # size changed
        (None, 8804780),  # nothing to compare
    ],
)
def test_differing_or_absent_headers_do_not_look_unchanged(
    last_modified: str | None, length: int | None
) -> None:
    """When we cannot tell, download and hash. Never assume unchanged."""
    meta = RemoteFeedMeta(last_modified="Fri, 21 Aug 2026 16:28:42 GMT", content_length=8804780)
    assert meta.looks_unchanged_from(last_modified, length) is False


def test_meta_without_headers_never_matches() -> None:
    assert RemoteFeedMeta(None, None).looks_unchanged_from(None, None) is False


# --- fetching over real HTTP --------------------------------------------------

LAST_MODIFIED = email.utils.formatdate(1787329722, usegmt=True)


@pytest_asyncio.fixture(loop_scope="session")
async def feed_server(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[tuple[str, Path]]:
    """Serve the fixture zip over real HTTP, with realistic headers."""
    path = write_feed(tmp_path_factory.mktemp("served") / "google_transit.zip")
    payload = path.read_bytes()

    async def handle(request: Request) -> Response:
        if request.path == "/missing.zip":
            return Response(status=404, body=b"no such feed")
        if request.path == "/broken.zip":
            return Response(status=500, body=b"upstream error")

        headers = (("Last-Modified", LAST_MODIFIED), ("Content-Length", str(len(payload))))
        if request.method == "HEAD":
            return Response(body=b"", content_type="application/zip", headers=headers)
        return Response(body=payload, content_type="application/zip", headers=headers)

    server = await serve(handle, 0)
    try:
        yield f"http://127.0.0.1:{bound_port(server)}", path
    finally:
        server.close()
        await server.wait_closed()


async def test_head_reports_the_headers_needed_to_skip(
    feed_server: tuple[str, Path],
) -> None:
    base, path = feed_server
    async with httpx.AsyncClient(timeout=10) as client:
        meta = await head_feed(client, f"{base}/google_transit.zip")

    assert meta.last_modified == LAST_MODIFIED
    assert meta.content_length == path.stat().st_size


async def test_download_streams_and_hashes(feed_server: tuple[str, Path], tmp_path: Path) -> None:
    base, source = feed_server
    destination = tmp_path / "downloaded.zip"

    async with httpx.AsyncClient(timeout=10) as client:
        result = await download_feed(client, f"{base}/google_transit.zip", destination)

    assert isinstance(result, DownloadedFeed)
    assert result.size_bytes == source.stat().st_size
    # The hash must be of exactly the bytes that landed on disk.
    assert result.sha256 == hashlib.sha256(destination.read_bytes()).hexdigest()
    assert result.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert result.meta.last_modified == LAST_MODIFIED


async def test_downloaded_feed_is_a_usable_archive(
    feed_server: tuple[str, Path], tmp_path: Path
) -> None:
    """End to end: fetched bytes parse as GTFS."""
    base, _ = feed_server
    destination = tmp_path / "downloaded.zip"

    async with httpx.AsyncClient(timeout=10) as client:
        await download_feed(client, f"{base}/google_transit.zip", destination)

    with GtfsArchive(destination) as archive:
        archive.validate()
        assert len(list(archive.rows("trips.txt"))) == 3


@pytest.mark.parametrize(("path", "status"), [("/missing.zip", 404), ("/broken.zip", 500)])
async def test_http_failure_carries_the_status(
    feed_server: tuple[str, Path], tmp_path: Path, path: str, status: int
) -> None:
    base, _ = feed_server
    async with httpx.AsyncClient(timeout=10) as client:
        with pytest.raises(GtfsHTTPError) as exc:
            await download_feed(client, f"{base}{path}", tmp_path / "x.zip")
    assert exc.value.status_code == status

    async with httpx.AsyncClient(timeout=10) as client:
        with pytest.raises(GtfsHTTPError):
            await head_feed(client, f"{base}{path}")


async def test_unreachable_host_is_a_transport_error(tmp_path: Path) -> None:
    """No HTTP status exists, so the loader must not invent one."""
    async with httpx.AsyncClient(timeout=2) as client:
        with pytest.raises(GtfsTransportError):
            await download_feed(client, "http://127.0.0.1:1/feed.zip", tmp_path / "x.zip")


# --- the real feed, deselected by default ------------------------------------


@pytest.mark.network
async def test_real_mts_feed_still_matches_our_assumptions(tmp_path: Path) -> None:
    """Guards the measurements the Phase 2 design rests on.

    Run with `uv run pytest -m network`. If this fails, MTS changed something and
    DESIGN.md's numbers need revisiting before trusting a load.
    """
    url = "https://www.sdmts.com/google_transit_files/google_transit.zip"
    destination = tmp_path / "real.zip"

    async with httpx.AsyncClient(timeout=120) as client:
        meta = await head_feed(client, url)
        assert meta.content_length and meta.content_length > 1_000_000
        result = await download_feed(client, url, destination)

    with GtfsArchive(destination) as archive:
        archive.validate()

        stop_times = archive.rows("stop_times.txt")
        first = next(stop_times)
        assert "shape_dist_traveled" in first, "Phase 3 depends on this column"
        assert "stop_sequence" in first

        past_midnight = sum(
            1 for row in stop_times if (gtfs_time_to_seconds(row["arrival_time"]) or 0) >= 86400
        )

    # Measured at 11,432 in the 2026-08-21 feed. Allowed to drift, but the
    # phenomenon must still exist or ADR-0025 loses its justification.
    assert past_midnight > 1000, f"expected times past midnight, found {past_midnight}"
    assert len(result.sha256) == 64
