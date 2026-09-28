"""Mock GTFS-Realtime feed server.

Serves the same URL shape as MTS so that mock and production differ only by
MTS_FEED_BASE_URL, including the key query parameter. See DESIGN.md ADR-0012.

Failure injection is the point of this, not an extra. A feed that only ever
behaves correctly cannot validate a collector whose main job is surviving a feed
that does not. Four failure modes are supported: HTTP 500, a slow response,
truncated protobuf, and a frozen header timestamp.

Random rates come from the environment for soak testing. Tests instead force a
specific failure with a query parameter, so no test depends on randomness:

    ?fail=500       respond with that status code
    ?slow=2.5       sleep that many seconds before responding
    ?truncate=1     return a deliberately corrupt partial payload
    ?frozen=1       pin the header timestamp to the feed epoch
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import random
import signal
from datetime import UTC, datetime, timedelta

from google.protobuf import text_format

from ontime_sd.config import TRIP_UPDATES, VEHICLE_POSITIONS, Settings
from ontime_sd.logging_setup import configure_logging
from ontime_sd.mock.simulator import Simulator
from ontime_sd.tiny_http import Request, Response, bound_port, serve

log = logging.getLogger(__name__)

PROTOBUF_CONTENT_TYPE = "application/x-protobuf"

# Our internal feed name, keyed by the MTS path segment.
_PATH_FEEDS = {
    "vehicle-positions-for-agency": VEHICLE_POSITIONS,
    "trip-updates-for-agency": TRIP_UPDATES,
}


def _parse_path(path: str) -> tuple[str, bool] | None:
    """Return (feed, text_format) for a feed path, or None if it is not one.

    Expected shape: /api/api/gtfs_realtime/<segment>/MTS.pb
    """
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return None

    filename = parts[-1]
    segment = parts[-2]

    feed = _PATH_FEEDS.get(segment)
    if feed is None:
        return None

    if filename == "MTS.pb":
        return feed, False
    if filename == "MTS.pbtext":
        return feed, True
    return None


class MockFeedServer:
    def __init__(self, settings: Settings, simulator: Simulator | None = None) -> None:
        self.settings = settings
        self.simulator = simulator or Simulator(vehicle_count=settings.mock_vehicle_count)
        self._random = random.Random(20260927)
        self.request_count = 0
        self._server: asyncio.Server | None = None

    # --- feed timestamp ---

    def feed_timestamp(self, now: datetime, *, frozen: bool = False) -> datetime:
        """Quantize the header timestamp to the refresh interval.

        Real agencies publish on their own cadence, so polling faster than that
        cadence legitimately sees the same header twice. Quantizing reproduces
        that, which is what exercises the collector's skip path.
        """
        if frozen:
            return self.simulator.epoch

        period = self.settings.mock_feed_refresh_seconds
        epoch = self.simulator.epoch
        elapsed = int((now - epoch).total_seconds())
        return epoch + timedelta(seconds=(elapsed // period) * period)

    # --- failure injection ---

    def _forced_failure(self, request: Request) -> Response | None:
        code = request.query.get("fail")
        if code:
            try:
                status = int(code)
            except ValueError:
                return Response(status=400, body=b"fail must be an integer status code")
            return Response(status=status, body=b"injected failure")
        return None

    def _roll(self, rate: float) -> bool:
        return rate > 0.0 and self._random.random() < rate

    # --- request handling ---

    async def handle(self, request: Request) -> Response:
        self.request_count += 1

        if request.method not in ("GET", "HEAD"):
            return Response(status=400, body=b"only GET is supported")

        if request.path in ("/", "/healthz"):
            return Response(body=b"mock feed ok\n")

        parsed = _parse_path(request.path)
        if parsed is None:
            return Response(status=404, body=b"no such feed")
        feed, as_text = parsed

        # A real key is not required, but if the collector is configured with
        # one it must actually arrive, so the auth path is exercised before the
        # real key exists.
        if self.settings.mts_api_key and request.query.get("key") != self.settings.mts_api_key:
            return Response(status=401, body=b"missing or wrong key")

        forced = self._forced_failure(request)
        if forced is not None:
            return forced
        if self._roll(self.settings.mock_failure_rate):
            return Response(status=500, body=b"injected random failure")

        slow = request.query.get("slow")
        if slow is not None:
            with contextlib.suppress(ValueError):
                await asyncio.sleep(float(slow))
        elif self._roll(self.settings.mock_slow_rate):
            await asyncio.sleep(self.settings.mock_slow_seconds)

        now = datetime.now(tz=UTC)
        frozen = request.query.get("frozen") == "1"
        feed_ts = self.feed_timestamp(now, frozen=frozen)

        if feed == VEHICLE_POSITIONS:
            message = self.simulator.vehicle_positions(now, feed_timestamp=feed_ts)
        else:
            message = self.simulator.trip_updates(
                now, feed_timestamp=feed_ts, style=self.settings.mock_trip_update_style
            )

        if as_text:
            body = text_format.MessageToString(message).encode()
            return Response(body=body, content_type="text/plain; charset=utf-8")

        payload = message.SerializeToString()

        truncate = request.query.get("truncate") == "1" or self._roll(
            self.settings.mock_truncate_rate
        )
        if truncate:
            # Cut mid message so the bytes are a valid prefix but not a valid
            # message, which is the realistic shape of a truncated response.
            payload = payload[: max(len(payload) // 2, 1)]

        return Response(body=payload, content_type=PROTOBUF_CONTENT_TYPE)

    # --- lifecycle ---

    async def start(self, port: int | None = None) -> int:
        self._server = await serve(
            self.handle, port if port is not None else self.settings.mock_port
        )
        actual = bound_port(self._server)
        log.info(
            "mock feed listening",
            extra={
                "port": actual,
                "vehicles": self.simulator.vehicle_count,
                "trip_update_style": self.settings.mock_trip_update_style,
                "feed_refresh_seconds": self.settings.mock_feed_refresh_seconds,
            },
        )
        return actual

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def base_url(self, port: int) -> str:
        return f"http://127.0.0.1:{port}/api/api/gtfs_realtime"


async def run(settings: Settings | None = None) -> None:
    settings = settings or Settings.from_env()
    server = MockFeedServer(settings)
    port = await server.start()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    print(f"mock feed on http://127.0.0.1:{port} (ctrl-c to stop)")
    try:
        await stop.wait()
    finally:
        await server.stop()
        log.info("mock feed stopped", extra={"requests_served": server.request_count})


def main() -> None:
    settings = Settings.from_env()
    configure_logging(settings.log_level)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(run(settings))
