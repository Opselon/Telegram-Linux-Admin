import json
import os
import getpass
import sys
import subprocess

# Add the project root to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import initialize_database
from src.config import config
SERVICE_FILE = '/etc/systemd/system/telegram_bot.service'
VENV_PYTHON = 'venv/bin/python'
CRON_FILE = '/etc/cron.d/telegram_bot_update'

# --- Color Definitions ---
C_BLUE = "\033[34m"
C_GREEN = "\033[32m"
C_YELLOW = "\033[33m"
C_RED = "\033[31m"
C_BOLD = "\033[1m"
C_RESET = "\033[0m"

def print_header(title):
    print(f"\n{C_BOLD}{'='*50}{C_RESET}")
    print(f"  {C_BOLD}{C_BLUE}{title}{C_RESET}")
    print(f"{C_BOLD}{'='*50}{C_RESET}")

def print_menu(options):
    for key, value in options.items():
        print(f"  [{key}] {value}")
    print("-" * 50)

def get_input(prompt):
    return input(f"  {C_YELLOW}> {prompt}:{C_RESET} ")

def run_as_root(command):
    """Executes a command with sudo, asking for password if necessary."""
    try:
        subprocess.run(["sudo", "-v"], check=True, capture_output=True) # Check if sudo is active
        subprocess.run(["sudo"] + command, check=True)
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("\nError: Could not execute command as root. Please run the setup with 'sudo'.")
        return False

def manage_telegram_bot():
    print_header("Telegram Bot Configuration")
    print(f"  Current Token: {config.telegram_token if config.telegram_token else 'Not set'}")
    new_token = get_input("Enter new Telegram Bot Token (or press Enter to keep current)")
    if new_token:
        config.telegram_token = new_token
        config.save_config()
        print("  ✅ Token updated.")

    input("\n  Press Enter to return to the main menu...")

def manage_whitelist():
    while True:
        print_header("Whitelist Management")
        users = config.whitelisted_users
        if not users:
            print("  No whitelisted users.")
        else:
            for i, user_id in enumerate(users):
                print(f"  {i+1}. {user_id}")

        print("\n  [a] Add User | [r] Remove User | [m] Main Menu")
        choice = get_input("Choose an action").lower()

        if choice == 'a':
            user_id_str = get_input("Enter Telegram User ID to add")
            if user_id_str.isdigit():
                user_id = int(user_id_str)
                if user_id not in config.whitelisted_users:
                    config.whitelisted_users.append(user_id)
                    config.save_config()
                    print("  ✅ User added.")
                else:
                    print("  User already in whitelist.")
            else:
                print("  ❌ Invalid ID.")
        elif choice == 'r':
            user_id_str = get_input("Enter User ID to remove")
            if user_id_str.isdigit():
                user_id = int(user_id_str)
                if user_id in config.whitelisted_users:
                    config.whitelisted_users.remove(user_id)
                    config.save_config()
                    print("  ✅ User removed.")
                else:
                    print("  User not in whitelist.")
            else:
                print("  ❌ Invalid ID.")
        elif choice == 'm':
            break
        else:
            print("  ❌ Invalid choice.")
        input("\n  Press Enter to continue...")

def manage_systemd_service(install=False):
    if install:
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

        if run_as_root(["mv", temp_service_file, SERVICE_FILE]):
            run_as_root(["systemctl", "daemon-reload"])
            run_as_root(["systemctl", "enable", "telegram_bot.service"])
            print("  ✅ Service installed/updated and enabled successfully.")
            print("  To start it, run: sudo systemctl start telegram_bot.service")

    elif choice == 'u':
        if is_installed:
            run_as_root(["systemctl", "stop", "telegram_bot.service"])
            run_as_root(["systemctl", "disable", "telegram_bot.service"])
            run_as_root(["rm", SERVICE_FILE])
            run_as_root(["systemctl", "daemon-reload"])
            print("  ✅ Service uninstalled.")
        else:
            print("  Service is not installed.")

    input("\n  Press Enter to return to the main menu...")


def manage_cron_job(install=False):
    if install:
        update_command = os.path.abspath('venv/bin/tla-bot-update')
        user = getpass.getuser()
        log_file = '/var/log/telegram_bot_update.log'

        # Ensure log file exists and has correct permissions
        if not os.path.exists(log_file):
            run_as_root(["touch", log_file])
            run_as_root(["chown", f"{user}:{user}", log_file])

        cron_content = f"0 3 * * * {user} {update_command} >> {log_file} 2>&1\n"
        temp_cron_file = "telegram_bot_update.tmp"
        with open(temp_cron_file, 'w') as f:
            f.write(cron_content)

        if run_as_root(["mv", temp_cron_file, CRON_FILE]):
            run_as_root(["chmod", "644", CRON_FILE])
            print("  ✅ Cron job for daily updates installed successfully.")

    elif choice == 'u':
        if is_installed:
            run_as_root(["rm", CRON_FILE])
            print("  ✅ Cron job uninstalled.")
        else:
            print("  Cron job is not installed.")

    input("\n  Press Enter to return to the main menu...")


def first_time_setup():
    """Guides the user through the initial essential setup."""
    print_header("Welcome to the Telegram Linux Admin Bot Setup Wizard!")
    print("This wizard will guide you through the essential setup steps.")

    # Step 1: Configure Telegram Token
    print_header("Step 1: Configure Telegram Bot")
    while not config.telegram_token:
        new_token = get_input("Please enter your Telegram Bot Token")
        if new_token:
            config.telegram_token = new_token
            config.save_config()
            print("  ✅ Token saved.")
        else:
            print("  ❌ Token cannot be empty.")

    # Step 2: Add initial whitelisted user
    print_header("Step 2: Add Your Telegram User ID")
    while not config.whitelisted_users:
        user_id_str = get_input("Please enter your Telegram User ID")
        if user_id_str.isdigit():
            user_id = int(user_id_str)
            config.whitelisted_users.append(user_id)
            config.save_config()
            print("  ✅ You have been added to the whitelist.")
        else:
            print("  ❌ Invalid ID. Please enter a numeric user ID.")

    # Step 3: Ask to install Systemd Service
    print_header("Step 3: Install as a Systemd Service (Recommended)")
    choice = get_input("Do you want to install the bot as a systemd service? (y/n)").lower()
    if choice == 'y':
        manage_systemd_service(install=True)

    # Step 4: Ask to install Cron Job
    print_header("Step 4: Configure Automatic Updates (Recommended)")
    choice = get_input("Do you want to set up a daily cron job for automatic updates? (y/n)").lower()
    if choice == 'y':
        manage_cron_job(install=True)

    print("\n🎉 Basic setup is complete! You can now start the bot.")
    print("You can re-run this script later to access the management panel and modify your settings.")
    input("\nPress Enter to exit...")


def management_menu():
    """Displays the main management menu for existing installations."""
    while True:
        print_header("Telegram Linux Admin - Management Panel")
        menu_options = {
            '1': "Configure Telegram Bot",
            '2': "Manage Whitelisted Users",
            '3': "Manage Systemd Service",
            '4': "Manage Cron Job for Auto-Updates",
            '5': "Exit"
        }
        print_menu(menu_options)
        choice = get_input("Select an option")

        if choice == '1':
            manage_telegram_bot()
        elif choice == '2':
            manage_whitelist()
        elif choice == '3':
            manage_systemd_service()
        elif choice == '4':
            manage_cron_job()
        elif choice == '5':
            print("\nExiting management panel. Goodbye!\n")
            break
        else:
            print("\n  ❌ Invalid option. Please try again.")
            input("\n  Press Enter to continue...")

def main():
    """Main function to run the setup or management panel."""
    initialize_database()
    # Check if this is a first-time setup by looking for the config file
    if not os.path.exists('config.json') or not config.telegram_token:
        first_time_setup()
    else:
        management_menu()

if __name__ == "__main__":
    main()
