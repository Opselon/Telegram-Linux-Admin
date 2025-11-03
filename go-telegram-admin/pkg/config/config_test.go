package config

import (
	"os"
	"testing"
)

func TestConfig(t *testing.T) {
	cfg := &Config{
		TelegramBotToken:      "test_token",
		TelegramChatID:        "12345",
		EnableAutoMaintenance: true,
	}

	path := "test_config.json"
	defer os.Remove(path)

	err := SaveConfig(path, cfg)
	if err != nil {
		t.Fatalf("failed to save config: %v", err)
	}

	loadedCfg, err := LoadConfig(path)
	if err != nil {
		t.Fatalf("failed to load config: %v", err)
	}

	if loadedCfg.TelegramBotToken != cfg.TelegramBotToken {
		t.Errorf("expected token %s, got %s", cfg.TelegramBotToken, loadedCfg.TelegramBotToken)
	}

	if loadedCfg.TelegramChatID != cfg.TelegramChatID {
		t.Errorf("expected chat id %s, got %s", cfg.TelegramChatID, loadedCfg.TelegramChatID)
	}

	if loadedCfg.EnableAutoMaintenance != cfg.EnableAutoMaintenance {
		t.Errorf("expected auto maintenance %v, got %v", cfg.EnableAutoMaintenance, loadedCfg.EnableAutoMaintenance)
	}
}
