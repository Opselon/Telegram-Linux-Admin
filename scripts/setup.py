import json
import os
import stat
import getpass
import subprocess

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


def setup_config():
    """Creates or updates the config.json file."""
    print_header("Configuration Setup")

    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
    else:
        config = {"telegram_token": "", "whitelisted_users": [], "servers": []}

    config['telegram_token'] = get_input("Enter your Telegram Bot Token", config.get('telegram_token'))

    print("\n--- Whitelisted Users ---")
    print("Enter your Telegram user ID to grant access to the bot.")
    print("You can get your user ID by messaging @userinfobot on Telegram.")
    user_id = get_input("Your Telegram User ID")
    if user_id.isdigit():
        config['whitelisted_users'] = [int(user_id)]
    else:
        print("Invalid user ID. Must be a number.")
        config['whitelisted_users'] = []


    while True:
        print("\n--- Server Configuration ---")
        if not config['servers']:
            print("No servers configured yet.")
        else:
            for i, server in enumerate(config['servers']):
                print(f"  {i+1}. {server['alias']} ({server['user']}@{server['hostname']})")

        choice = get_input("\nChoose an action: [a]dd server, [r]emove server, [d]one", "d")
        if choice.lower() == 'a':
            alias = get_input("  Enter a short alias for the server (e.g., 'webserver')")
            hostname = get_input("  Enter the server's hostname or IP address")
            user = get_input("  Enter the SSH username")
            key_path = get_input("  Enter the path to your SSH private key", f"/home/{getpass.getuser()}/.ssh/id_rsa")
            config['servers'].append({"alias": alias, "hostname": hostname, "user": user, "key_path": key_path})
        elif choice.lower() == 'r':
            try:
                index = int(get_input("  Enter the number of the server to remove")) - 1
                if 0 <= index < len(config['servers']):
                    del config['servers'][index]
                else:
                    print("Invalid server number.")
            except ValueError:
                print("Invalid input.")
        elif choice.lower() == 'd':
            break

    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)
    print(f"\nConfiguration successfully saved to {CONFIG_FILE}")

def setup_systemd():
    """Generates and installs a systemd service file."""
    print_header("Systemd Service Setup")
    if os.geteuid() != 0:
        print("This script is not running as root. Cannot install the systemd service.")
        print(f"To install the service, please run 'sudo python3 {os.path.abspath(__file__)} --install-service'")
        return

    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'))
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
ExecStart={python_path} {script_path}
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

        setup_config()

        auto_update = get_input("\nEnable daily automatic updates? (y/n)", "y")
        if auto_update.lower() == 'y':
            setup_cron()

        install_service = get_input("\nInstall the systemd service to run the bot automatically? (y/n)", "y")
        if install_service.lower() == 'y':
            setup_systemd()
