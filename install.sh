#!/bin/bash
set -e

echo "--- Setting up Python virtual environment ---"
python3 -m venv venv
source venv/bin/activate

echo "\n--- Installing dependencies ---"
pip install -r requirements.txt

echo "\n--- Running setup wizard ---"
python3 scripts/setup.py

echo "\n--- Setup complete ---"
echo "To activate the virtual environment, run: source venv/bin/activate"
echo "To run the bot manually, run: python3 src/main.py"
