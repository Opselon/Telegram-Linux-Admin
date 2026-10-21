"""Purpose: process entry point — lock file, pre-flight, wiring, polling.

Companion of ``src/bot/application.py``: that module owns the handler graph and
this one owns everything around it (single-instance lock, token/DB/SSH pre-flight,
security configuration, health endpoint, smoke-test mode, graceful shutdown).

OWNER: src/bot/process.py.
CONSUMES: src/bot/application.py, src/bot/state.py, src/config.py,
  src/database.py, src/ssh_manager.py, src/health.py, src/audit.py.
PROVIDES: ``main()`` — the console-script entry point (``tla-bot``).
INVARIANTS: exit codes are stable — 1 for a fatal configuration or init failure,
  0 otherwise; the lock file is always removed on a clean shutdown.
EXTEND: new startup phases go here, not in the handler modules.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from telegram.error import InvalidToken

from ..audit import audit
from ..config import config
from ..database import close_db_connection, initialize_database
from ..health import start_health_server, stop_health_server
from ..metrics import METRICS
from ..ssh_manager import SSHManager
from . import state
from .application import build_application
from .lifecycle import (
    _configure_security,
    _readiness_probe,
    create_lock_file,
    post_init,
    post_shutdown,
    remove_lock_file,
)

logger = logging.getLogger(__name__)


def main() -> None:
    """Initializes and runs the bot (console entry point ``tla-bot``)."""
    if state.LOCK_FILE.exists():
        try:
            pid = int(state.LOCK_FILE.read_text(encoding="utf-8").strip())
            os.kill(pid, 0)
            logger.error(
                "Lock file exists and process %s is running. "
                "Another instance of the bot is likely running.",
                pid,
            )
            sys.exit(1)
        except (OSError, ValueError, ProcessLookupError):
            remove_lock_file()

    create_lock_file()

    # --- Pre-flight Checks --------------------------------------------------
    if not config.telegram_token:
        logger.error("FATAL: Telegram token is not configured. Please run the setup wizard.")
        sys.exit(1)

    _configure_security(config)

    # --- Core Component Initialization --------------------------------------
    try:
        initialize_database()
        state.ssh_manager = SSHManager()
    except Exception as e:
        logger.critical(
            "FATAL: Error during database or SSH manager initialization: %s", e, exc_info=True
        )
        sys.exit(1)

    from ..access_control import permissions

    audit.record("bot.started", outcome="success", access_mode=permissions.mode)
    METRICS.set("ready", 1)

    application = build_application(on_startup=post_init, on_shutdown=post_shutdown)

    logger.info("Bot is starting...")

    if os.environ.get("SMOKE_TEST"):
        # CI smoke mode: serve the health endpoint without contacting Telegram.
        logger.info("Smoke test mode enabled. Bot will not connect to Telegram.")
        asyncio.run(_smoke_run())
    else:
        try:
            application.run_polling()
        except InvalidToken:
            logger.critical(
                "FATAL: The Telegram token is invalid. "
                "Please run the setup wizard again to configure your bot with a valid token."
            )
            sys.exit(1)


async def _smoke_run() -> None:
    """Runs only the health endpoint so a smoke test can probe it."""
    server = await start_health_server(readiness=_readiness_probe)
    try:
        await asyncio.sleep(float(os.environ.get("SMOKE_TEST_SECONDS", "15")))
    finally:
        await stop_health_server(server)
