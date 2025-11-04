#!/bin/bash
set -e

# --- Pre-installation Service Check ---
echo -e "${C_YELLOW}--- Checking for existing bot services ---${C_RESET}"
if systemctl is-active --quiet telegram_bot.service; then
    echo "An old version of the bot service is running. Stopping it now..."
    sudo systemctl stop telegram_bot.service
    echo "Service stopped."
fi

# --- Color Definitions ---
C_BLUE="\033[34m"
C_GREEN="\033[32m"
C_YELLOW="\033[33m"
C_RESET="\033[0m"

echo -e "${C_BLUE}--- Setting up Python virtual environment ---${C_RESET}"
python3 -m venv venv
source venv/bin/activate

echo -e "\n${C_BLUE}--- Installing project in editable mode ---${C_RESET}"
pip install -e .

echo -e "\n${C_BLUE}--- Running setup wizard ---${C_RESET}"
python3 scripts/setup.py

echo -e "\n${C_GREEN}--- Setup complete ---${C_RESET}"
# Start the service and show the logs for a few seconds to confirm it's running
echo -e "\n${C_YELLOW}--- Starting Bot Service ---${C_RESET}"
sudo systemctl daemon-reload
sudo systemctl restart telegram_bot.service
echo -e "${C_GREEN}The bot service has been started. Showing live logs for 15 seconds...${C_RESET}"
# Run journalctl in the background, sleep, then kill it.
sudo journalctl -u telegram_bot.service -f &
LOG_PID=$!
sleep 15
kill $LOG_PID > /dev/null 2>&1 || true # supress "Terminated" message
echo -e "\n${C_GREEN}--- Bot is running in the background. Use 'sudo journalctl -u telegram_bot.service -f' to see logs anytime. ---${C_RESET}"
