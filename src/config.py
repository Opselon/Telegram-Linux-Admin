import json
import os

CONFIG_FILE = 'config.json'

class Config:
    def __init__(self):
        self.telegram_token = ""
        self.whitelisted_users = []
        self.load_config()

    def load_config(self):
        try:
            with open(CONFIG_FILE, 'r') as f:
                config_data = json.load(f)
                self.telegram_token = config_data.get("telegram_token", "")
                self.whitelisted_users = config_data.get("whitelisted_users", [])
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    def save_config(self):
        config_data = {
            "telegram_token": self.telegram_token,
            "whitelisted_users": self.whitelisted_users,
        }
        with open(CONFIG_FILE, 'w') as f:
            json.dump(config_data, f, indent=2)

# Singleton instance
config = Config()
