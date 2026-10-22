"""Purpose: the single owner of every mutable, patchable runtime dependency.

The 3.1 monolith kept ``ssh_manager``, ``config``, the audit singleton, the
confirmation store and the session dictionaries as module globals, and tests
patch them as ``src.main.<name>``.  3.2 gives them one explicit owner module
and lets every handler read them as ``state.<name>``, so a patch is observed
everywhere and the import graph stays acyclic (this module never imports a
handler module).

OWNER: src/bot/state.py.
CONSUMES: src/config.py, src/audit.py, src/access_control.py,
  src/command_policy.py, src/database.py, src/localization.py,
  src/parse_mode.py.
PROVIDES: conversation states, the SSH manager and health-server handles,
  the config/audit/permission/rate-limit singletons, the pending
  confirmation store, per-user session dictionaries, feature flags, and the
  leaf helpers every handler needs (language lookup, translation, parse
  mode).
INVARIANTS:
  * exactly one instance per process;
  * handlers reach these through this module, never through a copied import;
  * no handler imports here.
EXTEND: add shared state or a leaf helper here, then read it as state.X.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from ..access_control import permissions, rate_limiter
from ..audit import audit
from ..command_policy import CONFIRMATIONS
from ..config import config
from ..database import get_server as get_server_record
from ..database import get_user_language_preference
from ..localization import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES, translate
from ..parse_mode import get_parse_mode

# --- Conversation states (stable ints: PTB persists them in user_data) ---
(
    AWAIT_COMMAND, ALIAS, HOSTNAME, USER,
    AUTH_METHOD, PASSWORD, KEY_PATH, AWAIT_RESTORE_CONFIRMATION,
    AWAIT_RESTORE_FILE, AWAIT_SERVICE_NAME, AWAIT_PACKAGE_NAME, AWAIT_CONTAINER_NAME,
    AWAIT_FILE_PATH, AWAIT_UPLOAD_FILE, AWAIT_PID, AWAIT_FIREWALL_RULE,
) = range(16)

# --- Mutable singletons ----------------------------------------------------
ssh_manager: Any = None
health_server: asyncio.AbstractServer | None = None
user_connections: dict[int, str] = {}
user_sessions: dict[int, Any] = {}
user_language_cache: dict[int, str] = {}
RESTORING: bool = False
SHELL_MODE_USERS: set[int] = set()
DEBUG_MODE: bool = False
MONITORING_TASKS: dict[int, Any] = {}
#: Single-instance guard, written next to the working directory.
LOCK_FILE: Path = Path("bot.lock")
#: membership set for language validation (SUPPORTED_LANGUAGES is a mapping).
SUPPORTED_LANGUAGE_SET = set(SUPPORTED_LANGUAGES)

# --- 3.2 subsystems --------------------------------------------------------
# Created by ``src/bot/process.py`` during startup and read by handlers through
# this module, exactly like ``ssh_manager``, so a test can patch them here.
# They stay ``None`` until then: importing this module must not start a queue
# or read policy files.
#:
#: ``src.jobs.JobQueue`` — serialized, bounded background command execution.
job_queue: Any = None
#: ``src.runtime.TargetResolver`` — maps a server alias to an execution target
#: (``self`` ⇒ the bot host itself, anything else ⇒ pooled SSH).
target_resolver: Any = None
#: ``src.policy.PolicyEngine`` — bounds what an operator profile may run.
policy_engine: Any = None
#: alias -> ``src.policy.Profile`` map loaded at startup (empty when unset).
policy_profiles: dict[str, Any] = {}
#: profile -> engine cache, so deny patterns compile once per profile.
policy_engines: dict[Any, Any] = {}


# --- Leaf helpers ----------------------------------------------------------
def _get_user_language(user_id: int | None) -> str:
    """Returns the cached language preference for a user (DB on first use)."""
    if user_id is None:
        return DEFAULT_LANGUAGE

    cached = user_language_cache.get(user_id)
    if cached:
        return cached

    preference = get_user_language_preference(user_id)
    if preference not in SUPPORTED_LANGUAGE_SET:
        preference = DEFAULT_LANGUAGE

    user_language_cache[user_id] = preference
    return preference


def _translate_for_user(user_id: int | None, key: str, **kwargs: str) -> str:
    """Translates *key* using the user's language preference."""
    return translate(key, _get_user_language(user_id), **kwargs)


def _get_user_parse_mode(user_id: int | None) -> str | None:
    """Returns the parse mode that suits the user's language."""
    return get_parse_mode(_get_user_language(user_id))
