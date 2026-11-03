"""Unified platform runtime — one asyncio process, three adapters.

Components (in startup order):
1. Security configuration + database (existing lifecycle).
2. ``SSHManager`` + ``JobQueue`` + services (the shared management core).
3. FastAPI web backend (uvicorn in-process; default loopback).
4. Bale polling (only when ``BALE_BOT_TOKEN`` is configured and the SDK extra
   is installed).
5. Telegram polling (only when a Telegram token is configured) — unchanged
   standalone behavior when web/Bale are absent.

Any single adapter failing to start degrades alone: the others keep running
and readiness reflects the failure. ``tla-bot`` remains the Telegram-only
entry point; ``tla-platform`` is the full platform.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal

from . import database
from .bot.integration import configure_observability
from .bot.lifecycle import _configure_security
from .config import config
from .observability import bind_context, log_event

logger = logging.getLogger(__name__)


def _web_enabled() -> bool:
    return os.environ.get("TLA_WEB_ENABLED", "1") != "0"


def _bale_token() -> str:
    return os.environ.get("BALE_BOT_TOKEN", "").strip()


def _telegram_token() -> str:
    token = os.environ.get("TLA_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN") or ""
    if not token:
        with contextlib.suppress(Exception):
            token = str(getattr(config, "bot_token", "") or "")
    return token.strip()


async def _run_web(stop: asyncio.Event) -> dict[str, bool]:
    """Runs the FastAPI app under uvicorn until *stop* is set. Returns health."""
    import uvicorn

    from .api import create_app
    from .api.events import wire_notifications
    from .services import build_services, get_services, set_services

    try:
        get_services()
    except RuntimeError:
        set_services(build_services())
    wire_notifications()

    uvi_config = uvicorn.Config(
        create_app(),
        host=os.environ.get("TLA_WEB_HOST", "127.0.0.1"),
        port=int(os.environ.get("TLA_WEB_PORT", "8080")),
        log_level="warning",
        lifespan="on",
    )
    server = uvicorn.Server(uvi_config)
    # The runtime owns the loop and signals; uvicorn must not touch them
    # (Server.serve() enters self.capture_signals()).
    server.capture_signals = _null_signals  # type: ignore[method-assign]
    task = asyncio.create_task(server.serve(), name="web-server")
    await stop.wait()
    server.should_exit = True
    with contextlib.suppress(asyncio.TimeoutError):
        await asyncio.wait_for(task, timeout=10)
    return {"web": True}


@contextlib.contextmanager
def _null_signals():  # noqa: ANN202 - uvicorn Server.capture_signals override
    yield


async def _run_bale(stop: asyncio.Event) -> dict[str, bool]:
    """Runs the Bale adapter until *stop* is set."""
    from .adapters.bale.lifecycle import run_bale

    return await run_bale(stop)


async def _run_telegram(stop: asyncio.Event) -> dict[str, bool]:
    """Runs the existing Telegram bot until *stop* is set."""
    from telegram.error import InvalidToken
    from telegram.ext import ApplicationBuilder

    from .bot.application import build_application
    from .bot.lifecycle import post_init, post_shutdown

    application = build_application(on_startup=post_init, on_shutdown=post_shutdown)
    await application.initialize()
    await application.start()
    updater = application.updater
    if updater is None:
        raise RuntimeError("Telegram updater unavailable")
    await updater.start_polling()
    await stop.wait()
    await updater.stop()
    await application.stop()
    await application.shutdown()
    return {"telegram": True}


async def run_platform() -> int:
    """Starts every configured adapter; returns a process exit code."""
    configure_observability()
    bind_context(component="platform")

    token = _telegram_token()
    bale_token = _bale_token()
    if not token and not bale_token and not _web_enabled():
        logger.error("nothing to run: no Telegram token, no Bale token, web disabled")
        return 1
    if token:
        try:
            _configure_security(config)
        except Exception:
            logger.exception("security configuration failed")
            return 1
        database.initialize_database()
        from .services.migrations import run_migrations

        run_migrations()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in ("SIGINT", "SIGTERM"):
        with contextlib.suppress(NotImplementedError, AttributeError):
            loop.add_signal_handler(getattr(signal, sig), stop.set)

    tasks: dict[str, asyncio.Task] = {}
    if _web_enabled():
        tasks["web"] = asyncio.create_task(_run_web(stop), name="adapter-web")
    if bale_token:
        tasks["bale"] = asyncio.create_task(_run_bale(stop), name="adapter-bale")
    if token:
        tasks["telegram"] = asyncio.create_task(_run_telegram(stop), name="adapter-telegram")

    if not tasks:
        logger.error("no adapter started")
        return 1

    log_event(logger, "platform.started", adapters=sorted(tasks))
    results: dict[str, bool] = {}
    try:
        # Wait for either the stop signal or any adapter to die unexpectedly.
        done, pending = await asyncio.wait(
            set(tasks.values()) | {asyncio.create_task(stop.wait())},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for name, task in tasks.items():
            if task in done:
                try:
                    results.update(task.result())
                except Exception:
                    logger.exception("adapter %s failed", name)
                    results[name] = False
            elif not task.done() and stop.is_set():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
                results[name] = True
    finally:
        stop.set()
        for task in tasks.values():
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
        from .services import get_services

        with contextlib.suppress(Exception):
            await get_services().manager.close_all_connections()
        with contextlib.suppress(Exception):
            await get_services().queue.stop()
        database.close_db_connection()
    log_event(logger, "platform.stopped", results=results)
    return 0


def main() -> None:
    try:
        code = asyncio.run(run_platform())
    except KeyboardInterrupt:
        code = 0
    raise SystemExit(code)


if __name__ == "__main__":
    main()
