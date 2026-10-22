"""Scheduled automatic backups (wave 3.4, lane B).

Public surface of the package, re-exported so the orchestrator can wire it:

* :class:`BackupPlan` / :class:`BackupRun` — validated value objects;
* :func:`next_run` / :func:`should_run` — pure datetime helpers;
* :func:`run_backup` — one execution through the injected archive producer;
* :data:`SCHEMA_SQL` and the plan CRUD — storage the orchestrator boots;
* the ``/backups`` handlers — admin-only Telegram UI.

Nothing here is imported by ``src/main.py`` yet: registration in
``application.py``/``lifecycle.py`` is orchestrator work, so importing this
package must stay side-effect free.
"""

from __future__ import annotations

from .models import OK, OVERWRITE_FAILED, RETRIEVAL_FAILED, RUN_STATUSES, BackupPlan, BackupRun
from .runner import archive_dir_for, rotate_archive_dir, run_backup
from .schedule import next_run, should_run, slot_for
from .storage import SCHEMA_SQL, add_plan, delete_plan, get_plan, list_plans, set_plan

__all__ = [
    "BackupPlan",
    "BackupRun",
    "OK",
    "OVERWRITE_FAILED",
    "RETRIEVAL_FAILED",
    "RUN_STATUSES",
    "SCHEMA_SQL",
    "add_plan",
    "archive_dir_for",
    "delete_plan",
    "get_plan",
    "list_plans",
    "next_run",
    "rotate_archive_dir",
    "run_backup",
    "set_plan",
    "should_run",
    "slot_for",
]
