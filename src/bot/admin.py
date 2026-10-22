"""Purpose: Admin dashboard, audit tail and bot self-update.

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

from . import state

import logging

from telegram import Update
from telegram.ext import ContextTypes

from ..audit import audit
from ..database import get_active_users_count
from ..database import get_database_size
from ..database import get_language_distribution
from ..database import get_plan_distribution
from ..database import get_recent_servers
from ..database import get_servers_added_this_week
from ..database import get_servers_added_today
from ..database import get_servers_per_user_stats
from ..database import get_system_health
from ..database import get_top_users_by_servers
from ..database import get_total_servers
from ..database import get_total_users
from ..database import get_users_joined_today
from ..localization import get_language_label
from ..localization import translate
from ..metrics import METRICS
from ..parse_mode import MessageBuilder
from datetime import datetime
import asyncio
import os
import subprocess
import sys

from .support import _extract_user_id, _send_message_safely

logger = logging.getLogger(__name__)


async def audit_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Shows recent security events. Usage: ``/audit [count]``."""
    user_id = update.effective_user.id
    language = state._get_user_language(user_id)
    args = context.args or []
    limit = 15
    if args and args[0].isdigit():
        limit = max(1, min(int(args[0]), 50))

    records = audit.tail(limit)
    METRICS.inc("audit_views_total")
    audit.record("audit.viewed", user_id=user_id, limit=limit)

    if not records:
        await _send_message_safely(
            update.effective_chat,
            translate("audit_empty", language),
            user_id,
            preformatted=True,
        )
        return

    lines: list[str] = [translate("audit_title", language, count=len(records))]
    for record in records:
        target = record.get("alias") or record.get("context", {}).get("hostname") or "-"
        lines.append(
            "`{ts}`  **{event}**  {outcome}  user=`{uid}`  target=`{target}`".format(
                ts=str(record.get("timestamp", ""))[11:19],
                event=record.get("event", "unknown"),
                outcome=record.get("outcome", "-"),
                uid=record.get("user_id", "-"),
                target=target,
            )
        )
    body = "\n".join(lines)
    if len(body) > 4000:
        body = body[:3990] + "…"
    await _send_message_safely(
        update.effective_chat,
        body,
        user_id,
        preformatted=True,
    )

async def dashboard_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Pro version: Comprehensive admin dashboard showing:
    - Total users and new joins today
    - Total servers and servers added today
    - Plan distribution
    - Language distribution
    - Active users
    - Recent servers
    - Server statistics
    """
    user_id = update.effective_user.id
    parse_mode = state._get_user_parse_mode(user_id)

    # Show loading message
    loading_msg = await _send_message_safely(update.message.chat, "📊 **Loading dashboard...**", user_id, preformatted=True)
    
    try:
        # Gather all statistics in parallel
        (
            total_users, users_today, total_servers, servers_today,
            active_users, plan_dist, lang_dist, recent_servers,
            server_stats, servers_week, top_users, health, db_size
        ) = await asyncio.gather(
            asyncio.to_thread(get_total_users),
            asyncio.to_thread(get_users_joined_today),
            asyncio.to_thread(get_total_servers),
            asyncio.to_thread(get_servers_added_today),
            asyncio.to_thread(get_active_users_count),
            asyncio.to_thread(get_plan_distribution),
            asyncio.to_thread(get_language_distribution),
            asyncio.to_thread(get_recent_servers, 5),
            asyncio.to_thread(get_servers_per_user_stats),
            asyncio.to_thread(get_servers_added_this_week),
            asyncio.to_thread(get_top_users_by_servers, 3),
            asyncio.to_thread(get_system_health),
            asyncio.to_thread(get_database_size)
        )
        
        # Build dashboard message
        builder = MessageBuilder(parse_mode)
        builder.add_text("📊 ")
        builder.add_bold("Admin Dashboard")
        builder.add_line()
        builder.add_line()
        
        # Users Section
        builder.add_text("👥 ")
        builder.add_bold("Users")
        builder.add_line()
        builder.add_text("Total Users: ")
        builder.add_bold(str(total_users))
        builder.add_line()
        builder.add_text("Active Users: ")
        builder.add_bold(str(active_users))
        builder.add_line()
        builder.add_text("New Today: ")
        builder.add_bold(str(users_today))
        builder.add_line()
        builder.add_line()
        
        # Servers Section
        builder.add_text("🖥️ ")
        builder.add_bold("Servers")
        builder.add_line()
        builder.add_text("Total Servers: ")
        builder.add_bold(str(total_servers))
        builder.add_line()
        builder.add_text("Added Today: ")
        builder.add_bold(str(servers_today))
        builder.add_line()
        builder.add_text("Avg per User: ")
        builder.add_bold(str(server_stats["avg"]))
        builder.add_text(" | Max: ")
        builder.add_bold(str(server_stats["max"]))
        builder.add_text(" | Min: ")
        builder.add_bold(str(server_stats["min"]))
        builder.add_line()
        builder.add_line()
        
        # Plan Distribution
        builder.add_text("💎 ")
        builder.add_bold("Plan Distribution")
        builder.add_line()
        if plan_dist:
            for plan, count in sorted(plan_dist.items()):
                builder.add_text(f"• {plan.capitalize()}: ")
                builder.add_bold(str(count))
                builder.add_line()
        else:
            builder.add_text("No data available")
            builder.add_line()
        builder.add_line()
        
        # Language Distribution
        builder.add_text("🌐 ")
        builder.add_bold("Language Distribution")
        builder.add_line()
        if lang_dist:
            # Show top 5 languages
            sorted_langs = sorted(lang_dist.items(), key=lambda x: x[1], reverse=True)[:5]
            for lang, count in sorted_langs:
                lang_label = get_language_label(lang)
                builder.add_text(f"• {lang_label}: ")
                builder.add_bold(str(count))
                builder.add_line()
        else:
            builder.add_text("No data available")
            builder.add_line()
        builder.add_line()
        
        # Recent Servers
        builder.add_text("🆕 ")
        builder.add_bold("Recent Servers")
        builder.add_line()
        if recent_servers:
            for server in recent_servers[:5]:
                builder.add_text("• ")
                builder.add_code(server["alias"])
                builder.add_text(" (")
                builder.add_code(str(server["owner_id"]))
                builder.add_text(")")
                if server.get("created_at"):
                    builder.add_text(" - ")
                    builder.add_text(str(server["created_at"])[:10])
                builder.add_line()
        else:
            builder.add_text("No recent servers")
            builder.add_line()
        
        # Weekly Statistics
        builder.add_line()
        builder.add_text("📈 ")
        builder.add_bold("Weekly Stats")
        builder.add_line()
        builder.add_text("Servers (7 days): ")
        builder.add_bold(str(servers_week))
        builder.add_line()
        builder.add_line()
        
        # Top Users
        if top_users:
            builder.add_text("🏆 ")
            builder.add_bold("Top Users")
            builder.add_line()
            for idx, user in enumerate(top_users, 1):
                builder.add_text(f"{idx}. User ")
                builder.add_code(str(user["owner_id"]))
                builder.add_text(": ")
                builder.add_bold(str(user["server_count"]))
                builder.add_text(" servers")
                builder.add_line()
            builder.add_line()
        
        # System Health
        builder.add_text("💚 ")
        builder.add_bold("System Health")
        builder.add_line()
        if isinstance(health.get("cpu_percent"), (int, float)):
            builder.add_text("CPU: ")
            builder.add_bold(f"{health['cpu_percent']}%")
            builder.add_text(" | Memory: ")
            builder.add_bold(f"{health['memory_percent']}%")
            builder.add_line()
            builder.add_text("Disk: ")
            builder.add_bold(f"{health['disk_percent']}%")
            builder.add_text(" | Free: ")
            builder.add_bold(f"{health['disk_free_gb']} GB")
            builder.add_line()
        else:
            builder.add_text("System metrics unavailable")
            builder.add_line()
        builder.add_line()
        
        # Database Info
        builder.add_text("💾 ")
        builder.add_bold("Database")
        builder.add_line()
        builder.add_text("Size: ")
        builder.add_bold(f"{db_size['size_mb']} MB")
        builder.add_line()
        builder.add_line()
        
        # Footer
        builder.add_line()
        builder.add_text("📅 Last updated: ")
        builder.add_text(datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
        await loading_msg.edit_text(builder.build(), parse_mode=parse_mode)
        
    except Exception as e:
        logger.error(f"Error generating dashboard: {e}", exc_info=True)
        builder = MessageBuilder(parse_mode)
        builder.add_text("❌ ")
        builder.add_bold("Dashboard Error")
        builder.add_line()
        builder.add_line()
        builder.add_text("Error: ")
        builder.add_code(str(e))
        await loading_msg.edit_text(builder.build(), parse_mode=parse_mode)

async def update_bot_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles the bot update process by launching the updater script in a detached process.
    This ensures the update can proceed even after the main bot service is stopped.
    """
    user_id = _extract_user_id(update)
    if update.callback_query:
        await update.callback_query.answer()
        base_message = update.callback_query.message
    else:
        base_message = update.effective_message

    try:
        # Get the path to the current python interpreter and the updater script
        python_executable = sys.executable
        updater_script = os.path.join(os.path.dirname(__file__), 'updater.py')

        # Use Popen to launch the updater in a new, detached process.
        # This allows the updater to outlive the main bot process.
        logger.info("Launching updater script in a detached process.")
        if os.name == 'posix':
            subprocess.Popen([python_executable, updater_script, "--auto"], start_new_session=True)
        else: # For Windows, Popen is detached by default
            subprocess.Popen([python_executable, updater_script, "--auto"])

        await _send_message_safely(
            base_message.chat,
            "✅ **Update process started successfully!**\n\n"
            "The bot will restart shortly to apply the new version. "
            "You can check `updater.log` for progress details.",
            user_id,
            preformatted=True
        )

    except Exception as exc:
        logger.error("Failed to launch the update process: %s", exc, exc_info=True)
        await _send_message_safely(
            base_message.chat,
            f"❌ **Failed to start the update process.**\n\n"
            f"An error occurred: `{exc}`\n"
            "Please check the bot's main log for more details.",
            user_id,
            preformatted=True
        )
