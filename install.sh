#!/bin/bash
set -e

echo "--- Setting up Python virtual environment ---"
python3 -m venv venv
source venv/bin/activate

echo "\n--- Installing project in editable mode ---"
pip install -e .

echo "\n--- Running setup wizard ---"
python3 scripts/setup.py

echo "\n--- Setup complete ---"
# Start the service and show the logs for a few seconds to confirm it's running
echo -e "\n--- Starting Bot Service ---"
sudo systemctl daemon-reload
sudo systemctl restart telegram_bot.service
echo "The bot service has been started. Showing live logs for 15 seconds..."
# Run journalctl in the background, sleep, then kill it.
sudo journalctl -u telegram_bot.service -f &
LOG_PID=$!
sleep 15
kill $LOG_PID > /dev/null 2>&1 || true # supress "Terminated" message
echo -e "\n--- Bot is running in the background. Use 'sudo journalctl -u telegram_bot.service -f' to see logs anytime. ---"
