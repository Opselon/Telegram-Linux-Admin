"""Purpose: Lock file, health endpoint, readiness probe and startup wiring.

OWNER: src/bot/<this module>.
CONSUMES: src/bot/state.py (mutable singletons, conversation states,
  leaf helpers) and the sibling handler modules listed below.
PROVIDES: python-telegram-bot handlers registered by
  src/bot/application.py.
INVARIANTS: handler logic and user-visible behavior are unchanged from
  3.1; only the import graph was made explicit. Mutable runtime state
  is read/written through ``state`` so a patched singleton is seen here.
EXTEND: add a handler in this module, then register it in
  src/bot/application.py.
"""

from __future__ import annotations

import logging

from telegram import BotCommand
from telegram.ext import Application

from ..access_control import OPEN as OPEN_ACCESS_MODE
from ..access_control import permissions
from ..audit import audit
from ..config import config
from ..database import close_db_connection
from ..database import get_all_user_language_preferences
from ..destination_policy import ALLOW_ALL as ALLOW_ALL_POLICY
from ..destination_policy import DESTINATIONS
from ..health import start_health_server
from ..health import stop_health_server
from ..metrics import METRICS
import asyncio
import os

from . import state
from .state import (
    LOCK_FILE, ssh_manager, user_language_cache,
)

logger = logging.getLogger(__name__)


def create_lock_file() -> None:
    """Creates a lock file to prevent multiple instances (2026 standards)."""
    try:
        state.LOCK_FILE.write_text(str(os.getpid()), encoding='utf-8')
        state.LOCK_FILE.chmod(0o600)  # Secure permissions
    except OSError as e:
        logger.error(f"Failed to create lock file: {e}")

def remove_lock_file() -> None:
    """Removes the lock file (2026 standards)."""
    try:
        if state.LOCK_FILE.exists():
            state.LOCK_FILE.unlink()
    except OSError as e:
        logger.warning(f"Failed to remove lock file: {e}")

async def post_shutdown(application: Application) -> None:
    """Gracefully shuts down the SSH manager and database connections."""
    logger.info("Bot is shutting down...")
    remove_lock_file()
    await stop_health_server(state.health_server)
    if state.ssh_manager:
        await state.ssh_manager.close_all_connections()
    close_db_connection()
    audit.record("bot.shutdown")

async def load_languages_into_cache():
    """Loads all user language preferences from the database into the cache."""
    logger.info("Loading user language preferences into cache...")
    try:
        preferences = await asyncio.to_thread(get_all_user_language_preferences)
        state.user_language_cache.update(preferences)
        logger.info(f"Loaded {len(preferences)} language preferences.")
    except Exception as e:
        logger.error(f"Failed to load language preferences: {e}", exc_info=True)

async def _readiness_probe() -> tuple[bool, dict[str, object]]:
    """Readiness check used by the HTTP endpoint (DB, token, SSH pool)."""
    detail: dict[str, object] = {}

    def _probe_db() -> bool:
        from ..database import get_db_connection

        get_db_connection().execute("SELECT 1").fetchone()
        return True

    try:
        detail["database"] = await asyncio.to_thread(_probe_db)
    except Exception as exc:
        detail["database"] = False
        detail["database_error"] = str(exc)[:200]

    detail["token_configured"] = bool(config.telegram_token)
    detail["state.ssh_manager"] = state.ssh_manager is not None
    detail["access_mode"] = permissions.mode
    detail["audit_writable"] = await asyncio.to_thread(audit.is_writable)

    if state.ssh_manager is not None:
        try:
            detail["pool"] = await state.ssh_manager.pool_stats()
        except Exception:
            pass

    ready = (
        bool(detail.get("database"))
        and bool(detail.get("token_configured"))
        and bool(detail.get("audit_writable"))
    )
    return ready, detail

async def _start_health_endpoint() -> None:
    """Starts the optional HTTP health/metrics server."""
    raw_port = os.environ.get("TLA_HEALTH_PORT", "").strip()
    if not raw_port:
        return
    host = os.environ.get("TLA_HEALTH_HOST", "127.0.0.1").strip() or "127.0.0.1"
    try:
        port = int(raw_port)
    except ValueError:
        logger.error("Invalid TLA_HEALTH_PORT=%r; health endpoint disabled", raw_port)
        return
    try:
        state.health_server = await start_health_server(
            port, host, readiness=_readiness_probe
        )
        METRICS.set("ready", 1)
    except OSError as exc:
        logger.error("Could not bind health endpoint on %s:%s: %s", host, port, exc)

async def post_init(application: Application):
    """Actions to run after the bot has been initialized."""
    await load_languages_into_cache()
    await _start_health_endpoint()
    await application.bot.set_my_commands([
        BotCommand("start", "Display the main menu"),
        BotCommand("add_server", "Start the guided process to add a new server"),
        BotCommand("check_updates", "Check for new bot updates"),
        BotCommand("update_bot", "Update the bot to the latest version"),
        BotCommand("debug", "Toggle verbose debug messages"),
        BotCommand("exit_shell", "Terminate an active interactive shell session"),
        BotCommand("cancel", "Cancel the current multi-step operation (like adding a server)"),
        BotCommand("dashboard", "Admin dashboard (admin only)"),
        BotCommand("audit", "Show recent security events (admin only)")
    ])

def _configure_security(cfg) -> None:
    """Applies the configuration to the security singletons.

    Without this call ``permissions`` would stay in its module default and
    ``DESTINATIONS`` would only see environment variables, so a file that
    asked for ``whitelist`` / ``deny_public`` would be silently ignored.
    """
    try:
        mode = permissions.set_mode(cfg.access_mode)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error("Invalid access_mode %r: %s", cfg.access_mode, exc)
        mode = permissions.mode

    permissions.update(
        whitelisted=cfg.whitelisted_users,
        admins=list(cfg.whitelisted_users[:1]),
    )
    if not cfg.whitelisted_users:
        logger.warning(
            "No whitelisted users configured: nobody will be able to use the bot "
            "while access_mode is %r.", mode,
        )

    try:
        DESTINATIONS.reconfigure(cfg.destination_policy, tuple(cfg.allowed_destinations))
    except ValueError as exc:
        logger.error(
            "Invalid destination policy %r (%s); keeping %r",
            cfg.destination_policy, exc, DESTINATIONS.mode,
        )

    if mode == OPEN_ACCESS_MODE:
        logger.warning(
            "SECURITY: access_mode='open' - every Telegram user who finds the bot "
            "can reach the configured servers. Set access_mode to 'whitelist' or "
            "'admin_only' in config.json."
        )
    if DESTINATIONS.mode == ALLOW_ALL_POLICY:
        logger.warning(
            "SECURITY: destination_policy='allow_all' - any host, including public "
            "ones, can be added. Consider 'deny_public' or 'allowlist'."
        )

    for warning in getattr(cfg, "warnings", []) or []:
        logger.warning("Configuration: %s", warning)

    audit.record(
        "security.configured",
        outcome="success",
        access_mode=mode,
        destination_policy=DESTINATIONS.mode,
        whitelisted=len(cfg.whitelisted_users),
        allowlist=len(cfg.allowed_destinations),
    )
    METRICS.set("access_mode_open", 1 if mode == OPEN_ACCESS_MODE else 0)
