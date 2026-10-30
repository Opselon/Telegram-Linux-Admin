"""PURPOSE: One-tap status panel for the host the bot itself runs on.

OWNER: src/bot/host_status.py (new in 3.5).
CONSUMES: standard library only — ``/proc`` and :mod:`shutil`; psutil is
  used opportunistically when installed.  No Telegram imports beyond the
  handler glue, no SSH connection required.
PROVIDES: :func:`collect_host_snapshot` (pure data) and
  :func:`quick_status` (the ``/status`` command + ``host_status`` callback
  rendering that snapshot through ``ui_kit.kv_panel``).
INVARIANTS:
  * Read-only: never executes processes, never touches the network.
  * Missing ``/proc`` fields degrade to ``n/a`` instead of raising, so the
    panel also renders on macOS/Windows hosts (where most values fall back
    to ``os``/``shutil`` equivalents).
EXTEND: add fields to :func:`collect_host_snapshot`; the panel renders
  whatever pairs come back, so new metrics need no UI change.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from datetime import datetime, timezone

from telegram import InlineKeyboardButton, Update
from telegram.ext import ContextTypes

from ..localization import translate
from . import state
from .support import (
    _answer_callback_query_safely,
    _edit_message_safely,
    _extract_user_id,
    _resolve_message,
    _send_message_safely,
)
from .ui_kit import ICONS, kv_panel

logger = logging.getLogger(__name__)

_PROC_UPTIME = "/proc/uptime"
_PROC_MEMINFO = "/proc/meminfo"
_PROC_LOADAVG = "/proc/loadavg"
_PROC_STAT = "/proc/stat"


def _read_first_line(path: str) -> str:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return handle.readline().strip()
    except OSError:
        return ""


def _meminfo() -> dict[str, int]:
    values: dict[str, int] = {}
    try:
        with open(_PROC_MEMINFO, "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                try:
                    values[key.strip()] = int(rest.strip().split()[0])
                except (ValueError, IndexError):
                    continue
    except OSError:
        pass
    return values


def _cpu_sample() -> tuple[int, int] | None:
    """Returns ``(idle, total)`` jiffies for a two-sample CPU percentage."""
    line = _read_first_line(_PROC_STAT)
    if not line.startswith("cpu"):
        return None
    fields = line.split()[1:]
    try:
        nums = [int(f) for f in fields]
    except ValueError:
        return None
    idle = nums[3] + (nums[4] if len(nums) > 4 else 0)
    return idle, sum(nums)


def _format_uptime(seconds: float) -> str:
    days, rem = divmod(int(seconds), 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def collect_host_snapshot() -> list[tuple[str, str]]:
    """Gathers the host snapshot as ordered ``(label, value)`` pairs."""
    pairs: list[tuple[str, str]] = []

    pairs.append(("Time", datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S")))

    # CPU: two samples 120ms apart for a live percentage; fall back to load.
    sample_a = _cpu_sample()
    if sample_a:
        time.sleep(0.12)
        sample_b = _cpu_sample()
    else:
        sample_b = None
    if sample_a and sample_b:
        idle_delta = sample_b[0] - sample_a[0]
        total_delta = sample_b[1] - sample_a[1]
        cpu_pct = 100.0 * (1 - idle_delta / total_delta) if total_delta > 0 else 0.0
        pairs.append(("CPU", f"{min(max(cpu_pct, 0.0), 100.0):.1f}%"))
    else:
        pairs.append(("CPU", "n/a"))

    # Memory.
    mem = _meminfo()
    if mem.get("MemTotal") and mem.get("MemAvailable") is not None:
        total_kib = mem["MemTotal"]
        avail_kib = mem.get("MemAvailable", 0)
        used_kib = max(total_kib - avail_kib, 0)
        pairs.append(
            (
                "RAM",
                f"{used_kib // 1024} / {total_kib // 1024} MB ({100 * used_kib / total_kib:.0f}%)",
            )
        )
    else:
        pairs.append(("RAM", "n/a"))

    # Swap (best effort).
    if mem.get("SwapTotal"):
        swap_total = mem["SwapTotal"]
        swap_free = mem.get("SwapFree", 0)
        pairs.append(("Swap", f"{(swap_total - swap_free) // 1024} / {swap_total // 1024} MB"))

    # Load average.
    load_line = _read_first_line(_PROC_LOADAVG)
    if load_line:
        pairs.append(("Load", " ".join(load_line.split()[:3])))

    # Disk for the bot's working directory.
    try:
        usage = shutil.disk_usage(os.getcwd())
        pairs.append(
            (
                "Disk",
                f"{usage.used // (1024**3)} / {usage.total // (1024**3)} GB ({100 * usage.used / usage.total:.0f}%)",
            )
        )
    except OSError:
        pairs.append(("Disk", "n/a"))

    # Uptime.
    uptime_line = _read_first_line(_PROC_UPTIME)
    if uptime_line:
        try:
            pairs.append(("Uptime", _format_uptime(float(uptime_line.split()[0]))))
        except ValueError:
            pass

    # Python + pid + boot identity, useful in incident reports.
    pairs.append(("PID", str(os.getpid())))
    pairs.append(
        (
            "Python",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
        )
    )
    return pairs


async def quick_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/status`` + ``host_status`` callback: renders the local host panel."""
    user_id = _extract_user_id(update)
    language = state._get_user_language(user_id) if user_id else None
    snapshot = await asyncio.to_thread(collect_host_snapshot)
    title = f"{ICONS['status']} {translate('status_title', language)}"
    text, markup = kv_panel(title, snapshot)
    refresh_row: tuple[InlineKeyboardButton, ...] = (
        InlineKeyboardButton(
            f"{ICONS['ok']} {translate('status_refresh', language)}",
            callback_data="host_status",
        ),
    )
    markup.inline_keyboard = (refresh_row,) + markup.inline_keyboard

    if update.callback_query:
        await _answer_callback_query_safely(update.callback_query)
        await _edit_message_safely(
            update.callback_query.message,
            text,
            user_id,
            reply_markup=markup,
            preformatted=True,
        )
    else:
        message = _resolve_message(update)
        if message:
            await _send_message_safely(
                message.chat,
                text,
                user_id,
                reply_markup=markup,
                preformatted=True,
            )
