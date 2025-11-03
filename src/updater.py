import subprocess
import sys
import argparse

def run_command(command):
    """Executes a shell command and returns its output."""
    try:
        result = subprocess.run(
            command,
            shell=True,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return result.stdout.strip()
    except subprocess.CalledProcessError as e:
        return f"Error: {e.stderr.strip()}"

def check_for_updates():
    """Checks if there are any updates available in the git repository."""
    run_command("git fetch")
    local_hash = run_command("git rev-parse HEAD")
    remote_hash = run_command("git rev-parse @{u}")

    if local_hash == remote_hash:
        return "You are already on the latest version."
    else:
        return "An update is available! Use /update_bot to apply it."

def apply_update(is_auto=False):
    """Applies the update by pulling the latest changes and restarting the bot."""
    pull_output = run_command("git pull")
    if "Error:" in pull_output:
        if not is_auto:
            return f"Failed to pull updates: {pull_output}"
        else:
            print(f"Auto-update failed to pull: {pull_output}")
            return

    pip_output = run_command(f"{sys.executable} -m pip install -r requirements.txt")
    if "Error:" in pip_output:
        if not is_auto:
            return f"Failed to update dependencies: {pip_output}"
        else:
            print(f"Auto-update failed to install dependencies: {pip_output}")
            return

    restart_output = run_command("sudo systemctl restart telegram_bot.service")
    if "Error:" in restart_output:
        if not is_auto:
            return f"Failed to restart the bot: {restart_output}"
        else:
            print(f"Auto-update failed to restart bot: {restart_output}")
            return

    if not is_auto:
        return "Update applied successfully! The bot is restarting."
    else:
        print("Auto-update applied successfully.")

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--auto', action='store_true', help='Run in automated (cron) mode.')
    args = parser.parse_args()

    if args.auto:
        update_status = check_for_updates()
        if "An update is available!" in update_status:
            apply_update(is_auto=True)
        else:
            print("No updates available.")
    else:
        # This part is for manual execution, which is not the primary use case of this script
        print("This script is intended to be run with the --auto flag by a cron job.")
