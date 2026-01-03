"""Tests for the configuration helper module."""

import json
import os
import stat

import pytest

from src.config import Config, ConfigError, validate_token


def test_missing_file_uses_defaults(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(str(cfg_path))
    assert config.telegram_token == ""
    assert config.whitelisted_users == []
    assert config.last_error is None


def test_save_is_atomic_and_secure(tmp_path):
    cfg_path = tmp_path / "config.json"
    config = Config(str(cfg_path))
    config.set_token("12345:ABCDE")
    config.add_whitelisted_user(100)
    config.add_whitelisted_user("200")

    # File should exist with restrictive permissions.
    mode = stat.S_IMODE(os.stat(cfg_path).st_mode)
    expected_mode = 0o666 if os.name == "nt" else 0o600
    assert mode & expected_mode == expected_mode

    with open(cfg_path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["telegram_token"] == "12345:ABCDE"
    assert data["whitelisted_users"] == [100, 200]

    # Re-loading should preserve values.
    config2 = Config(str(cfg_path))
    assert config2.telegram_token == "12345:ABCDE"
    assert config2.whitelisted_users == [100, 200]


def test_load_config_hardens_permissions(tmp_path):
    if os.name == "nt":
        pytest.skip("Windows permission model differs from POSIX")

    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"telegram_token": "12345:ABCDE"}), encoding="utf-8")
    os.chmod(cfg_path, 0o666)

    config = Config(str(cfg_path))
    assert any("tightened to 0o600" in warning for warning in config.warnings)
    assert stat.S_IMODE(os.stat(cfg_path).st_mode) == 0o600


def test_save_config_warns_when_chmod_fails(tmp_path, monkeypatch):
    cfg_path = tmp_path / "config.json"
    config = Config(str(cfg_path))

    def fake_chmod(path, mode):
        raise PermissionError("denied")

    monkeypatch.setattr("src.config.os.chmod", fake_chmod)

    config.set_token("12345:ABCDE")

    assert any("Unable to enforce secure permissions" in warning for warning in config.warnings)


@pytest.mark.parametrize(
    "token",
    [
        "12345:ABCDE",
        "999999999:secret",
        "12345:abc_def-XYZ",
    ],
)
def test_validate_token_accepts_valid_strings(token):
    assert validate_token(token) == token


def test_validate_token_trims_whitespace():
    assert validate_token(" 12345:ABCDE ") == "12345:ABCDE"


@pytest.mark.parametrize(
    "token",
    ["", "no-colon", "abc:def", "12345:", ":secret", "12345:with space"],
)
def test_validate_token_rejects_invalid_strings(token):
    with pytest.raises(ValueError):
        validate_token(token)


def test_invalid_json_sets_error(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text("{invalid", encoding="utf-8")

    config = Config(str(cfg_path))
    assert isinstance(config.last_error, ConfigError)
    assert config.telegram_token == ""
    assert config.whitelisted_users == []


def test_replace_whitelist_sanitizes(tmp_path):
    config = Config(str(tmp_path / "config.json"))
    config.replace_whitelist(["001", "2", "bad", -1, 2])
    assert config.whitelisted_users == [1, 2]


def test_set_token_requires_valid_format(tmp_path):
    config = Config(str(tmp_path / "config.json"))
    with pytest.raises(ValueError):
        config.set_token("invalid-token")


def test_load_config_with_invalid_token_records_error(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"telegram_token": "123abc"}), encoding="utf-8")

    config = Config(str(cfg_path))
    assert config.telegram_token == ""
    assert isinstance(config.last_error, ConfigError)


def test_load_config_with_non_string_token_records_error(tmp_path):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"telegram_token": 12345}), encoding="utf-8")

    config = Config(str(cfg_path))
    assert config.telegram_token == ""
    assert isinstance(config.last_error, ConfigError)
