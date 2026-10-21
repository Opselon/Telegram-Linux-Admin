"""Zero-dependency HTTP health/metrics endpoint.

Runs on ``TLA_HEALTH_PORT`` (disabled when unset) and speaks just enough
HTTP/1.1 for a load balancer, a ``docker healthcheck`` or Prometheus:

``GET /healthz``
    Liveness.  Always ``200`` while the process can answer.

``GET /readyz``
    Readiness.  ``200`` when the probe (database + configuration) passes,
    ``503`` with the failure detail otherwise.

``GET /metrics``
    Prometheus text exposition of :data:`src.metrics.METRICS` plus uptime.

The server binds to loopback by default so enabling it never exposes the bot
to the internet by accident; set ``TLA_HEALTH_HOST=0.0.0.0`` inside a
container to let the orchestrator scrape it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Awaitable, Callable

from .metrics import METRICS

logger = logging.getLogger(__name__)

#: Seconds since the module was imported, exported as ``tla_uptime_seconds``.
_STARTED_AT = time.monotonic()

ReadinessProbe = Callable[[], "Awaitable[tuple[bool, dict[str, object]]]"]


def _http_response(status: int, body: str, content_type: str) -> bytes:
    reason = {
        200: "OK",
        400: "Bad Request",
        404: "Not Found",
        405: "Method Not Allowed",
        503: "Service Unavailable",
    }.get(status, "OK")
    payload = body.encode("utf-8")
    headers = (
        f"HTTP/1.1 {status} {reason}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(payload)}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("ascii")
    return headers + payload


async def _handle_client(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    readiness: ReadinessProbe | None,
    max_request_bytes: int,
) -> None:
    try:
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            writer.write(_http_response(400, "bad request\n", "text/plain"))
            await writer.drain()
            return

        if len(raw) > max_request_bytes:
            writer.write(_http_response(400, "request too large\n", "text/plain"))
            await writer.drain()
            return

        request_line = raw.split(b"\r\n", 1)[0].decode("latin-1", "replace")
        parts = request_line.split()
        method = parts[0].upper() if parts else ""
        path = parts[1].split("?", 1)[0] if len(parts) > 1 else ""

        if method != "GET":
            writer.write(_http_response(405, "method not allowed\n", "text/plain"))
        elif path in ("/healthz", "/health", "/"):
            writer.write(_http_response(200, json.dumps({"status": "ok"}) + "\n", "application/json"))
        elif path == "/readyz":
            ready, detail = True, {"status": "ready"}
            if readiness is not None:
                try:
                    ready, detail = await asyncio.wait_for(readiness(), timeout=5.0)
                except asyncio.TimeoutError:
                    ready, detail = False, {"status": "timeout"}
                except Exception as exc:  # pragma: no cover - defensive
                    ready, detail = False, {"status": "error", "error": str(exc)}
            detail = {"status": "ready" if ready else "not_ready", **detail}
            writer.write(
                _http_response(
                    200 if ready else 503,
                    json.dumps(detail) + "\n",
                    "application/json",
                )
            )
        elif path == "/metrics":
            METRICS.set("uptime_seconds", round(time.monotonic() - _STARTED_AT, 3))
            writer.write(_http_response(200, METRICS.render(), "text/plain; version=0.0.4"))
        else:
            writer.write(_http_response(404, "not found\n", "text/plain"))

        await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    except Exception:  # pragma: no cover - never let the health server crash the bot
        logger.warning("Health endpoint error", exc_info=True)
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _env_port() -> int | None:
    raw = os.environ.get("TLA_HEALTH_PORT", "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.error("Invalid TLA_HEALTH_PORT=%r; health endpoint disabled", raw)
        return None


async def start_health_server(
    port: int | None = None,
    host: str | None = None,
    *,
    readiness: ReadinessProbe | None = None,
    max_request_bytes: int = 8192,
) -> asyncio.AbstractServer | None:
    """Starts the health server and returns the underlying asyncio server.

    Both ``port`` and ``host`` default to the environment
    (``TLA_HEALTH_PORT`` / ``TLA_HEALTH_HOST``); when no port is configured the
    endpoint stays disabled and ``None`` is returned so callers can treat it as
    a no-op.
    """

    if port is None:
        port = _env_port()
    if port is None:
        return None
    if host is None:
        host = os.environ.get("TLA_HEALTH_HOST", "127.0.0.1").strip() or "127.0.0.1"

    handler = lambda reader, writer: _handle_client(  # noqa: E731 - tiny adapter
        reader, writer, readiness, max_request_bytes
    )
    server = await asyncio.start_server(handler, host=host, port=port)
    METRICS.set("health_server_up", 1)
    addresses = ", ".join(str(sock.getsockname()) for sock in (server.sockets or []))
    logger.info("Health endpoint listening on %s", addresses or f"{host}:{port}")
    return server


async def stop_health_server(server: asyncio.AbstractServer | None) -> None:
    """Stops a server returned by :func:`start_health_server`."""
    if server is None:
        return
    server.close()
    try:
        await server.wait_closed()
    except Exception:  # pragma: no cover
        pass
    METRICS.set("health_server_up", 0)
