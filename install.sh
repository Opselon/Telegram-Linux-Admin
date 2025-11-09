#!/bin/bash
set -e

# --- Pre-installation Service Check ---
echo -e "${C_YELLOW}--- Checking for existing bot services ---${C_RESET}"
if systemctl is-active --quiet telegram_bot.service; then
    echo "An old version of the bot service is running. Attempting a graceful shutdown..."
    # Try to stop the service gracefully with a 10-second timeout.
    if ! sudo timeout 10s systemctl stop telegram_bot.service; then
        echo "Service did not stop gracefully. Forcefully terminating..."
        # Find the PID of the bot process
        BOT_PID=$(pgrep -f "tla-bot")
        if [ -n "$BOT_PID" ]; then
            sudo kill -9 "$BOT_PID"
            echo "Process terminated."
        else
            echo "Could not find the bot process to terminate."
        fi
        # Verify it's stopped
        if systemctl is-active --quiet telegram_bot.service; then
             echo "Warning: Service still appears to be active."
        else
             echo "Service stopped."
        fi
    else
        echo "Service stopped gracefully."
    fi
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
yes | python3 scripts/setup.py

echo -e "\n${C_GREEN}--- Setup complete ---${C_RESET}"
