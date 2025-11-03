#!/bin/bash
set -e

echo "----------------------------------------"
echo " Telegram Multi-Server Bot Uninstaller"
echo "----------------------------------------"
echo "This script will permanently remove the bot and all its data."
echo

# Must be run as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script as root (e.g., sudo ./scripts/uninstall.sh)"
  exit 1
fi

read -p "Are you sure you want to completely remove the application? [y/N]: " confirm
if [[ ! "$confirm" =~ ^[yY](es)?$ ]]; then
    echo "Uninstallation cancelled."
    exit 0
fi

echo "--> Stopping and disabling the systemd service..."
systemctl stop telegram_bot.service || true
systemctl disable telegram_bot.service || true

echo "--> Removing systemd service file..."
rm -f /etc/systemd/system/telegram_bot.service
systemctl daemon-reload

echo "--> Removing cron job for auto-updates..."
rm -f /etc/cron.d/telegram_bot_update

# Determine the script's own directory to find the project root
SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )
PROJECT_ROOT=$(realpath "$SCRIPT_DIR/..")

echo "--> Removing application directory: $PROJECT_ROOT"
# The script will be inside the directory it's trying to remove,
# so we move out and remove it in the background.
# A temporary script is created to handle the final deletion.
TMP_UNINSTALL="/tmp/tmp_uninstall_bot.sh"

cat > "$TMP_UNINSTALL" <<- EOM
#!/bin/bash
# Temporary script to remove the final application directory
sleep 1
echo "Finalizing uninstallation..."
rm -rf "$PROJECT_ROOT"
echo "Application directory removed."
rm -- "\$0" # Self-destruct
EOM

chmod +x "$TMP_UNINSTALL"
nohup "$TMP_UNINSTALL" > /dev/null 2>&1 &

echo
echo "Uninstallation process has been initiated."
echo "The application directory will be removed in the background."
echo "Uninstallation complete."
exit 0
