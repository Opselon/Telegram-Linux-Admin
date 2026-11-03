"""Bale adapter lifecycle — the entry point the platform runtime calls.

``run_bale(stop)`` is awaited as a task by ``src.runtime_app``; it must never
raise.  Shape of the flow (verified against python-bale-bot 2.5.0 source):

- ``async with bot`` (``Bot.__aenter__`` → ``_setup_hook``, ``bale/bot.py``
  L104-117) starts the SDK HTTP client (aiohttp session).
- ``await bot.get_me()`` (``bale/bot.py`` L323) validates the token
  server-side; a bad token raises ``bale.InvalidToken``.
- ``bot.updater.start()`` (``bale/updater.py`` L50) runs the long-poll loop
  and dispatches ``on_message``/``on_callback`` events; it exits when
  ``running`` is cleared or the task is cancelled.
- ``bot.close()`` (``bale/bot.py`` L258) stops the updater and closes the
  HTTP client.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

logger = logging.getLogger(__name__)

SHUTDOWN_GRACE_SECONDS = 3.0


async def run_bale(stop: asyncio.Event) -> dict[str, bool]:
    """Runs the Bale adapter until *stop* is set.

    Returns ``{"bale": True}`` on a clean run, ``{"bale": False}`` when the
    adapter is disabled or failed to start (missing/invalid token, missing
    SDK, network failure).  Never raises out of the task.
    """
    try:
        import bale  # noqa: F401 - guards the SDK-dependent imports below
    except ImportError as exc:  # pragma: no cover - environment dependent
        logger.error("Bale adapter disabled: python-bale-bot is not installed (%s)",
                     exc)
        return {"bale": False}

    from .client import bale_token_from_env, create_bot
    from .handlers import register
    from .notifications import wire_notifications

    token = bale_token_from_env()
    if not token:
        logger.error("Bale adapter disabled: BALE_BOT_TOKEN is not set")
        return {"bale": False}

    try:
        bot = create_bot(token)
    except Exception as exc:
        logger.error("Bale adapter failed to initialize: %s: %s",
                     type(exc).__name__, exc)
        return {"bale": False}

    from src.services import get_services

    try:
        async with bot:
            try:
                await bot.get_me()
            except bale.InvalidToken:
                logger.error("Bale adapter disabled: BALE_BOT_TOKEN was "
                             "rejected by Bale (invalid token)")
                return {"bale": False}
            except Exception as exc:
                logger.error("Bale adapter failed to start: %s: %s",
                             type(exc).__name__, exc)
                return {"bale": False}

            get_services()
            register(bot)
            unsubscribe = wire_notifications(bot)
            updater_task = asyncio.create_task(bot.updater.start(),
                                               name="bale-updater")
            logger.info("Bale adapter started (polling)")
            died = False
            try:
                stop_wait = asyncio.create_task(stop.wait())
                done, _pending = await asyncio.wait(
                    {stop_wait, updater_task},
                    return_when=asyncio.FIRST_COMPLETED)
                # A mid-run updater death (e.g. token revoked) is a failure,
                # not a clean stop.
                if updater_task in done and not updater_task.cancelled():
                    died = updater_task.exception() is not None
                    if died:
                        logger.error("Bale polling loop died: %r",
                                     updater_task.exception())
            finally:
                with contextlib.suppress(Exception):
                    await bot.close()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await asyncio.wait_for(asyncio.shield(updater_task),
                                           SHUTDOWN_GRACE_SECONDS)
                updater_task.cancel()
                await asyncio.gather(updater_task, return_exceptions=True)
                with contextlib.suppress(asyncio.CancelledError):
                    stop_wait.cancel()
                    await stop_wait
                unsubscribe()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error("Bale adapter crashed: %s: %s", type(exc).__name__, exc)
        return {"bale": False}
    logger.info("Bale adapter stopped cleanly")
    # A dead polling loop is a failure outcome even though shutdown finished.
    return {"bale": not died}
