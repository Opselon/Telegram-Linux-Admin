import os
import getpass
import sys
import subprocess
import shutil
from textwrap import fill

# Add the project root to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import initialize_database
from src.config import config
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


def install_systemd_service() -> bool:
    bot_command = os.path.abspath('venv/bin/tla-bot')
    service_content = f"""
[Unit]
Description=Telegram Linux Admin Bot
After=network.target

[Service]
User={getpass.getuser()}
Group={getpass.getuser()}
WorkingDirectory={os.getcwd()}
Environment="PYTHONPATH={os.getcwd()}"
ExecStart={bot_command}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
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

def main():
    """
    Main entry point for the setup script.

    This function implements the "smart" setup flow. It checks if a configuration
    file already exists.
    - If no config is found, it launches the `first_time_setup()` wizard to guide
      the user through the initial required configuration.
    - If a config already exists, it launches the `management_menu()` to allow
      the user to view and modify their existing settings.
    """
    print_banner()
    initialize_database()
    warn_if_config_invalid()
    # Check if this is a first-time setup by looking for the config file or if the token is empty.
    if not os.path.exists('config.json') or not config.telegram_token:
        first_time_setup()
    else:
        management_menu()

if __name__ == "__main__":
    # The script begins execution here.
    main()
