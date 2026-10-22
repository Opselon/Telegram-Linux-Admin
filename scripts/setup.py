import argparse
import json
import os
import getpass
import socket
import sys
import subprocess
import shutil
import time
import urllib.error
import urllib.request
from textwrap import fill

# Add the project root to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import initialize_database
from src.config import Config, config, validate_token
SERVICE_FILE = '/etc/systemd/system/telegram_bot.service'
CRON_FILE = '/etc/cron.d/telegram_bot_update'
UPDATE_LOG_FILE = '/var/log/telegram_bot_update.log'

# --- Color Definitions ---
C_BLUE = "\033[34m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_MAGENTA = "\033[35m"
C_CYAN = "\033[36m"
C_BOLD = "\033[1m"
C_DIM = "\033[2m"
C_RESET = "\033[0m"

THEME_WIDTH = 70

def print_banner():
    border = "═" * (THEME_WIDTH - 2)
    title = "TELEGRAM LINUX ADMIN SETUP"
    tagline = "Guided wizard for a secure, happy bot"
    print(f"\n{C_BOLD}{C_CYAN}╔{border}╗{C_RESET}")
    print(
        f"{C_BOLD}{C_CYAN}║{C_RESET} {C_MAGENTA}{title.center(THEME_WIDTH - 4)}{C_RESET} {C_BOLD}{C_CYAN}║{C_RESET}"
    )
    print(
        f"{C_BOLD}{C_CYAN}║{C_RESET} {C_DIM}{tagline.center(THEME_WIDTH - 4)}{C_RESET} {C_BOLD}{C_CYAN}║{C_RESET}"
    )
    print(f"{C_BOLD}{C_CYAN}╚{border}╝{C_RESET}\n")

def print_header(title, icon="✨"):
    border = "─" * (THEME_WIDTH - 2)
    line = f"{icon} {title}"
    print(f"\n{C_BOLD}{C_BLUE}┌{border}┐{C_RESET}")
    print(
        f"{C_BOLD}{C_BLUE}│{C_RESET} {C_BOLD}{line.ljust(THEME_WIDTH - 4)}{C_RESET} {C_BOLD}{C_BLUE}│{C_RESET}"
    )
    print(f"{C_BOLD}{C_BLUE}└{border}┘{C_RESET}")

def print_menu(options):
    for key, value in options.items():
        print(f"  {C_CYAN}[{key}]{C_RESET} {value}")
    print(f"  {C_DIM}{'─' * (THEME_WIDTH - 6)}{C_RESET}")

def print_success(message):
    print(f"  {C_GREEN}✔ {message}{C_RESET}")

def print_warning(message):
    print(f"  {C_YELLOW}! {message}{C_RESET}")

def print_error(message):
    print(f"  {C_RED}✖ {message}{C_RESET}")

def print_info(message):
    wrapped = fill(message, width=THEME_WIDTH - 6)
    for line in wrapped.splitlines():
        print(f"  {C_DIM}{line}{C_RESET}")

def get_input(prompt):
    return input(f"  {C_MAGENTA}? {prompt}:{C_RESET} ")

def pause(message="Press Enter to continue..."):
    input(f"\n  {C_DIM}{message}{C_RESET}")


def confirm(prompt: str, default: bool | None = None) -> bool:
    """Prompts for a yes/no confirmation with optional defaults."""
    if default is True:
        suffix = " [Y/n]"
    elif default is False:
        suffix = " [y/N]"
    else:
        suffix = " [y/n]"

    while True:
        choice = get_input(f"{prompt}{suffix}").strip().lower()
        if not choice and default is not None:
            return default
        if choice in {"y", "yes"}:
            return True
        if choice in {"n", "no"}:
            return False
        print_error("Please respond with 'y' or 'n'.")
def run_as_root(command, allow_failure=False):
    """Executes a command with elevated privileges when required."""
    sudo_path = shutil.which("sudo")
    run_command = list(command)

    try:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            result = subprocess.run(run_command, check=not allow_failure)
            return result.returncode == 0

        if sudo_path is None:
            print_error("`sudo` is not available. Run this script as root or install sudo.")
            return False

        subprocess.run([sudo_path, "-n", "true"], check=False, capture_output=True)
        result = subprocess.run([sudo_path] + run_command, check=not allow_failure)
        return result.returncode == 0
    except subprocess.CalledProcessError as exc:
        if not allow_failure:
            print_error(
                f"Command '{' '.join(run_command)}' failed with exit code {exc.returncode}."
            )
        return False
    except FileNotFoundError:
        print_error("Required command is missing on this system.")
        return False


def mask_token(token: str) -> str:
    if not token:
        return "Not set"
    if len(token) <= 8:
        return "Configured"
    return f"{token[:4]}…{token[-4:]}"


def warn_if_config_invalid():
    if getattr(config, "last_error", None):
        print_warning("Existing configuration could not be fully loaded. Defaults were used instead.")
        print_info(str(config.last_error))


def is_service_installed() -> bool:
    return os.path.exists(SERVICE_FILE)


def get_service_status() -> dict:
    status = {"installed": is_service_installed(), "active": False, "enabled": False}
    if not status["installed"]:
        return status

    try:
        active_result = subprocess.run(
            ["systemctl", "is-active", "--quiet", "telegram_bot.service"],
            check=False
        )
        enabled_result = subprocess.run(
            ["systemctl", "is-enabled", "--quiet", "telegram_bot.service"],
            check=False
        )
        status["active"] = active_result.returncode == 0
        status["enabled"] = enabled_result.returncode == 0
    except FileNotFoundError:
        status["active"] = False
        status["enabled"] = False

    return status


def is_cron_installed() -> bool:
    return os.path.exists(CRON_FILE)


def _systemd_quote(value: str) -> str:
    """Escapes a value for ``Environment=`` / ``ExecStart=`` in a unit file."""
    return str(value).replace("%", "%%").replace('"', '\\"')


def service_account() -> str:
    """Account the bot runs as.

    ``sudo ./install.sh`` must not leave the unit running as root while the
    checkout is owned by the invoking user: the real person is ``SUDO_USER``.
    """
    return os.environ.get("SUDO_USER") or getpass.getuser()


def build_service_unit(health_port: int | None = None) -> str:
    """Renders the systemd unit (pure, so it is testable without root)."""
    bot_command = os.path.abspath('venv/bin/tla-bot')
    config_path = os.path.abspath(config.path)
    db_path = os.path.abspath(os.environ.get("TLA_DB_FILE", "database.db"))
    audit_dir = os.path.abspath(os.environ.get("TLA_AUDIT_DIR", "var/log"))
    account = service_account()
    environment_lines = [
        f'Environment="TLA_CONFIG_FILE={_systemd_quote(config_path)}"',
        f'Environment="TLA_DB_FILE={_systemd_quote(db_path)}"',
        f'Environment="TLA_AUDIT_DIR={_systemd_quote(audit_dir)}"',
    ]
    if health_port:
        environment_lines += [
            f'Environment="TLA_HEALTH_PORT={int(health_port)}"',
            'Environment="TLA_HEALTH_HOST=127.0.0.1"',
        ]
    environment_block = "\n".join(environment_lines)
    working_directory = _systemd_quote(os.getcwd())
    return f"""
[Unit]
Description=Telegram Linux Admin Bot
After=network.target
Wants=network-online.target

[Service]
User={account}
Group={account}
WorkingDirectory={working_directory}
Environment="PYTHONPATH={working_directory}"
{environment_block}
ExecStart={bot_command}
Restart=always
RestartSec=10
TimeoutStopSec=5

[Install]
WantedBy=multi-user.target
"""


def install_systemd_service(health_port: int | None = None) -> bool:
    service_content = build_service_unit(health_port=health_port)
    temp_service_file = "telegram_bot.service.tmp"
    with open(temp_service_file, 'w') as f:
        f.write(service_content)

    try:
        if run_as_root(["mv", temp_service_file, SERVICE_FILE]):
            run_as_root(["systemctl", "daemon-reload"], allow_failure=True)
            run_as_root(["systemctl", "enable", "telegram_bot.service"], allow_failure=True)
            print_success("Service installed/updated and enabled successfully.")
            print_info("To start it now run: sudo systemctl start telegram_bot.service")
            return True
        print_error("Failed to install the systemd service. See the messages above for details.")
        return False
    finally:
        if os.path.exists(temp_service_file):
            os.remove(temp_service_file)


def uninstall_systemd_service() -> bool:
    if not is_service_installed():
        print_warning("The systemd service is not currently installed.")
        return False

    run_as_root(["systemctl", "stop", "telegram_bot.service"], allow_failure=True)
    run_as_root(["systemctl", "disable", "telegram_bot.service"], allow_failure=True)
    removed = run_as_root(["rm", SERVICE_FILE])
    if removed:
        run_as_root(["systemctl", "daemon-reload"], allow_failure=True)
        print_success("Systemd service removed.")
        return True

    print_error("Failed to remove the systemd service file.")
    return False


def install_cron_job() -> bool:
    update_command = os.path.abspath('venv/bin/tla-bot-update')
    user = getpass.getuser()

    if not os.path.exists(UPDATE_LOG_FILE):
        run_as_root(["touch", UPDATE_LOG_FILE], allow_failure=True)
        run_as_root(["chown", f"{user}:{user}", UPDATE_LOG_FILE], allow_failure=True)

    cron_content = f"0 3 * * * {user} {update_command} >> {UPDATE_LOG_FILE} 2>&1\n"
    temp_cron_file = "telegram_bot_update.tmp"
    with open(temp_cron_file, 'w') as f:
        f.write(cron_content)

    try:
        if run_as_root(["mv", temp_cron_file, CRON_FILE]):
            run_as_root(["chmod", "644", CRON_FILE], allow_failure=True)
            print_success("Cron job for daily updates installed successfully.")
            print_info("Updates will be checked every day at 03:00.")
            return True
        print_error("Failed to install the cron job. See the messages above for details.")
        return False
    finally:
        if os.path.exists(temp_cron_file):
            os.remove(temp_cron_file)


def uninstall_cron_job() -> bool:
    if not is_cron_installed():
        print_warning("The cron job is not currently installed.")
        return False

    if run_as_root(["rm", CRON_FILE]):
        print_success("Cron job removed.")
        return True

    print_error("Failed to remove the cron job.")
    return False


def show_setup_summary():
    print_header("Setup Summary", icon="📋")
    print_info(f"Telegram bot token: {'Configured' if config.telegram_token else 'Missing'}")

    if config.whitelisted_users:
        users = ', '.join(str(user) for user in config.whitelisted_users)
        print_info(f"Whitelisted Telegram IDs: {users}")
    else:
        print_warning("No whitelisted users configured yet.")

    service_status = get_service_status()
    if service_status["installed"]:
        print_info(
            f"Systemd service: Installed (Enabled: {'Yes' if service_status['enabled'] else 'No'}, "
            f"Active: {'Running' if service_status['active'] else 'Stopped'})"
        )
    else:
        print_warning("Systemd service: Not installed")

    if is_cron_installed():
        print_info("Auto-update cron job: Installed")
    else:
        print_warning("Auto-update cron job: Not installed")

def manage_telegram_bot():
    print_header("Telegram Bot Configuration", icon="🤖")
    if config.telegram_token:
        print_info(f"Current token: {mask_token(config.telegram_token)}")
    else:
        print_warning("No Telegram bot token has been configured yet.")

    print_info("Provide a new token below or press Enter to keep the existing value.")
    new_token = get_input("Enter new Telegram Bot Token (leave blank to keep current)").strip()
    if new_token:
        try:
            config.set_token(new_token)
        except ValueError as exc:
            print_error(str(exc))
        else:
            print_success("Token updated.")
    else:
        print_info("Keeping existing token value.")

    pause("Press Enter to return to the main menu...")

def manage_whitelist():
    while True:
        print_header("Whitelist Management", icon="🛡️")
        users = config.whitelisted_users
        if not users:
            print_warning("No whitelisted users have been added yet.")
        else:
            print_info("Only the listed Telegram user IDs will be able to control the bot.")
            for i, user_id in enumerate(users):
                print(f"  {C_GREEN}{i+1}. {user_id}{C_RESET}")

        print_menu({
            'a': "Add a Telegram user ID",
            'r': "Remove an existing ID",
            'm': "Return to the main menu"
        })
        choice = get_input("Choose an action").lower().strip()

        if choice == 'a':
            user_id_str = get_input("Enter Telegram User ID to add").strip()
            try:
                config.add_whitelisted_user(user_id_str)
                print_success("User added to whitelist.")
            except ValueError as exc:
                print_error(str(exc))
        elif choice == 'r':
            user_id_str = get_input("Enter User ID to remove").strip()
            try:
                removed = config.remove_whitelisted_user(user_id_str)
            except ValueError as exc:
                print_error(str(exc))
            else:
                if removed:
                    print_success("User removed from whitelist.")
                else:
                    print_warning("That user ID is not in the whitelist.")
        elif choice == 'm':
            break
        else:
            print_error("Invalid choice. Please pick one of the listed options.")

        if choice != 'm':
            pause()

def manage_systemd_service(install=False):
    if install:
        install_systemd_service()
        return

    while True:
        status = get_service_status()
        print_header("Systemd Service Manager", icon="🛠️")
        if status["installed"]:
            print_success("Service file detected.")
            print_info(f"Location: {SERVICE_FILE}")
            print_info(f"Enabled on boot: {'Yes' if status['enabled'] else 'No'}")
            print_info(f"Active right now: {'Running' if status['active'] else 'Stopped'}")
        else:
            print_warning("Service is not installed yet.")
            print_info("Install it to keep the bot running automatically on boot.")

        print_menu({
            '1': "Install or update systemd service",
            '2': "Uninstall service",
            '3': "Start or restart the service",
            '4': "Stop the service",
            'b': "Back to the main menu"
        })
        choice = get_input("Select an option").lower().strip()

        if choice == '1':
            install_systemd_service()
        elif choice == '2':
            uninstall_systemd_service()
        elif choice == '3':
            if status["installed"]:
                if run_as_root(["systemctl", "restart", "telegram_bot.service"], allow_failure=True):
                    print_success("Service started (or restarted) successfully.")
                else:
                    print_warning(
                        "Could not start the service. Check logs with 'sudo journalctl -u telegram_bot.service'."
                    )
            else:
                print_warning("Install the service before attempting to start it.")
        elif choice == '4':
            if status["installed"]:
                if run_as_root(["systemctl", "stop", "telegram_bot.service"], allow_failure=True):
                    print_success("Service stopped.")
                else:
                    print_warning("Failed to stop the service. It might not be running.")
            else:
                print_warning("Service is not installed.")
        elif choice == 'b':
            break
        else:
            print_error("Invalid option. Please try again.")

        if choice != 'b':
            pause()


def manage_cron_job(install=False):
    if install:
        install_cron_job()
        return

    while True:
        installed = is_cron_installed()
        print_header("Automatic Update Scheduler", icon="🕒")
        if installed:
            print_success("Daily update cron job is configured.")
            print_info(f"Cron file: {CRON_FILE}")
            print_info(f"It runs the updater every day at 03:00 and logs to {UPDATE_LOG_FILE}.")
        else:
            print_warning("Cron job is not installed.")
            print_info("Install it to automatically fetch the latest bot updates each night.")

        print_menu({
            '1': "Install or update cron job",
            '2': "Remove cron job",
            'b': "Back to the main menu"
        })
        choice = get_input("Select an option").lower().strip()

        if choice == '1':
            install_cron_job()
        elif choice == '2':
            uninstall_cron_job()
        elif choice == 'b':
            break
        else:
            print_error("Invalid option. Please try again.")

        if choice != 'b':
            pause()


def first_time_setup():
    """Guides the user through the initial essential setup."""
    print_header("Welcome to the Telegram Linux Admin Bot Setup Wizard!", icon="👋")
    print_info("This guided experience will help you configure the essentials in just a few minutes.")

    # Step 1: Configure Telegram Token
    print_header("Step 1 · Configure Telegram Bot", icon="1️⃣")
    while not config.telegram_token:
        new_token = get_input("Please enter your Telegram Bot Token").strip()
        try:
            config.set_token(new_token)
        except ValueError as exc:
            print_error(str(exc))
        else:
            print_success("Token saved.")

    # Step 2: Add initial whitelisted user
    print_header("Step 2 · Add Your Telegram User ID", icon="2️⃣")
    while not config.whitelisted_users:
        user_id_str = get_input("Please enter your Telegram User ID").strip()
        try:
            config.add_whitelisted_user(user_id_str)
            print_success("You have been added to the whitelist.")
        except ValueError as exc:
            print_error(str(exc))

    # Step 3: Ask to install Systemd Service
    print_header("Step 3 · Install as a Systemd Service (Recommended)", icon="3️⃣")
    if confirm("Install the bot as a systemd service?", default=True):
        manage_systemd_service(install=True)
    else:
        print_info("You can install the service later from the management menu.")

    # Step 4: Ask to install Cron Job
    print_header("Step 4 · Configure Automatic Updates (Recommended)", icon="4️⃣")
    if confirm("Set up a daily cron job for automatic updates?", default=True):
        manage_cron_job(install=True)
    else:
        print_info("You can enable automatic updates from the management menu at any time.")

    print_header("All set!", icon="🎉")
    print_info("Basic setup is complete. The bot is ready to run.")
    print_info("Re-run this script anytime to open the management panel and adjust settings.")
    show_setup_summary()
    pause("Press Enter to exit the wizard...")


def management_menu():
    """Displays the main management menu for existing installations."""
    while True:
        print_header("Telegram Linux Admin · Management Panel", icon="🧭")
        print_info("Select an option to view or update a part of your setup.")
        menu_options = {
            '1': "Configure Telegram Bot",
            '2': "Manage Whitelisted Users",
            '3': "Manage Systemd Service",
            '4': "Manage Cron Job for Auto-Updates",
            '5': "Exit"
        }
        print_menu(menu_options)
        choice = get_input("Select an option").strip()

        if choice == '1':
            manage_telegram_bot()
        elif choice == '2':
            manage_whitelist()
        elif choice == '3':
            manage_systemd_service()
        elif choice == '4':
            manage_cron_job()
        elif choice == '5':
            print_info("Exiting management panel. Goodbye!")
            break
        else:
            print_error("Invalid option. Please try again.")
            pause()

# ---------------------------------------------------------------------------
# One-command (easy) setup
# ---------------------------------------------------------------------------

EASY_ACCESS_MODE = "whitelist"
EASY_DESTINATION_POLICY = "deny_public"
EASY_HEALTH_PORT = 8081
EASY_PORT_ATTEMPTS = 20


def collect_admin_ids(raw_values) -> list[str]:
    """Flattens ``--admin-id`` values (repeatable or comma-separated)."""
    ids: list[str] = []
    for value in raw_values or []:
        if value is None:
            continue
        for part in str(value).replace(",", " ").split():
            if part:
                ids.append(part)
    return ids


def env_admin_ids() -> list[str]:
    raw = os.environ.get("TLA_ADMIN_ID", "") or os.environ.get("TLA_ADMIN_IDS", "")
    return [part for part in raw.replace(",", " ").split() if part]


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def choose_health_port(preferred: int | None) -> int | None:
    """First free port from ``preferred`` upward; ``None`` when none is free."""
    base = int(preferred or EASY_HEALTH_PORT)
    for candidate in range(base, base + EASY_PORT_ATTEMPTS):
        if not port_in_use(candidate):
            return candidate
    return None


def _health_get(url: str, timeout: float = 3.0) -> tuple[int, str]:
    request = urllib.request.Request(url, headers={"User-Agent": "tla-setup"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def wait_for_readiness(port: int, timeout: float = 60.0) -> tuple[bool, str]:
    """Polls ``/readyz`` until the bot is ready or the deadline passes."""
    deadline = time.monotonic() + timeout
    last = ""
    while time.monotonic() < deadline:
        try:
            status, body = _health_get(f"http://127.0.0.1:{int(port)}/readyz")
            last = body.strip()
            if status == 200 and '"ready"' in last:
                return True, last
        except Exception as exc:  # connection refused until the bot listens
            last = str(exc)
        time.sleep(1.0)
    return False, last


def _journal_tail(lines: int = 15) -> str:
    """Best-effort tail of the unit journal (empty when unavailable)."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "telegram_bot.service", "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (result.stdout or result.stderr or "").strip()


def _easy_summary(token: str, admin_ids: list[str], health_port: int | None,
                  service_active: bool, ready: bool, detail: str,
                  ok: bool = True) -> dict:
    return {
        "ok": bool(ok),
        "token": mask_token(token),
        "admin_ids": admin_ids,
        "access_mode": config.access_mode,
        "destination_policy": config.destination_policy,
        "config_path": os.path.abspath(config.path),
        "database_path": os.path.abspath(os.environ.get("TLA_DB_FILE", "database.db")),
        "audit_dir": os.path.abspath(os.environ.get("TLA_AUDIT_DIR", "var/log")),
        "health_port": health_port,
        "service_active": service_active,
        "service_installed": is_service_installed(),
        "cron_installed": is_cron_installed(),
        "readiness": ready,
        "readiness_detail": detail,
    }


def easy_setup(args) -> int:
    """Non-interactive, secure-by-default installation.

    Validates the credentials, writes ``config.json`` (whitelist + deny_public),
    prepares the database, installs the systemd unit and daily-updater cron
    job, starts the bot and verifies ``/readyz``. Every failure is fatal with a
    non-zero exit code so callers (``install.sh`` / curl) cannot treat a broken
    install as success.
    """
    print_header("One-Command Setup (easy)", icon="⚡")
    print_info(
        "Non-interactive mode: token + admin ID + secure defaults "
        "(whitelist access, deny_public destinations, health endpoint)."
    )

    # Re-read the file so hand-written keys survive this rewrite; the process
    # may have imported config.py before another step touched it.
    config.load_config()

    token = (args.token or os.environ.get("TLA_BOT_TOKEN") or config.telegram_token or "").strip()
    admin_ids = collect_admin_ids(args.admin_ids) or env_admin_ids()

    problems: list[str] = []
    computed_token = ""
    try:
        computed_token = validate_token(token)
    except ValueError as exc:
        problems.append(f"Invalid bot token: {exc}")

    if not admin_ids:
        if config.whitelisted_users:
            admin_ids = [str(user) for user in config.whitelisted_users]
            print_info("Keeping the existing whitelist entries.")
        else:
            problems.append(
                "No admin Telegram user ID. Pass --admin-id or set TLA_ADMIN_ID."
            )
    for raw_id in admin_ids:
        try:
            Config._validate_user_id(raw_id)
        except ValueError as exc:
            problems.append(f"Admin ID {raw_id!r}: {exc}")

    if problems:
        for problem in problems:
            print_error(problem)
        print_info(
            "Example: ./install.sh --token 123456:ABC --admin-id 42424242"
        )
        return 2

    token = computed_token

    stored_port = config.extra.get("health_port") if isinstance(config.extra, dict) else None
    preferred_port = args.health_port or (int(stored_port) if isinstance(stored_port, int) else None)
    health_port = choose_health_port(preferred_port)
    if health_port is None:
        print_error("No free port for the health endpoint in the requested range.")
        return 1

    access_mode = (args.access_mode or EASY_ACCESS_MODE).strip().lower()
    destination_policy = (args.destination_policy or EASY_DESTINATION_POLICY).strip().lower()

    try:
        config.set_token(token)
        config.set_access_mode(access_mode)
        config.destination_policy = destination_policy
        for admin_id in admin_ids:
            config.add_whitelisted_user(admin_id)
        config.extra["health_port"] = int(health_port)
        config.save_config()
    except ValueError as exc:
        print_error(f"Failed to write configuration: {exc}")
        return 2

    print_success("Configuration written securely (mode 0600).")
    db_file = os.environ.get("TLA_DB_FILE", "database.db")
    if db_file == "database.db":
        # Keep the database next to the config, wherever it lives.
        os.environ["TLA_DB_FILE"] = os.path.join(
            os.path.dirname(os.path.abspath(config.path)), "database.db"
        )
        db_file = os.environ["TLA_DB_FILE"]
    initialize_database()
    print_success(f"Database ready: {db_file}")

    service_installed = False
    if args.no_service:
        print_warning("Skipping systemd service installation (--no-service).")
    else:
        print_header("Systemd service", icon="🛠️")
        service_installed = install_systemd_service(health_port=health_port)

    cron_installed = False
    if not args.no_cron:
        cron_installed = install_cron_job()

    ready = False
    detail = ""
    if service_installed and args.no_start:
        print_warning("Service installed but not started (--no-start).")
    elif service_installed:
        started = run_as_root(
            ["systemctl", "restart", "telegram_bot.service"], allow_failure=True
        )
        if not started:
            print_error(
                "systemctl restart failed. Inspect with: "
                "sudo journalctl -u telegram_bot.service -n 30"
            )
            if args.json:
                summary = _easy_summary(
                    token, admin_ids, health_port, False, False, "", ok=False
                )
                print(json.dumps(summary, sort_keys=True))
            return 1
        ready, detail = wait_for_readiness(health_port, args.verify_timeout)
        if ready:
            print_success("Health check passed (GET /readyz → 200 ready).")
            print_info(f"healthz: http://127.0.0.1:{health_port}/healthz")
            print_info(f"readyz:  http://127.0.0.1:{health_port}/readyz")
        else:
            print_error(f"Readiness probe did not pass. Last response: {detail}")
            print_info("Last journal lines for telegram_bot.service:")
            journal = _journal_tail()
            for line in journal.splitlines()[:15]:
                print(f"    {line}")
            if args.json:
                summary = _easy_summary(
                    token, admin_ids, health_port, True, ready, detail, ok=False
                )
                print(json.dumps(summary, sort_keys=True))
            return 1
    else:
        print_info(
            "Start the bot manually with:\n"
            f"  TLA_CONFIG_FILE={os.path.abspath(config.path)} "
            f"TLA_DB_FILE={os.path.abspath(db_file)} "
            f"TLA_HEALTH_PORT={health_port} "
            f"TLA_AUDIT_DIR={os.path.abspath(os.environ.get('TLA_AUDIT_DIR', 'var/log'))} "
            "venv/bin/tla-bot"
        )

    print_header("All set!", icon="🎉")
    show_setup_summary()

    if args.json:
        ok = True if (args.no_service or args.no_start or not service_installed) else ready
        summary = _easy_summary(
            token, admin_ids, health_port,
            is_service_installed() and not args.no_start, ready, detail, ok=ok,
        )
        summary["access_mode"] = config.access_mode
        summary["destination_policy"] = config.destination_policy
        print(json.dumps(summary, sort_keys=True))
    return 0


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description=(
            "Telegram Linux Admin setup: interactive wizard (default) or a "
            "fully non-interactive one-command install with --token/--admin-id."
        ),
    )
    parser.add_argument(
        "--easy",
        action="store_true",
        help="Non-interactive install with secure defaults (whitelist + deny_public).",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("TLA_BOT_TOKEN"),
        help="Telegram bot token (or TLA_BOT_TOKEN). Never printed in clear text.",
    )
    parser.add_argument(
        "--admin-id",
        "--user-id",
        "--telegram-id",
        dest="admin_ids",
        action="append",
        default=[],
        help="Telegram user ID allowed to administer the bot. Repeatable, "
             "comma-separated values allowed (or TLA_ADMIN_ID).",
    )
    parser.add_argument("--config", default=None, help="Path to config.json.")
    parser.add_argument(
        "--access-mode",
        choices=("", "open", "whitelist", "admin_only"),
        default=EASY_ACCESS_MODE,
        help="Access mode written in easy mode. Default: whitelist.",
    )
    parser.add_argument(
        "--destination-policy",
        choices=("", "allow_all", "deny_public", "allowlist"),
        default=EASY_DESTINATION_POLICY,
        help="Destination policy written in easy mode. Default: deny_public.",
    )
    parser.add_argument(
        "--health-port",
        type=int,
        default=None,
        help="Loopback port for /healthz, /readyz, /metrics. Default: first free of 8081+.",
    )
    parser.add_argument(
        "--verify-timeout",
        type=int,
        default=60,
        help="Seconds to wait for /readyz after service start. Default: 60.",
    )
    parser.add_argument("--no-service", action="store_true", help="Skip systemd unit installation.")
    parser.add_argument("--no-cron", action="store_true", help="Skip the daily auto-update cron job.")
    parser.add_argument("--no-start", action="store_true", help="Do not restart/verify the service.")
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print a single JSON summary line (for automation).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Wizard by default; fully non-interactive when credentials are supplied."""
    args = build_arg_parser().parse_args(argv)
    if args.config:
        global config
        config = Config(args.config)
        os.environ.setdefault("TLA_CONFIG_FILE", os.path.abspath(args.config))
    print_banner()

    non_interactive = (
        args.easy
        or bool(args.token)
        or bool(collect_admin_ids(args.admin_ids))
        or bool(env_admin_ids())
    )
    if non_interactive:
        try:
            return easy_setup(args)
        except KeyboardInterrupt:
            print_error("Setup interrupted.")
            return 130

    initialize_database()
    warn_if_config_invalid()
    if not os.path.exists(config.path) or not config.telegram_token:
        first_time_setup()
    else:
        management_menu()
    return 0


if __name__ == "__main__":
    sys.exit(main())
