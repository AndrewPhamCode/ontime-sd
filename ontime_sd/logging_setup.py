"""Structured JSON logging, one object per line.

The collector runs unattended under launchd, so its output is read later by
grep and by jq rather than by a human watching a terminal. One JSON object per
line makes that possible without a log parser.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime

# Attributes LogRecord always carries. Anything else came from extra= and is
# merged into the JSON object as a field.
_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "taskName",
        "thread",
        "threadName",
    }
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)

        return json.dumps(payload, default=str)


# httpx and httpcore log every request at INFO, including the full URL. The feed
# URL carries the MTS API key as a query parameter, so at INFO those libraries
# would write the key in plaintext to the collector log on disk, on every poll,
# forever. Our own poll lines log the redacted URL instead.
_SILENCED_LOGGERS = ("httpx", "httpcore")


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level)

    for name in _SILENCED_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
