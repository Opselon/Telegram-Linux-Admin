package config

import (
	"encoding/json"
	"os"
)

// Config holds the application configuration.
type Config struct {
	TelegramBotToken      string `json:"telegram_bot_token"`
	TelegramChatID        string `json:"telegram_chat_id"`
	EnableAutoMaintenance bool   `json:"enable_auto_maintenance"`
}

// LoadConfig loads the configuration from a JSON file.
func LoadConfig(path string) (*Config, error) {
	file, err := os.Open(path)
	if err != nil {
		return nil, err
	}
	defer file.Close()

	cfg := &Config{}
	decoder := json.NewDecoder(file)
	err = decoder.Decode(cfg)
	if err != nil {
		return nil, err
	}

	return cfg, nil
}

// SaveConfig saves the configuration to a JSON file.
func SaveConfig(path string, cfg *Config) error {
	file, err := os.Create(path)
	if err != nil {
		return err
	}
	defer file.Close()

	encoder := json.NewEncoder(file)
	encoder.SetIndent("", "  ")
	err = encoder.Encode(cfg)
	if err != nil {
		return err
	}

	return nil
}
