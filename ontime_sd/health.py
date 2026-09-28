"""The collector health endpoint.

GET /healthz returns 200 while at least one feed has succeeded recently, and 503
otherwise. That is the signal a process manager needs: should this process be
restarted. A single failing feed is a data quality problem rather than a reason
to restart, and poll_log already records it per feed. See ADR-0011 and ADR-0022.

Served by the minimal asyncio HTTP helper rather than a web framework, because
the collector is the component that must never stop and should not carry one for
a single parameterless route.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime

from ontime_sd.collector import CollectorState
from ontime_sd.config import Settings
from ontime_sd.tiny_http import Request, Response, bound_port, serve

log = logging.getLogger(__name__)

JSON_CONTENT_TYPE = "application/json"


def build_report(
    state: CollectorState, stale_after_seconds: int, now: datetime | None = None
) -> tuple[bool, dict[str, object]]:
    """Return (healthy, report). Pure, so it can be tested without a socket."""
    now = now or datetime.now(tz=UTC)
    stale = state.is_stale(now, stale_after_seconds)

    feeds: dict[str, object] = {}
    for name, health in sorted(state.feeds.items()):
        age = (
            None
            if health.last_success is None
            else round((now - health.last_success).total_seconds(), 1)
        )
        feeds[name] = {
            "last_success": health.last_success.isoformat() if health.last_success else None,
            "seconds_since_success": age,
            "consecutive_failures": health.consecutive_failures,
            "polls": health.polls,
            "rows_written": health.rows_written,
            # Per feed staleness is reported but does not by itself fail the
            # check. Alerting on it belongs in the runbook, not here.
            "stale": age is None or age > stale_after_seconds,
        }

    report: dict[str, object] = {
        "status": "stale" if stale else "ok",
        "uptime_seconds": round((now - state.started_at).total_seconds(), 1),
        "stale_after_seconds": stale_after_seconds,
        "feeds": feeds,
    }
    return not stale, report


class HealthServer:
    def __init__(self, state: CollectorState, settings: Settings) -> None:
        self.state = state
        self.settings = settings
        self._server = None

    async def handle(self, request: Request) -> Response:
        if request.path not in ("/healthz", "/"):
            return Response(
                status=404,
                body=b'{"error":"not found"}\n',
                content_type=JSON_CONTENT_TYPE,
            )

        healthy, report = build_report(self.state, self.settings.health_stale_after_seconds)
        body = (json.dumps(report, indent=2) + "\n").encode()
        return Response(status=200 if healthy else 503, body=body, content_type=JSON_CONTENT_TYPE)

    async def start(self, port: int | None = None) -> int:
        self._server = await serve(
            self.handle, port if port is not None else self.settings.health_port
        )
        actual = bound_port(self._server)
        log.info("health endpoint listening", extra={"port": actual})
        return actual

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
