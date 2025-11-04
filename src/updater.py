import subprocess
import sys
import argparse
import logging
import shutil
import os

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
    Includes pre-update checks, backup, and rollback functionality.
    Returns a detailed log of the process.
    """
    update_log = []

    def log_and_append(message):
        logger.info(message)
        if not is_auto:
            update_log.append(message)

    log_and_append("🚀 **Starting Update Process...**")

    # --- Pre-Update Safety Checks ---
    log_and_append("\n**1. Running Pre-Update Safety Checks...**")

    # Check if this is a git repository
    git_check = run_command("git rev-parse --is-inside-work-tree")
    if git_check["returncode"] != 0 or git_check["stdout"] != "true":
        error_message = "❌ **Error:** This does not appear to be a git repository. Cannot update."
        log_and_append(error_message)
        return "\n".join(update_log)

    # Check for uncommitted changes
    status_check = run_command("git status --porcelain")
    if status_check["stdout"]:
        error_message = (
            "❌ **Error:** Uncommitted changes detected. Please commit or stash them before updating.\n"
            f"```\n{status_check['stdout']}\n```"
        )
        log_and_append(error_message)
        return "\n".join(update_log)

    log_and_append("✅ Safety checks passed.")

    # --- Backup ---
    log_and_append("\n**2. Backing up current state...**")

    # Get current commit hash for rollback
    current_hash_result = run_command("git rev-parse HEAD")
    if current_hash_result["returncode"] != 0:
        error_message = "❌ **Error:** Could not get current commit hash. Aborting update."
        log_and_append(error_message)
        return "\n".join(update_log)

    current_hash = current_hash_result["stdout"]
    log_and_append(f"✅ Current commit hash saved: `{current_hash}`")

    # Backup the database
    db_backup_path = ""
    try:
        # Assumes the script is run from the project root
        db_path = 'database.db'
        if os.path.exists(db_path):
            db_backup_path = f"{db_path}.backup"
            shutil.copy2(db_path, db_backup_path)
            log_and_append("✅ Database backed up successfully.")
        else:
            log_and_append("ℹ️ Database file not found, skipping backup.")
    except Exception as e:
        error_message = f"❌ **Error:** Failed to back up database: {e}"
        log_and_append(error_message)
        return "\n".join(update_log)

    try:
        # Step 3: Git Pull
        log_and_append("\n**3. Pulling latest changes from Git...**")
        pull_result = run_command("git pull")
        if pull_result["returncode"] != 0:
            raise Exception(f"Failed to pull updates:\n```\n{pull_result['stderr']}\n```")
        log_and_append(f"✅ Git pull successful.\n```\n{pull_result['stdout']}\n```")

        # Step 4: Update Dependencies
        log_and_append("\n**4. Installing/updating dependencies...**")
        pip_command = f"'{sys.executable}' -m pip install -r requirements.txt"
        pip_result = run_command(pip_command)
        if pip_result["returncode"] != 0:
            raise Exception(f"Failed to update dependencies:\n```\n{pip_result['stderr']}\n```")
        log_and_append("✅ Dependencies are up to date.")

        # Step 5: Restart Bot
        log_and_append("\n**5. Restarting the bot service...**")
        restart_command = "sudo systemctl restart telegram_bot.service"
        restart_result = run_command(restart_command)
        if restart_result["returncode"] != 0:
            raise Exception(
                "Failed to restart the bot service. This is a critical error. "
                "The bot may be offline.\n"
                f"```\n{restart_result['stderr']}\n```"
            )
        log_and_append("✅ Bot service restart command issued successfully.")

        log_and_append("\n🎉 **Update process completed!** The bot is restarting.")

    except Exception as e:
        log_and_append(f"\n❌ **Update Failed:** {e}")
        log_and_append("\n🔄 **Attempting to roll back to the previous version...**")
        rollback_log = rollback(current_hash, db_backup_path)
        update_log.extend(rollback_log)

    finally:
        # Clean up the database backup file
        if db_backup_path and os.path.exists(db_backup_path):
            os.remove(db_backup_path)
            log_and_append("\n🗑️ Cleaned up database backup file.")

    return "\n".join(update_log)


def rollback(commit_hash, db_backup_path):
    """
    Rolls back the repository to a specific commit, restores the database,
    re-installs dependencies, and restarts the bot.
    """
    rollback_log = []

    def log_and_append(message):
        logger.warning(message)
        rollback_log.append(message)

    # 1. Restore Code
    log_and_append("\n**1. Reverting code to the last stable version...**")
    reset_command = f"git reset --hard {commit_hash}"
    reset_result = run_command(reset_command)
    if reset_result["returncode"] != 0:
        log_and_append(f"❌ **CRITICAL FAILURE:** Could not revert code. Manual intervention required.\n```\n{reset_result['stderr']}\n```")
        return rollback_log
    log_and_append("✅ Code reverted successfully.")

    # 2. Restore Database
    if db_backup_path and os.path.exists(db_backup_path):
        log_and_append("\n**2. Restoring database from backup...**")
        try:
            shutil.move(db_backup_path, 'database.db')
            log_and_append("✅ Database restored successfully.")
        except Exception as e:
            log_and_append(f"❌ **CRITICAL FAILURE:** Could not restore database. Manual intervention required.\nError: {e}")
            return rollback_log

    # 3. Re-install old dependencies
    log_and_append("\n**3. Re-installing previous dependencies...**")
    pip_command = f"'{sys.executable}' -m pip install -r requirements.txt"
    pip_result = run_command(pip_command)
    if pip_result["returncode"] != 0:
        log_and_append(f"⚠️ **Warning:** Failed to re-install dependencies. The bot may not start correctly.\n```\n{pip_result['stderr']}\n```")
    else:
        log_and_append("✅ Dependencies re-installed.")

    # 4. Restart Bot
    log_and_append("\n**4. Attempting to restart the bot service...**")
    restart_command = "sudo systemctl restart telegram_bot.service"
    restart_result = run_command(restart_command)
    if restart_result["returncode"] != 0:
        log_and_append(f"❌ **CRITICAL FAILURE:** Could not restart the bot. Manual intervention is likely required.\n```\n{restart_result['stderr']}\n```")
    else:
        log_and_append("✅ Bot service restarted. The system should be back to its pre-update state.")

    return rollback_log

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
