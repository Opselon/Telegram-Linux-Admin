import subprocess
import sys
import argparse
import logging

# Configure logging for the updater script
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler("updater.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

def run_command(command):
    """
    Executes a shell command and returns a dictionary with stdout, stderr, and return code.
    Logs the command and its outcome.
    """
    logger.info(f"Executing command: {command}")
    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300  # 5-minute timeout for commands
        )

        if result.returncode == 0:
            logger.info(f"Command successful. STDOUT:\n{result.stdout.strip()}")
        else:
            logger.error(
                f"Command failed with return code {result.returncode}.\n"
                f"STDOUT:\n{result.stdout.strip()}\n"
                f"STDERR:\n{result.stderr.strip()}"
            )

        return {
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
            "returncode": result.returncode
        }

    except subprocess.TimeoutExpired:
        logger.error(f"Command '{command}' timed out.")
        return {
            "stdout": "",
            "stderr": "Command timed out after 300 seconds.",
            "returncode": -1,
        }
    except Exception as e:
        logger.error(f"An unexpected error occurred while running command '{command}': {e}", exc_info=True)
        return {
            "stdout": "",
            "stderr": f"An unexpected error occurred: {e}",
            "returncode": -1,
        }

def check_for_updates():
    """
    Checks if there are any updates available in the git repository.
    Returns a dictionary with the status and a descriptive message.
    """
    logger.info("Checking for updates...")

    fetch_result = run_command("git fetch")
    if fetch_result["returncode"] != 0:
        return {"status": "error", "message": f"Failed to fetch from remote:\n{fetch_result['stderr']}"}

    local_hash_result = run_command("git rev-parse HEAD")
    if local_hash_result["returncode"] != 0:
        return {"status": "error", "message": f"Failed to get local commit hash:\n{local_hash_result['stderr']}"}

    remote_hash_result = run_command("git rev-parse @{u}")
    if remote_hash_result["returncode"] != 0:
        return {"status": "error", "message": f"Failed to get remote commit hash:\n{remote_hash_result['stderr']}"}

    if local_hash_result["stdout"] == remote_hash_result["stdout"]:
        logger.info("No updates available.")
        return {"status": "no_update", "message": "You are already on the latest version."}
    else:
        logger.info("Update available.")
        return {"status": "update_available", "message": "An update is available! Use /update_bot to apply it."}


def apply_update(is_auto=False):
    """
    Applies the update by pulling the latest changes and restarting the bot.
    Returns a detailed log of the process.
    """
    update_log = []

    def log_and_append(message):
        logger.info(message)
        if not is_auto:
            update_log.append(message)

    log_and_append("🚀 **Starting Update Process...**")

    # 1. Git Pull
    log_and_append("\n**1. Pulling latest changes from Git...**")
    pull_result = run_command("git pull")
    if pull_result["returncode"] != 0:
        error_message = f"❌ **Error:** Failed to pull updates.\n```\n{pull_result['stderr']}\n```"
        log_and_append(error_message)
        return "\n".join(update_log)
    log_and_append(f"✅ Git pull successful.\n```\n{pull_result['stdout']}\n```")

    # 2. Update Dependencies
    log_and_append("\n**2. Installing/updating dependencies...**")
    pip_command = f"'{sys.executable}' -m pip install -r requirements.txt"
    pip_result = run_command(pip_command)
    if pip_result["returncode"] != 0:
        error_message = f"❌ **Error:** Failed to update dependencies.\n```\n{pip_result['stderr']}\n```"
        log_and_append(error_message)
        return "\n".join(update_log)
    log_and_append("✅ Dependencies are up to date.")

    # 3. Restart Bot
    log_and_append("\n**3. Restarting the bot service...**")
    restart_command = "sudo systemctl restart telegram_bot.service"
    restart_result = run_command(restart_command)
    if restart_result["returncode"] != 0:
        error_message = f"❌ **Critical Error:** Failed to restart the bot service.\n" \
                        f"The bot might be down. Please check the server manually.\n" \
                        f"```\n{restart_result['stderr']}\n```"
        log_and_append(error_message)
        return "\n".join(update_log)
    log_and_append("✅ Bot service restart command issued successfully.")

    log_and_append("\n🎉 **Update process completed!** The bot is restarting.")
    return "\n".join(update_log)

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
