"""Purpose: backward-compatible entry point.

The bot lived in this one file from 1.0 through 3.1 and grew to 3779 lines.
3.2 moved every handler into the ``src/bot`` package (one module per feature
area) and left this module as a thin facade so existing deployments, the
PyInstaller entry point (``packaging/pyinstaller_entry.py``), the systemd unit
and the test suite that imports ``src.main.<name>`` keep working unchanged.

OWNER: src/bot (this shim is owned by the package owners; do not add logic here).
CONSUMES: src/bot (all handler modules), src/config.py.
PROVIDES: ``main()`` and a re-export of every handler the 3.1 module defined.
INVARIANTS: a name that existed in 3.1's src/main.py must remain importable from
  here; tests and external scripts depend on it.
EXTEND: nothing — new code belongs in src/bot/<feature>.py.
"""

from __future__ import annotations

# src/bot/__init__.py re-exports the state singletons, the conversation states
# and ``main``; pulling the handler names in below completes the 3.1 surface.
from .bot import *  # noqa: F401,F403
from .bot import main  # noqa: F401

from .access_control import permissions, rate_limiter
from .audit import audit
from .command_policy import CONFIRMATIONS, Risk, classify, validate_command_text
from .config import config
from .database import get_server as get_server_record
from .localization import translate

# Handler re-exports keep ``from src.main import <handler>`` working for the
# test suite and for anyone scripting against the module.
from .bot.admin import audit_command, dashboard_command, update_bot_command
from .bot.backup import (
    backup,
    cancel_restore,
    restore_confirmation,
    restore_file,
    restore_start,
)
from .bot.commands import (
    cancel_command_callback,
    cancel_command_confirmation,
    confirm_command_callback,
    execute_command,
    run_command_start,
)
from .bot.docker import (
    cancel_docker_action,
    docker_action_start,
    docker_management_menu,
    docker_ps,
    execute_docker_action,
)
from .bot.errors import error_handler
from .bot.files import (
    cancel_file_manager_action,
    download_file,
    file_manager_action_start,
    file_manager_dispatch,
    file_manager_menu,
    list_files,
    upload_file,
    upload_file_path,
)
from .bot.firewall import (
    cancel_firewall_action,
    execute_firewall_action,
    firewall_action_start,
    firewall_management_menu,
    firewall_status,
)
from .bot.lifecycle import (
    _configure_security,
    _readiness_probe,
    _start_health_endpoint,
    create_lock_file,
    load_languages_into_cache,
    post_init,
    post_shutdown,
    remove_lock_file,
)
from .bot.monitoring import (
    get_disk_usage,
    get_network_info,
    get_open_ports,
    get_resource_usage,
    get_static_info,
    live_monitoring,
    server_status_menu,
    stop_live_monitoring,
)
from .bot.navigation import language_menu, main_menu, set_language, start
from .bot.packages import (
    cancel_install_package,
    execute_install_package,
    install_package_start,
    package_management_menu,
    package_manager_action,
)
from .bot.processes import (
    cancel_kill_process,
    execute_kill_process,
    kill_process_start,
    list_processes,
    process_management_menu,
)
from .bot.servers import (
    add_server_start,
    auth_method_invalid_input,
    cancel_add_server,
    connect_server_menu,
    disconnect,
    get_alias,
    get_auth_method,
    get_hostname,
    get_key_path,
    get_password,
    get_user,
    handle_server_connection,
    remove_server_confirm,
    remove_server_menu,
    save_server,
)
from .bot.services import (
    cancel_service_action,
    execute_service_action,
    service_action_start,
    service_management_menu,
)
from .bot.shell import exit_shell, handle_shell_command, start_shell_session
from telegram.ext import ConversationHandler

import os  # noqa: F401  (tests patch src.main.os.*)
import subprocess  # noqa: F401  (tests patch src.main.subprocess)
import sys  # noqa: F401  (tests patch src.main.sys)
import types
import zipfile  # noqa: F401  (tests patch src.main.zipfile)

from .bot.support import (
    DashboardStats,
    ServerInfo,
    UserSession,
    _answer_callback_query_safely,
    _build_language_keyboard,
    _edit_message_safely,
    _extract_user_id,
    _host_key_mismatch_message,
    _resolve_message,
    _send_connection_error,
    _send_message_safely,
    admin_authorized,
    authorized,
    log_structured,
    rate_limit,
    send_debug_message,
    toggle_debug_mode,
)
from .bot.system import (
    confirm_system_command,
    execute_system_command,
    system_commands_menu,
)
from .bot import state as _state

#: Names 3.2 centralises in ``state``; ``__setattr__`` forwards writes there so
#: ``patch("src.main.<name>", ...)`` is visible to every handler module.
_MUTABLE = frozenset(
    {
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
        "rate_limiter",
        "permissions",
        "audit",
        "config",
        "translate",
        "_translate_for_user",
        "get_server_record",
    }
)


class _CompatModule(types.ModuleType):
    """Module class that mirrors ``state`` for 3.1-era patch targets."""

    def __getattr__(self, name: str):
        try:
            return getattr(_state, name)
        except AttributeError:
            raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from None

    def __setattr__(self, name: str, value) -> None:
        if name in _MUTABLE and hasattr(_state, name):
            setattr(_state, name, value)
        super().__setattr__(name, value)


#: Rebinding ``__class__`` is the documented way to give a module custom
#: attribute semantics; PEP 562 ``__getattr__`` alone cannot observe setattr.
sys.modules[__name__].__class__ = _CompatModule

if __name__ == "__main__":
    main()
