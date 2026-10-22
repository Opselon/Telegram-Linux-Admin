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
from ..ops.maintenance import register_defaults
from ..ops.scheduler import Scheduler
from ..database import register_extra_schema


def _register_feature_schema() -> None:
    """Registers 3.4 feature-table schema once, idempotently."""
    from ..alerts.storage import SCHEMA_INDEX_SQL as _alert_idx
    from ..alerts.storage import SCHEMA_SQL as _alert_schema
    from ..sched.storage import SCHEMA_SQL as _sched_schema
    register_extra_schema([_alert_schema, _sched_schema, _alert_idx])


_register_feature_schema()
import asyncio
import os
from pathlib import Path

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
    state.application = application
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
        BotCommand("alerts", "Manage usage alert rules per server (admin only)"),
        BotCommand("backups", "Schedule and run automatic backups (admin only)"),
        BotCommand("audit", "Show recent security events (admin only)")
    ])

async def _alerts_probe(rule):
    """Reads the configured metric for one rule; None = target unreachable.

    Local-host rules read through :func:`src.database.get_system_health`;
    remote rules are skipped (None) until an SSH probe is wired, so a sweep
    never hangs the scheduler on an unreachable box.
    """
    from ..database import get_system_health

    alias_self = getattr(state, "SELF_ALIAS", "self")
    if rule.alias not in (alias_self, "self", "localhost"):
        METRICS.inc("alerts_probe_skips_total", kind=rule.kind, reason="remote")
        return None
    try:
        health = await asyncio.to_thread(get_system_health)
    except Exception:
        logger.exception("alerts probe failed for kind %s", rule.kind)
        return None
    field = {
        "mem_high": "memory_percent",
        "disk_high": "disk_percent",
        "cpu_high": "cpu_percent",
    }.get(rule.kind)
    if field is None:
        return None
    value = health.get(field)
    return float(value) if isinstance(value, (int, float)) else None


async def _alerts_sweep() -> None:
    """One alerts pass over every owner's enabled rules."""
    from ..alerts.checker import check_rules
    from ..alerts.notifier import notify
    from ..alerts.storage import list_rules
    from ..database import get_whitelisted_users

    owners: list[int] = []
    try:
        owners = [
            int(uid) for uid in await asyncio.to_thread(get_whitelisted_users)
        ]
    except Exception:
        logger.exception("alerts sweep could not enumerate owners")
        return

    for owner in owners:
        try:
            rules = list_rules(owner, None, True)
        except Exception:
            logger.exception("alerts sweep could not read rules for %s", owner)
            continue
        if not rules:
            continue
        events = await check_rules(rules, _alerts_probe)
        for event in events:
            await notify(event, sender=_telegram_alert_sender)


async def _telegram_alert_sender(event) -> bool:
    """Best-effort Telegram delivery for one alert event."""
    from ..alerts.notifier import render_event_text

    app = getattr(state, "application", None)
    if app is None:
        return False
    try:
        await app.bot.send_message(
            chat_id=event.owner_id,
            text=render_event_text(event),
            parse_mode="Markdown",
        )
        return True
    except Exception:
        logger.exception("alert delivery failed for owner %s", event.owner_id)
        return False


async def _sched_backup_tick() -> None:
    """Runs due backup plans once (daily cadence driven by the scheduler)."""
    from ..sched.runner import run_backup
    from ..sched.schedule import should_run
    from ..sched.storage import list_enabled_plans
    from ..sched.ui import get_run_cmd
    from datetime import datetime as _dt

    run_cmd = get_run_cmd()
    if run_cmd is None:
        return
    try:
        plans = await asyncio.to_thread(list_enabled_plans)
    except Exception:
        logger.exception("scheduled backups could not list plans")
        return
    now = _dt.now()
    for plan in plans:
        if not plan.enabled or not should_run(plan, now, None):
            continue
        await run_backup(plan, run_cmd)


def _wire_backup_run_cmd() -> None:
    """Points the sched UI/runner at the schedulable backup entry."""
    from ..bot.backup import create_backup_archive
    from ..sched.ui import set_backup_base, set_run_cmd

    async def _run(alias: str):
        base = set_backup_base
        out_dir = Path("data") / "backups" / "scheduled" / alias
        out_dir.mkdir(parents=True, exist_ok=True)
        path = await create_backup_archive(destination_dir=out_dir)
        return Path(path)

    set_run_cmd(_run)


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
        # 3.4 feature cadences: alert sweep every 5 min; backup plan tick
        # every 15 min (should_run gates each plan to once per calendar day).
        scheduler.add("alerts", 300.0, _alerts_sweep)
        scheduler.add("backups", 900.0, _sched_backup_tick)
        _wire_backup_run_cmd()
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
