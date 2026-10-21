"""Purpose: builds the python-telegram-bot Application and registers
every handler in one reviewable place (extracted from the 3.1 monolith).

OWNER: src/bot/application.py.
CONSUMES: every handler module in src/bot, src/bot/state.py, src/config.py.
PROVIDES: build_application(), owning builder timeouts, the error handler
  and the full ConversationHandler/CallbackQuery/Command graph.
INVARIANTS: registration order, and every callback pattern, are exactly the
  ones that shipped in 3.1 — those patterns are the wire format the buttons
  already sitting in users' chats still carry, so they must never be
  "cleaned up".
EXTEND: register new handlers here; keep ConversationHandlers ahead of any
  CallbackQueryHandler that shares a prefix with them.
"""

from __future__ import annotations

import logging

from telegram.ext import (
    Application, CallbackQueryHandler, CommandHandler,
    ConversationHandler, MessageHandler, filters,
)

from ..config import config
from ..access_control import OPEN as OPEN_ACCESS_MODE

from . import state
from .state import (
    ALIAS, AUTH_METHOD, AWAIT_COMMAND, AWAIT_CONTAINER_NAME, AWAIT_FILE_PATH, AWAIT_FIREWALL_RULE, AWAIT_PACKAGE_NAME, AWAIT_PID, AWAIT_RESTORE_CONFIRMATION, AWAIT_RESTORE_FILE, AWAIT_SERVICE_NAME, AWAIT_UPLOAD_FILE, HOSTNAME, KEY_PATH, PASSWORD, USER,
)

from .admin import dashboard_command, update_bot_command
from .backup import (
    backup, cancel_restore, restore_confirmation, restore_file,
    restore_start,
)
from .commands import cancel_command_callback, execute_command, run_command_start
from .docker import (
    cancel_docker_action, docker_action_start, docker_management_menu, docker_ps,
    execute_docker_action,
)
from .errors import error_handler
from .files import (
    cancel_file_manager_action, file_manager_action_start, file_manager_dispatch, file_manager_menu,
    upload_file,
)
from .firewall import (
    cancel_firewall_action, execute_firewall_action, firewall_action_start, firewall_management_menu,
    firewall_status,
)
from .lifecycle import post_init, post_shutdown
from .monitoring import (
    get_disk_usage, get_network_info, get_open_ports, get_resource_usage,
    get_static_info, live_monitoring, server_status_menu, stop_live_monitoring,
)
from .navigation import language_menu, main_menu, set_language, start
from .packages import (
    cancel_install_package, execute_install_package, install_package_start, package_management_menu,
    package_manager_action,
)
from .processes import (
    cancel_kill_process, execute_kill_process, kill_process_start, list_processes,
    process_management_menu,
)
from .servers import (
    add_server_start, auth_method_invalid_input, cancel_add_server, connect_server_menu,
    disconnect, get_alias, get_auth_method, get_hostname,
    get_key_path, get_password, get_user, handle_server_connection,
    remove_server_confirm, remove_server_menu,
)
from .services import (
    cancel_service_action, execute_service_action, service_action_start, service_management_menu,
)
from .shell import exit_shell, start_shell_session
from .support import toggle_debug_mode
from .system import confirm_system_command, execute_system_command, system_commands_menu

logger = logging.getLogger(__name__)


def build_application(*, post_init=None, post_shutdown=None) -> Application:
    """Returns a fully wired Application; the caller starts polling."""
    # Configure request timeout to prevent ReadTimeout errors
    # Default timeout is 5 seconds, increasing to 30 seconds for better reliability
    application = (
        Application.builder()
        .token(config.telegram_token)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .connect_timeout(30.0)
        .read_timeout(30.0)
        .write_timeout(30.0)
        .build()
    )

    # --- Error Handling ---
    application.add_error_handler(error_handler)

    # --- Conversation Handlers ---
    add_server_handler = ConversationHandler(
        entry_points=[
            CommandHandler('add_server', add_server_start),
            CallbackQueryHandler(add_server_start, pattern='^add_server_start$')
        ],
        states={
            ALIAS: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_alias)],
            HOSTNAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_hostname)],
            USER: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_user)],
            AUTH_METHOD: [
                CallbackQueryHandler(get_auth_method),
                MessageHandler(filters.TEXT & ~filters.COMMAND, auth_method_invalid_input)
            ],
            PASSWORD: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_password)],
            KEY_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, get_key_path)],
        },
        fallbacks=[CommandHandler('cancel', cancel_add_server)],
    )

    run_command_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(run_command_start, pattern='^run_command_')],
        states={
            AWAIT_COMMAND: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_command)],
        },
        fallbacks=[],
    )
    application.add_handler(add_server_handler)
    application.add_handler(run_command_handler)

    restore_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(restore_start, pattern='^restore$')],
        states={
            AWAIT_RESTORE_CONFIRMATION: [CallbackQueryHandler(restore_confirmation)],
            AWAIT_RESTORE_FILE: [MessageHandler(filters.ATTACHMENT, restore_file)],
        },
        fallbacks=[CommandHandler('cancel', cancel_restore)],
    )
    application.add_handler(restore_handler)

    service_management_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(service_action_start, pattern='^check_service_'),
            CallbackQueryHandler(service_action_start, pattern='^start_service_'),
            CallbackQueryHandler(service_action_start, pattern='^stop_service_'),
            CallbackQueryHandler(service_action_start, pattern='^restart_service_'),
        ],
        states={
            AWAIT_SERVICE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_service_action)],
        },
        fallbacks=[CommandHandler('cancel', cancel_service_action)],
    )
    application.add_handler(service_management_handler)

    install_package_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(install_package_start, pattern='^pkg_install_')],
        states={
            AWAIT_PACKAGE_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_install_package)],
        },
        fallbacks=[CommandHandler('cancel', cancel_install_package)],
    )
    application.add_handler(install_package_handler)

    docker_action_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(docker_action_start, pattern='^docker_logs_'),
            CallbackQueryHandler(docker_action_start, pattern='^docker_start_'),
            CallbackQueryHandler(docker_action_start, pattern='^docker_stop_'),
        ],
        states={
            AWAIT_CONTAINER_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_docker_action)],
        },
        fallbacks=[CommandHandler('cancel', cancel_docker_action)],
    )
    application.add_handler(docker_action_handler)

    file_manager_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(file_manager_action_start, pattern='^fm_ls_'),
            CallbackQueryHandler(file_manager_action_start, pattern='^fm_download_'),
            CallbackQueryHandler(file_manager_action_start, pattern='^fm_upload_'),
        ],
        states={
            AWAIT_FILE_PATH: [MessageHandler(filters.TEXT & ~filters.COMMAND, file_manager_dispatch)],
            AWAIT_UPLOAD_FILE: [MessageHandler(filters.ATTACHMENT, upload_file)],
        },
        fallbacks=[CommandHandler('cancel', cancel_file_manager_action)],
    )
    application.add_handler(file_manager_handler)

    kill_process_handler = ConversationHandler(
        entry_points=[CallbackQueryHandler(kill_process_start, pattern='^kill_process_')],
        states={
            AWAIT_PID: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_kill_process)],
        },
        fallbacks=[CommandHandler('cancel', cancel_kill_process)],
    )
    application.add_handler(kill_process_handler)

    firewall_management_handler = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(firewall_action_start, pattern='^fw_allow_'),
            CallbackQueryHandler(firewall_action_start, pattern='^fw_deny_'),
            CallbackQueryHandler(firewall_action_start, pattern='^fw_delete_'),
        ],
        states={
            AWAIT_FIREWALL_RULE: [MessageHandler(filters.TEXT & ~filters.COMMAND, execute_firewall_action)],
        },
        fallbacks=[CommandHandler('cancel', cancel_firewall_action)],
    )
    application.add_handler(firewall_management_handler)

    # --- UI & Menu Handlers ---
    application.add_handler(CallbackQueryHandler(main_menu, pattern='^main_menu$'))
    application.add_handler(CallbackQueryHandler(language_menu, pattern='^language_menu$'))
    application.add_handler(CallbackQueryHandler(set_language, pattern='^set_language_'))
    application.add_handler(CallbackQueryHandler(connect_server_menu, pattern='^connect_server_menu$'))
    application.add_handler(CallbackQueryHandler(remove_server_menu, pattern='^remove_server_menu$'))
    application.add_handler(CallbackQueryHandler(update_bot_command, pattern='^update_bot$'))
    application.add_handler(CallbackQueryHandler(handle_server_connection, pattern='^connect_'))
    application.add_handler(CallbackQueryHandler(start_shell_session, pattern='^start_shell_'))
    application.add_handler(CallbackQueryHandler(server_status_menu, pattern='^server_status_menu_'))
    application.add_handler(CallbackQueryHandler(get_static_info, pattern='^static_info_'))
    application.add_handler(CallbackQueryHandler(get_resource_usage, pattern='^resource_usage_'))
    application.add_handler(CallbackQueryHandler(live_monitoring, pattern='^live_monitoring_'))
    application.add_handler(CallbackQueryHandler(stop_live_monitoring, pattern='^stop_live_monitoring_'))
    application.add_handler(CallbackQueryHandler(backup, pattern='^backup$'))
    application.add_handler(CallbackQueryHandler(service_management_menu, pattern='^service_management_menu_'))
    application.add_handler(CallbackQueryHandler(package_management_menu, pattern='^package_management_menu_'))
    application.add_handler(CallbackQueryHandler(package_manager_action, pattern='^pkg_update_'))
    application.add_handler(CallbackQueryHandler(package_manager_action, pattern='^pkg_upgrade_'))
    application.add_handler(CallbackQueryHandler(docker_management_menu, pattern='^docker_management_menu_'))
    application.add_handler(CallbackQueryHandler(docker_ps, pattern='^docker_ps_'))
    application.add_handler(CallbackQueryHandler(file_manager_menu, pattern='^file_manager_menu_'))
    application.add_handler(CallbackQueryHandler(process_management_menu, pattern='^process_management_menu_'))
    application.add_handler(CallbackQueryHandler(list_processes, pattern='^ps_aux_'))
    application.add_handler(CallbackQueryHandler(firewall_management_menu, pattern='^firewall_management_menu_'))
    application.add_handler(CallbackQueryHandler(firewall_status, pattern='^fw_status_'))
    application.add_handler(CallbackQueryHandler(cancel_command_callback, pattern='^cancel_command_'))
    application.add_handler(CallbackQueryHandler(system_commands_menu, pattern='^system_commands_menu_'))
    application.add_handler(CallbackQueryHandler(confirm_system_command, pattern='^reboot_'))
    application.add_handler(CallbackQueryHandler(confirm_system_command, pattern='^shutdown_'))
    application.add_handler(CallbackQueryHandler(execute_system_command, pattern='^execute_'))
    application.add_handler(CallbackQueryHandler(get_disk_usage, pattern='^disk_usage_'))
    application.add_handler(CallbackQueryHandler(get_network_info, pattern='^network_info_'))
    application.add_handler(CallbackQueryHandler(get_open_ports, pattern='^open_ports_'))
    application.add_handler(CallbackQueryHandler(disconnect, pattern='^disconnect_'))
    application.add_handler(CallbackQueryHandler(remove_server_confirm, pattern='^remove_'))

    # --- Command Handlers ---
    application.add_handler(CommandHandler('start', start))
    application.add_handler(CommandHandler('language', language_menu))
    application.add_handler(CommandHandler('debug', toggle_debug_mode))
    application.add_handler(CommandHandler('exit_shell', exit_shell))
    application.add_handler(CommandHandler('update_bot', update_bot_command))
    application.add_handler(CommandHandler('dashboard', dashboard_command))

    return application
