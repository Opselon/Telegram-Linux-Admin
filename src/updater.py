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
    Applies a Git update to the application in a safe, transactional manner.

    This function performs the following steps:
    1.  **Safety Checks:** Ensures the directory is a Git repo with no uncommitted changes.
    2.  **Backup:** Saves the current Git commit hash and a copy of the database.
    3.  **Update:** Pulls the latest code, reinstalls dependencies, and restarts the service.
    4.  **Rollback:** If any step in the update fails, it automatically triggers a rollback
        to the pre-update state.
    5.  **Cleanup:** Removes temporary backup files.

    Args:
        is_auto (bool): If True, logs are written to the logger but not collected for display
                        in Telegram (for use in automated cron jobs).

    Returns:
        str: A formatted log of the entire update process.
    """
    update_log = []

    def log_and_append(message):
        """Helper to log messages and append them to the user-facing log."""
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
        pip_command = f"'{sys.executable}' -m pip install -e ."
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

        log_and_append("\n🎉 **Update process completed!** The bot is restarting and will be back online shortly.")

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
    Rolls the application back to a previous state after a failed update.

    This is a critical recovery function that performs the following steps:
    1.  **Code Revert:** Resets the Git repository to the last known good commit.
    2.  **Database Restore:** Restores the database from the backup file.
    3.  **Dependency Re-install:** Re-installs the dependencies to match the old code.
    4.  **Service Restart:** Restarts the bot to bring it back online in its previous state.

    Args:
        commit_hash (str): The Git commit hash to revert to.
        db_backup_path (str): The path to the database backup file.

    Returns:
        list: A list of log messages detailing the rollback process.
    """
    rollback_log = []

    def log_and_append(message):
        """Helper to log rollback messages with a WARNING level."""
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
    pip_command = f"'{sys.executable}' -m pip install -e ."
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

def main():
    parser = argparse.ArgumentParser(description="Telegram Linux Admin Bot Updater")
    parser.add_argument('--auto', action='store_true', help='Run in automated (cron) mode.')
    args = parser.parse_args()

    if args.auto:
        logger.info("Running in automated mode...")
        update_status = check_for_updates()
        if update_status["status"] == "update_available":
            logger.info("Update available, applying automatically.")
            apply_update(is_auto=True)
        else:
            logger.info("No updates available.")
    else:
        print("--- Manual Bot Updater ---")
        update_status = check_for_updates()
        print(f"Status: {update_status['message']}")

        if update_status["status"] == "update_available":
            choice = input("An update is available. Do you want to apply it? (y/n): ").lower()
            if choice == 'y':
                print("Starting update...")
                log_output = apply_update(is_auto=False)
                # We need to strip markdown for the console
                log_output = log_output.replace("**", "").replace("`", "")
                print(log_output)
            else:
                print("Update cancelled.")

if __name__ == '__main__':
    main()
