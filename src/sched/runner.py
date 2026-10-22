"""Purpose: executes one scheduled backup run and prunes old archives.

OWNER: lane `backup` of wave 3.4 (branch feat/3.4-backup), package src/sched/.
CONSUMES: :class:`src.sched.models.BackupPlan` / :class:`BackupRun`,
  :data:`src.metrics.METRICS`, stdlib ``pathlib``/``shutil``/``tempfile``.
PROVIDES: :func:`run_backup` (one plan, one run), :func:`archive_dir_for`,
  :func:`rotate_archive_dir` (rotation, also used by "run now" and the tests).

PUBLIC ENTRY CONTRACT (CONTRACT.md lane B #3)
    ``run_cmd`` is injected and must be the *public* archive producer the
    ``/backup`` handler uses — never a private helper of ``src.bot.backup``.
    What exists on base 5bd8aa5: the public callable in ``src.bot.backup`` is
    the handler :func:`src.bot.backup.backup(update, context) -> None`
    (registered in ``src/bot/application.py`` as ``CallbackQueryHandler(
    backup, pattern='^backup$')``).  It is a PTB handler bound to an ``Update``
    and a ``ContextTypes.DEFAULT_TYPE`` context, it builds its ZIP inline and
    it returns ``None`` — it is therefore NOT callable from a scheduler, and
    there is no other public archive-building function in that module.
    This lane must not edit ``src/bot/backup.py`` (file ownership), so the
    runner takes the archive producer as a port with the signature
    ``async (alias: str) -> Path | None`` returning *where the archive was
    written* (``None`` = not produced).  The orchestrator satisfies it by
    extracting the inline block of ``src.bot.backup.backup`` into a public
    ``create_backup_archive(alias) -> Path | None`` and passing that same
    function to both call sites — one implementation, two callers, no
    private internals touched from here.  Until then ``run_cmd`` is supplied
    by the caller and this module stays decoupled.

INVARIANTS
  * never raises into the scheduler loop (hard law L5): every failure path
    returns a :class:`BackupRun` with a terminal status;
  * outcomes: archive moved into ``<base>/<alias>/`` → ``ok``; no archive
    produced or ``run_cmd`` raised → ``retrieval_failed``; a filesystem
    failure while mkdir/move/prune → ``overwrite_failed``;
  * ``detail`` never carries secrets (hard law L2): for a raised
    ``run_cmd`` only the exception *class name* is recorded, for a
    filesystem error only the ``OSError`` text, both capped at 120 chars;
  * on success the archive is renamed to ``<alias>_<YYYYmmdd_HHMMSS>_<uuid8>``
    so two runs in the same second cannot collide, and the directory keeps
    the ``plan.keep`` newest files;
  * metrics: ``sched_backup_total{status=...}`` per outcome and
    ``sched_backup_errors_total{phase=...}`` per error path.
EXTEND: a new outcome needs a constant in ``models.RUN_STATUSES`` first, then
  a branch here, then a test.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Awaitable, Callable

from ..metrics import METRICS
from .models import OK, OVERWRITE_FAILED, RETRIEVAL_FAILED, BackupPlan, BackupRun

logger = logging.getLogger(__name__)

__all__ = ["run_backup", "archive_dir_for", "rotate_archive_dir", "DEFAULT_BASE", "RunCmd"]

#: Default rotation root.  Kept relative on purpose: the bot's working
#: directory is its data root, so this survives a move of the install.
DEFAULT_BASE = Path("data") / "backups" / "scheduled"

#: ``async (alias) -> Path | None``: where the archive landed, or None.
RunCmd = Callable[[str], Awaitable[Path | None]]


def archive_dir_for(alias: str, base: Path | None = None) -> Path:
    """Per-plan archive directory ``<base>/<alias>``. Pure path building."""
    return (base if base is not None else DEFAULT_BASE) / alias


def rotate_archive_dir(plan_dir: Path, keep: int) -> int:
    """Trims ``plan_dir`` to its ``keep`` newest files; returns the count deleted.

    Newest-first by ``(mtime, name)`` so a burst of same-second writes still
    orders deterministically.  A missing directory is not an error (nothing
    stored yet).  Individual deletes that fail are logged and counted, never
    raised (law L5) — the remaining stale files still get pruned.
    """
    if keep < 1:
        raise ValueError("keep must be >= 1")
    if not plan_dir.is_dir():
        return 0
    archives = sorted(
        (entry for entry in plan_dir.iterdir() if entry.is_file()),
        key=lambda entry: (entry.stat().st_mtime, entry.name),
        reverse=True,
    )
    deleted = 0
    for stale in archives[keep:]:
        try:
            stale.unlink()
            deleted += 1
        except OSError:
            logger.warning("could not prune stale backup %s", stale, exc_info=True)
            METRICS.inc("sched_backup_errors_total", phase="prune")
    return deleted


async def run_backup(
    plan: BackupPlan,
    run_cmd: RunCmd,
    *,
    base: Path | None = None,
    now: datetime | None = None,
) -> BackupRun:
    """Runs one scheduled backup for ``plan``; returns its :class:`BackupRun`.

    ``run_cmd`` is the injected public archive producer (see the module
    docstring).  ``base`` overrides the rotation root; ``now`` pins the
    timestamp so tests and callers stay deterministic (no clock read inside
    the assertions path).
    """
    started = now if now is not None else datetime.now()

    try:
        written = await run_cmd(plan.alias)
    except Exception as exc:  # noqa: BLE001 - degrade, log, count (law L5)
        logger.warning("backup run for %s failed: %s", plan.alias, exc, exc_info=True)
        METRICS.inc("sched_backup_total", status=RETRIEVAL_FAILED)
        METRICS.inc("sched_backup_errors_total", phase="retrieve")
        return BackupRun(
            plan_id=plan.id,
            alias=plan.alias,
            started_at=started,
            status=RETRIEVAL_FAILED,
            detail=type(exc).__name__[:120],
        )

    if written is None or not Path(written).is_file():
        logger.warning("backup for %s produced no archive (%r)", plan.alias, written)
        METRICS.inc("sched_backup_total", status=RETRIEVAL_FAILED)
        METRICS.inc("sched_backup_errors_total", phase="retrieve")
        return BackupRun(
            plan_id=plan.id,
            alias=plan.alias,
            started_at=started,
            status=RETRIEVAL_FAILED,
            detail="archive not produced",
        )

    source = Path(written)
    target_dir = archive_dir_for(plan.alias, base)
    staging = Path(tempfile.mkdtemp(prefix="tla_sched_"))
    try:
        suffix = "".join(source.suffixes) or ".zip"
        stamp = started.strftime("%Y%m%d_%H%M%S")
        name = f"{plan.alias}_{stamp}_{uuid.uuid4().hex[:8]}{suffix}"
        # Copy into staging first: a cross-device or half-copied archive must
        # never appear inside the rotation directory and then be pruned as
        # "the oldest good one".
        staged = staging / name
        shutil.copy2(source, staged)
        target_dir.mkdir(parents=True, exist_ok=True)
        final = target_dir / name
        shutil.move(str(staged), str(final))
        # The producer hands ownership of the archive to the runner, so the
        # staging copy it returned must not linger in the host temp dir. Only
        # removed *after* the store succeeded: a failed store keeps it intact.
        try:
            if source.is_file() and source.parent != target_dir:
                source.unlink()
        except OSError:
            logger.info("could not remove staged archive %s", source, exc_info=True)
    except OSError as exc:
        logger.warning("could not store backup for %s: %s", plan.alias, exc, exc_info=True)
        METRICS.inc("sched_backup_total", status=OVERWRITE_FAILED)
        METRICS.inc("sched_backup_errors_total", phase="overwrite")
        return BackupRun(
            plan_id=plan.id,
            alias=plan.alias,
            started_at=started,
            status=OVERWRITE_FAILED,
            detail=str(exc)[:120],
        )
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    deleted = rotate_archive_dir(target_dir, plan.keep)
    logger.info(
        "scheduled backup for %s stored at %s (pruned %d)", plan.alias, final, deleted
    )
    METRICS.inc("sched_backup_total", status=OK)
    return BackupRun(
        plan_id=plan.id,
        alias=plan.alias,
        started_at=started,
        status=OK,
        detail=str(final),
    )
