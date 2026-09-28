"""A minimal asyncio HTTP server for the two endpoints this project serves.

Used by the mock feed and by the collector health endpoint. Both need to answer
a handful of GET requests with a body and a status code, and neither needs
routing, validation, or serialization. See DESIGN.md ADR-0011 for why the
collector does not carry a web framework to do this.

This is deliberately not a general HTTP implementation. It handles the request
line, skips headers, and never reads a body.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger(__name__)

_MAX_REQUEST_LINE = 8192

_REASONS = {
    200: "OK",
    400: "Bad Request",
    404: "Not Found",
    500: "Internal Server Error",
    503: "Service Unavailable",
}


@dataclass(frozen=True, slots=True)
class Request:
    method: str
    path: str
    query: dict[str, str]


@dataclass(frozen=True, slots=True)
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "text/plain; charset=utf-8"
    # Extra headers, which override the defaults when they collide. Needed for
    # things only the handler knows, such as Last-Modified, and for HEAD replies
    # that must declare a Content-Length without carrying a body.
    headers: tuple[tuple[str, str], ...] = ()


Handler = Callable[[Request], Awaitable[Response]]


def _render(response: Response) -> bytes:
    reason = _REASONS.get(response.status, "Unknown")

    # Case insensitive merge, so an explicit header replaces the default rather
    # than being sent alongside it.
    fields: dict[str, tuple[str, str]] = {
        "content-type": ("Content-Type", response.content_type),
        "content-length": ("Content-Length", str(len(response.body))),
        "connection": ("Connection", "close"),
    }
    for name, value in response.headers:
        fields[name.lower()] = (name, value)

    lines = [f"HTTP/1.1 {response.status} {reason}"]
    lines += [f"{name}: {value}" for name, value in fields.values()]
    head = "\r\n".join(lines) + "\r\n\r\n"
    return head.encode("latin-1") + response.body


async def _handle_connection(
    handler: Handler, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    try:
        try:
            raw = await reader.readline()
        except (ValueError, asyncio.LimitOverrunError):
            # Request line longer than the stream limit.
            writer.write(_render(Response(status=400, body=b"request line too long")))
            await writer.drain()
            return

        if not raw or len(raw) > _MAX_REQUEST_LINE:
            return

        try:
            method, target, _ = raw.decode("latin-1").split()
        except ValueError:
            writer.write(_render(Response(status=400, body=b"malformed request line")))
            await writer.drain()
            return

        # Read and discard headers up to the blank line so the client does not
        # see a reset before it finishes writing.
        while True:
            line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break

        split = urlsplit(target)
        query = {key: values[0] for key, values in parse_qs(split.query).items()}
        request = Request(method=method.upper(), path=split.path, query=query)

        try:
            response = await handler(request)
        except Exception:
            log.exception("handler failed", extra={"path": request.path})
            response = Response(status=500, body=b"handler error")

        writer.write(_render(response))
        await writer.drain()
    except (ConnectionResetError, BrokenPipeError):
        pass
    finally:
        writer.close()
        # The peer may already be gone, which is normal for a health probe.
        try:
            await writer.wait_closed()
        except (ConnectionResetError, BrokenPipeError):
            pass


async def serve(handler: Handler, port: int, host: str = "127.0.0.1") -> asyncio.Server:
    """Start a server and return it. The caller owns its lifetime."""

    async def on_connect(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_connection(handler, reader, writer)

    return await asyncio.start_server(on_connect, host, port)


def bound_port(server: asyncio.Server) -> int:
    """The port a server actually bound, which matters when port 0 was asked for."""
    return server.sockets[0].getsockname()[1]
