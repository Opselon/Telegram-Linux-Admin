#!/bin/bash
set -e

# --- Color Definitions ---
C_RED="\033[31m"
C_YELLOW="\033[33m"
C_GREEN="\033[32m"
C_RESET="\033[0m"

echo -e "${C_YELLOW}----------------------------------------${C_RESET}"
echo -e "${C_YELLOW} Telegram Multi-Server Bot Uninstaller${C_RESET}"
echo -e "${C_YELLOW}----------------------------------------${C_RESET}"
echo -e "${C_RED}This script will permanently remove the bot and all its data.${C_RESET}"
echo

# Must be run as root
if [ "$EUID" -ne 0 ]; then
  echo -e "${C_RED}Error: Please run this script as root (e.g., sudo ./scripts/uninstall.sh)${C_RESET}"
  exit 1
fi

read -p "Are you sure you want to completely remove the application? [y/N]: " confirm
if [[ ! "$confirm" =~ ^[yY](es)?$ ]]; then
    echo "Uninstallation cancelled."
    exit 0
fi

echo -e "\n${C_YELLOW}--> Stopping and disabling the systemd service...${C_RESET}"
systemctl stop telegram_bot.service > /dev/null 2>&1 || true
systemctl disable telegram_bot.service > /dev/null 2>&1 || true
echo "Service stopped and disabled."

echo -e "\n${C_YELLOW}--> Removing systemd service file...${C_RESET}"
rm -f /etc/systemd/system/telegram_bot.service
systemctl daemon-reload
echo "Systemd file removed."

echo -e "\n${C_YELLOW}--> Removing cron job for auto-updates...${C_RESET}"
rm -f /etc/cron.d/telegram_bot_update
echo "Cron job removed."

# Determine the script's own directory to find the project root
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
PROJECT_ROOT=$(realpath "$SCRIPT_DIR/..")

# Check if the directory exists before trying to remove it
if [ -d "$PROJECT_ROOT" ]; then
    echo -e "\n${C_YELLOW}--> Preparing to remove application directory: $PROJECT_ROOT${C_RESET}"

    # Create a temporary script to handle the final deletion
    TMP_UNINSTALL="/tmp/tmp_uninstall_bot.sh"

    cat > "$TMP_UNINSTALL" <<- EOM
#!/bin/bash
echo "Finalizing uninstallation..."
# Kill any remaining processes that might be using the directory
# fuser -k -9 "$PROJECT_ROOT" || true
sleep 1
rm -rf "$PROJECT_ROOT"
echo "Application directory removed."
rm -- "\$0" # Self-destruct
EOM

    chmod +x "$TMP_UNINSTALL"

    # Execute the temporary script in the background
    nohup "$TMP_UNINSTALL" > /dev/null 2>&1 &

    echo "Removal process has been initiated and will complete in the background."
else
    echo -e "\n${C_YELLOW}Application directory not found, skipping removal.${C_RESET}"
fi

echo -e "\n${C_GREEN}----------------------------------------${C_RESET}"
echo -e "${C_GREEN}      Uninstallation Complete ✅${C_RESET}"
echo -e "${C_GREEN}----------------------------------------${C_RESET}"
echo "All associated system files have been removed."

exit 0
