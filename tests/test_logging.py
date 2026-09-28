"""Log output must never contain the API key.

The feed URL carries the key as a query parameter, and the collector log is a
plaintext file on disk that launchd appends to indefinitely. A key leaked there
is a key leaked, even though it was never committed.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from ontime_sd.logging_setup import configure_logging
from tests.conftest import make_settings

KEY = "super-secret-mts-key"


@pytest.fixture(autouse=True)
def restore_logging():
    """configure_logging mutates global state, so put it back afterwards."""
    root = logging.getLogger()
    saved = (root.handlers[:], root.level)
    saved_levels = {n: logging.getLogger(n).level for n in ("httpx", "httpcore")}
    yield
    root.handlers[:] = saved[0]
    root.setLevel(saved[1])
    for name, level in saved_levels.items():
        logging.getLogger(name).setLevel(level)


def test_httpx_request_logging_is_silenced() -> None:
    """Regression: httpx logged the full keyed URL at INFO on every poll."""
    configure_logging("INFO")

    assert logging.getLogger("httpx").level == logging.WARNING
    assert logging.getLogger("httpcore").level == logging.WARNING


async def test_a_real_request_does_not_log_the_key(
    caplog: pytest.LogCaptureFixture,
) -> None:
    configure_logging("INFO")

    with caplog.at_level(logging.INFO):
        async with httpx.AsyncClient(timeout=2) as client:
            # Nothing is listening, so this fails. httpx logs the attempt
            # either way, which is the leak being tested.
            with pytest.raises(httpx.HTTPError):
                await client.get(f"http://127.0.0.1:1/feed.pb?key={KEY}")

    assert KEY not in caplog.text


def test_redacted_url_is_what_gets_logged() -> None:
    settings = make_settings(mts_api_key=KEY)
    redacted = settings.redacted_feed_url("vehicle_positions")

    assert KEY not in redacted
    assert "REDACTED" in redacted


def test_json_formatter_emits_one_object_per_line() -> None:
    """launchd appends to a file read later by jq, not by a human watching."""
    from ontime_sd.logging_setup import JsonFormatter

    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="poll ok",
        args=(),
        exc_info=None,
    )
    record.feed = "trip_updates"
    record.rows_written = 412

    line = JsonFormatter().format(record)
    parsed = json.loads(line)

    assert "\n" not in line
    assert parsed["message"] == "poll ok"
    assert parsed["feed"] == "trip_updates"
    assert parsed["rows_written"] == 412
    assert parsed["level"] == "INFO"
    assert parsed["ts"].endswith("+00:00")
