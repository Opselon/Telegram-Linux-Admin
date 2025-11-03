import json
import os
import stat
import getpass

CONFIG_FILE = 'config.json'
SERVICE_FILE = '/etc/systemd/system/telegram_bot.service'
VENV_PYTHON = 'venv/bin/python'

def get_input(prompt, default=None):
    """Gets user input with an optional default value."""
    response = input(f"{prompt} [{default}]: ")
    return response or default

def setup_config():
    """Creates or updates the config.json file."""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
    else:
        config = {"telegram_token": "", "whitelisted_users": [], "servers": []}

    config['telegram_token'] = get_input("Enter your Telegram Bot Token", config.get('telegram_token'))

    print("\n--- Whitelisted Users ---")
    print("Enter your Telegram user ID. You can get this from bots like @userinfobot.")
    user_id = get_input("Your Telegram User ID")
    if user_id.isdigit():
        config['whitelisted_users'] = [int(user_id)]
    else:
        print("Invalid user ID. Must be a number.")
        config['whitelisted_users'] = []


    while True:
        print("\n--- Server Configuration ---")
        for i, server in enumerate(config['servers']):
            print(f"{i+1}. {server['alias']} ({server['user']}@{server['hostname']})")

        choice = get_input("\n[a]dd, [r]emove, or [d]one?", "d")
        if choice.lower() == 'a':
            alias = get_input("Server Alias")
            hostname = get_input("Hostname")
            user = get_input("User")
            key_path = get_input("Path to SSH private key", f"/home/{getpass.getuser()}/.ssh/id_rsa")
            config['servers'].append({"alias": alias, "hostname": hostname, "user": user, "key_path": key_path})
        elif choice.lower() == 'r':
            try:
                index = int(get_input("Server number to remove")) - 1
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
    print(f"\nConfiguration saved to {CONFIG_FILE}")

def setup_systemd():
    """Generates and installs a systemd service file."""
    if os.geteuid() != 0:
        print("\n--- Systemd Service ---")
        print("This script is not running as root. Cannot install systemd service.")
        print(f"To install the service, run 'sudo python3 {os.path.abspath(__file__)} --install-service'")
        return

    script_path = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'src', 'main.py'))
    python_path = os.path.abspath(VENV_PYTHON)

    if not os.path.exists(python_path):
        print(f"Error: {python_path} not found. Please create a virtual environment.")
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

[Install]
WantedBy=multi-user.target
"""
    try:
        with open(SERVICE_FILE, 'w') as f:
            f.write(service_content)

        os.system('systemctl daemon-reload')
        os.system('systemctl enable telegram_bot.service')
        print(f"\nSystemd service created at {SERVICE_FILE}")
        print("To start the service, run: sudo systemctl start telegram_bot.service")
        print("To see the logs, run: sudo journalctl -u telegram_bot -f")
    except Exception as e:
        print(f"\nError creating systemd service: {e}")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-service', action='store_true', help='Install the systemd service (requires root).')
    args = parser.parse_args()

    if args.install_service:
        setup_systemd()
    else:
        print("--- Telegram Bot Setup Wizard ---")
        setup_config()
        setup_systemd()
