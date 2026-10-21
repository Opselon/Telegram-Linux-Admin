"""Purpose: package facade for the bot handlers.

OWNER: src/bot/__init__.py.
CONSUMES: every handler sub-module.
PROVIDES: the public handler surface (imported by src/main.py, the PyInstaller
  entry point and the test suite) so callers never need to know which module a
  handler moved into.
INVARIANTS: re-exports are additive; a name removed here is a breaking change
  for tests and for src/main.py's compatibility shim.  The mutable singletons
  resolve through ``state`` at access time, so patching ``src.main.ssh_manager``
  is visible to every handler.
EXTEND: add a new handler to its module, then re-export it here.
"""

from __future__ import annotations

from . import state
from .application import build_application
from .process import main
from .state import (
    ALIAS,
    AUTH_METHOD,
    AWAIT_COMMAND,
    AWAIT_CONTAINER_NAME,
    AWAIT_FILE_PATH,
    AWAIT_FIREWALL_RULE,
    AWAIT_PACKAGE_NAME,
    AWAIT_PID,
    AWAIT_RESTORE_CONFIRMATION,
    AWAIT_RESTORE_FILE,
    AWAIT_SERVICE_NAME,
    AWAIT_UPLOAD_FILE,
    DEBUG_MODE,
    HOSTNAME,
    KEY_PATH,
    LOCK_FILE,
    MONITORING_TASKS,
    PASSWORD,
    RESTORING,
    SHELL_MODE_USERS,
    SUPPORTED_LANGUAGE_SET,
    USER,
)
from .support import UserSession

__all__ = [
    "state",
    "build_application",
    "main",
    "ssh_manager",
    "health_server",
    "user_connections",
    "user_sessions",
    "user_language_cache",
    "RESTORING",
    "SHELL_MODE_USERS",
    "DEBUG_MODE",
    "LOCK_FILE",
    "MONITORING_TASKS",
    "SUPPORTED_LANGUAGE_SET",
    "UserSession",
    "AWAIT_COMMAND",
    "ALIAS",
    "HOSTNAME",
    "USER",
    "AUTH_METHOD",
    "PASSWORD",
    "KEY_PATH",
    "AWAIT_RESTORE_CONFIRMATION",
    "AWAIT_RESTORE_FILE",
    "AWAIT_SERVICE_NAME",
    "AWAIT_PACKAGE_NAME",
    "AWAIT_CONTAINER_NAME",
    "AWAIT_FILE_PATH",
    "AWAIT_UPLOAD_FILE",
    "AWAIT_PID",
    "AWAIT_FIREWALL_RULE",
]


def __getattr__(name: str):  # PEP 562
    """Resolves the mutable singletons through ``state`` on every access."""
    return getattr(state, name)
