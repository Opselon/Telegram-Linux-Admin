"""Tests for security-relevant configuration handling."""

from __future__ import annotations

import json
import os

import pytest

from src.config import VALID_ACCESS_MODES, Config


@pytest.fixture()
def config_path(tmp_path):
    return str(tmp_path / "config.json")


def _write(path: str, payload: dict) -> None:
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)


class TestAccessModeConfig:
    def test_defaults_to_open_when_unset(self, config_path, monkeypatch):
        monkeypatch.delenv("TLA_ACCESS_MODE", raising=False)
        config = Config(config_path)
        assert config.access_mode in VALID_ACCESS_MODES

    def test_reads_access_mode_from_file(self, config_path):
        _write(config_path, {"telegram_token": "", "whitelisted_users": [], "access_mode": "whitelist"})
        assert Config(config_path).access_mode == "whitelist"

    def test_invalid_access_mode_falls_back_with_warning(self, config_path):
        _write(config_path, {"access_mode": "everyone"})
        config = Config(config_path)
        assert config.access_mode != "everyone"
        assert any("access_mode" in warning for warning in config.warnings)

    def test_set_access_mode_persists(self, config_path):
        config = Config(config_path)
        assert config.set_access_mode("whitelist") == "whitelist"
        assert Config(config_path).access_mode == "whitelist"

    def test_set_access_mode_rejects_invalid(self, config_path):
        config = Config(config_path)
        with pytest.raises(ValueError):
            config.set_access_mode("nope")

    def test_save_preserves_unknown_keys(self, config_path):
        _write(
            config_path,
            {
                "telegram_token": "",
                "custom_setting": {"nested": [1, 2, 3]},
                "servers": [{"alias": "legacy"}],
            },
        )
        config = Config(config_path)
        config.set_access_mode("admin_only")

        with open(config_path, encoding="utf-8") as handle:
            saved = json.load(handle)
        assert saved["custom_setting"] == {"nested": [1, 2, 3]}
        assert saved["servers"] == [{"alias": "legacy"}]
        assert saved["access_mode"] == "admin_only"


class TestDestinationConfig:
    def test_reads_policy_and_allowlist(self, config_path):
        _write(
            config_path,
            {
                "destination_policy": "allowlist",
                "allowed_destinations": ["10.0.0.0/8", "db.internal"],
            },
        )
        config = Config(config_path)
        assert config.destination_policy == "allowlist"
        assert config.allowed_destinations == ["10.0.0.0/8", "db.internal"]

    def test_accepts_comma_separated_allowlist(self, config_path):
        _write(config_path, {"allowed_destinations": "10.0.0.1, 10.0.0.2"})
        config = Config(config_path)
        assert config.allowed_destinations == ["10.0.0.1", "10.0.0.2"]
