import json
import os
import sys
import getpass

# Add the project root to the Python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

from src.database import initialize_database, add_user, add_server, get_all_servers, remove_server

CONFIG_FILE = 'config.json'
SERVICE_FILE = '/etc/systemd/system/telegram_bot.service'
VENV_PYTHON = 'venv/bin/python'
CRON_FILE = '/etc/cron.d/telegram_bot_update'


def print_header(title):
    print("\n" + "="*40)
    print(f" {title}")
    print("="*40)

def get_input(prompt, default=None):
    """Gets user input with an optional default value."""
    if default:
        response = input(f"{prompt} (default: {default}): ")
        return response or default
    else:
        return input(f"{prompt}: ")

def setup_telegram_token():
    """Sets up the config.json file with the Telegram token."""
    print_header("Telegram Bot Token Setup")
    token = get_input("Enter your Telegram Bot Token")
    with open('config.json', 'w') as f:
        json.dump({"telegram_token": token}, f, indent=2)
    print("Telegram token saved to config.json")

def setup_database():
    """Initializes the database and guides the user through initial setup."""
    initialize_database()

    print_header("Initial User & Server Setup")
    print("Enter your Telegram user ID to grant yourself access to the bot.")
    print("You can get your user ID by messaging @userinfobot on Telegram.")
    user_id = get_input("Your Telegram User ID")
    if user_id.isdigit():
        add_user(int(user_id))
        print("User added successfully.")
    else:
        print("Invalid user ID. Must be a number.")

    while True:
        print("\n--- Server Configuration ---")
        servers = get_all_servers()
        if not servers:
            print("No servers configured yet.")
        else:
            for i, server in enumerate(servers):
                print(f"  {i+1}. {server['alias']} ({server['user']}@{server['hostname']})")

        choice = get_input("\nChoose an action: [a]dd server, [r]emove server, [d]one", "d")
        if choice.lower() == 'a':
            alias = get_input("  Enter a short alias for the server (e.g., 'webserver')")
            hostname = get_input("  Enter the server's hostname or IP address")
            user = get_input("  Enter the SSH username")
            key_path = get_input("  Enter the path to your SSH private key", f"/home/{getpass.getuser()}/.ssh/id_rsa")
            try:
                add_server(alias, hostname, user, key_path)
                print("Server added successfully.")
            except ValueError as e:
                print(f"Error: {e}")
        elif choice.lower() == 'r':
            try:
                index = int(get_input("  Enter the number of the server to remove")) - 1
                if 0 <= index < len(servers):
                    remove_server(servers[index]['alias'])
                    print("Server removed.")
                else:
                    print("Invalid server number.")
            except ValueError:
                print("Invalid input.")
        elif choice.lower() == 'd':
            break

def setup_systemd():
    """Generates and installs a systemd service file."""
    print_header("Systemd Service Setup")
    if os.geteuid() != 0:
        print("This script is not running as root. Cannot install the systemd service.")
        print(f"To install the service, please run 'sudo python3 {os.path.abspath(__file__)} --install-service'")
        return

    python_path = os.path.abspath(VENV_PYTHON)

    if not os.path.exists(python_path):
        print(f"Error: Python executable not found at {python_path}.")
        print("Please ensure you have created a virtual environment by running install.sh.")
        return

    service_content = f"""
[Unit]
Description=Telegram Multi-Server Bot
After=network.target

[Service]
User={getpass.getuser()}
Group={getpass.getuser()}
WorkingDirectory={os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))}
ExecStart={python_path} -m src.main
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(SERVICE_FILE, 'w') as f:
            f.write(service_content)

        os.system('systemctl daemon-reload')
        os.system('systemctl enable telegram_bot.service')
        print(f"Systemd service created at {SERVICE_FILE}")
        print("\nTo start the service now, run: sudo systemctl start telegram_bot.service")
        print("To check the service status, run: sudo systemctl status telegram_bot.service")
        print("To view live logs, run: sudo journalctl -u telegram_bot -f")
    except Exception as e:
        print(f"\nAn error occurred while creating the systemd service: {e}")

def setup_cron():
    """Generates and installs a cron job for daily updates."""
    print_header("Automatic Updates Setup")
    if os.geteuid() != 0:
        print("This script is not running as root. Cannot install the cron job.")
        print(f"To install the cron job, please run 'sudo python3 {os.path.abspath(__file__)} --install-cron'")
        return

    updater_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'updater.py'))
    python_path = os.path.abspath(VENV_PYTHON)

    if not os.path.exists(python_path):
        print(f"Error: Python executable not found at {python_path}.")
        return

    cron_content = f"0 3 * * * {getpass.getuser()} cd {os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))} && {python_path} {updater_path} --auto\n"

    try:
        with open(CRON_FILE, 'w') as f:
            f.write(cron_content)
        print(f"Cron job for daily updates created at {CRON_FILE}")
    except Exception as e:
        print(f"\nAn error occurred while creating the cron job: {e}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Setup wizard for the Telegram Multi-Server Bot.")
    parser.add_argument('--install-service', action='store_true', help='Install the systemd service (requires root).')
    parser.add_argument('--install-cron', action='store_true', help='Install the cron job for daily updates (requires root).')
    args = parser.parse_args()

    if args.install_service:
        setup_systemd()
    elif args.install_cron:
        setup_cron()
    else:
        print_header("Telegram Bot Setup Wizard")
        print("This wizard will guide you through configuring the bot.")

        if not os.path.exists('config.json'):
            setup_telegram_token()

        setup_database()

        auto_update = get_input("\nEnable daily automatic updates? (y/n)", "y")
        if auto_update.lower() == 'y':
            setup_cron()

        install_service = get_input("\nInstall the systemd service to run the bot automatically? (y/n)", "y")
        if install_service.lower() == 'y':
            setup_systemd()

        print("\n--- Setup Complete ---")
        print("To run the bot manually, activate the virtual environment and run:")
        print("source venv/bin/activate")
        print("python3 -m src.main")
