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
from ..access_control import SetupEnrollment
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
from ..ops.maintenance import register_defaults
from ..ops.scheduler import Scheduler
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
    """Gracefully shuts down the ops scheduler, SSH manager and DB."""
    logger.info("Bot is shutting down...")
    await _stop_ops_scheduler()
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
    # Public key name: the readiness payload is scraped by monitoring, so it
    # must not leak an internal module path. "transport" says the same thing
    # without naming where the connection manager lives.
    detail["transport"] = state.ssh_manager is not None
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

async def get_deep_readiness() -> tuple[bool, dict[str, object]]:
    """3.3 deep readiness: adds error-scoped checks on top of the HTTP probe."""
    try:
        from ..ops.healthcheck import deep_readiness
    except Exception:  # pragma: no cover - degraded import
        return await _readiness_probe()
    cfg = getattr(state, "config", None)
    try:
        result = await deep_readiness(cfg, audit_logger=audit)
    except Exception:
        logger.exception("deep_readiness failed; falling back to HTTP probe")
        return await _readiness_probe()
    detail: dict[str, object] = {
        f"check_{i}": explanation for i, explanation in enumerate(result.explanations)
    }
    detail["ok"] = result.ok
    return result.ok, detail

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
        BotCommand("jobs", "Show your running and waiting jobs (admin only)"),
        BotCommand("audit", "Show recent security events (admin only)"),
        BotCommand("security", "Security Center: access modes and whitelist (admin only)"),
        BotCommand("status", "Live status of the host running the bot")
    ])

async def _start_ops_scheduler(application: Application) -> None:
    """Registers and starts the 3.3 ops maintenance scheduler.

    Failure here must never block the bot from starting — every default job
    degrades on its own (maintenance results / scheduler error capture), so a
    registration or start failure is logged and skipped instead.
    """
    if getattr(state, "ops_scheduler", None) is not None:
        return
    scheduler = Scheduler()
    queue = getattr(state, "job_queue", None)
    config = getattr(state, "config", None)
    # the lifecycle module already imports the ``audit`` instance directly
    audit_logger = audit
    try:
        await register_defaults(
            scheduler,
            audit_logger=audit_logger,
            config=config,
            queue=queue,
        )
        scheduler.start()
    except Exception:
        logger.exception(
            "ops scheduler registration/start failed; maintenance disabled"
        )
        return
    state.ops_scheduler = scheduler
    METRICS.set("ops_scheduler_up", 1)
    logger.info(
        "ops scheduler up: %s",
        ", ".join(sorted(scheduler.status().keys())),
    )

async def _stop_ops_scheduler() -> None:
    """Stops the maintenance scheduler if one was started."""
    scheduler = getattr(state, "ops_scheduler", None)
    if scheduler is None:
        return
    await scheduler.stop()
    state.ops_scheduler = None
    METRICS.set("ops_scheduler_up", 0)

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
        pin = getattr(cfg, "setup_pin", "") or ""
        if pin:
            state.setup_enrollment = SetupEnrollment(pin)
            logger.warning(
                "Setup PIN detected and no admin configured: the first user to "
                "send '/start <pin>' becomes the admin, then the PIN is burned."
            )
        else:
            state.setup_enrollment = SetupEnrollment("")
            logger.warning(
                "Set TLA_SETUP_PIN (env or config.json 'setup_pin') to enroll the "
                "first admin via '/start <pin>', or add your Telegram user ID to "
                "TLA_ADMIN_ID."
            )
    else:
        state.setup_enrollment = SetupEnrollment("")

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
