# PURPOSE: Prove the .env loader and the fail-closed access-mode default.
# OWNER: security lane, 3.5.
# CONSUMES: src.config (load_dotenv, Config, VALID_ACCESS_MODES).
# PROVIDES: tests/test_config_env.py.
# INVARIANTS: .env never overrides real environment variables; the default
#          access mode is admin_only when nothing is configured; open mode
#          must be chosen explicitly; setup_pin loads from env and file.
# EXTEND: add config-surface regressions here.

from __future__ import annotations

import importlib
import json
import os

import pytest

import src.config as config_module
from src.config import Config, load_dotenv


@pytest.fixture(autouse=True)
def clean_env(monkeypatch, tmp_path):
    """Keeps .env tests hermetic: clear TLA_* vars, point config at tmp."""
    for var in (
        "TLA_BOT_TOKEN",
        "TELEGRAM_BOT_TOKEN",
        "TLA_ADMIN_ID",
        "ADMIN_USER_ID",
        "TLA_ACCESS_MODE",
        "TLA_SETUP_PIN",
        "TLA_ENV_FILE",
        "TLA_CONFIG_FILE",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("TLA_CONFIG_FILE", str(tmp_path / "config.json"))
    yield


def _reload_config_module():
    importlib.reload(config_module)
    return config_module


class TestLoadDotenv:
    def test_parses_pairs_and_skips_comments(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        env_file.write_text(
            "# comment\n"
            "TLA_BOT_TOKEN=12345:ABCDEF\n"
            'TLA_ACCESS_MODE="whitelist"\n'
            "TLA_ADMIN_ID='100200300'\n"
            "invalid line without equals\n"
            "\n",
            encoding="utf-8",
        )
        applied = load_dotenv(str(env_file))
        assert "TLA_BOT_TOKEN" in applied
        assert os.environ["TLA_BOT_TOKEN"] == "12345:ABCDEF"
        assert os.environ["TLA_ACCESS_MODE"] == "whitelist"
        assert os.environ["TLA_ADMIN_ID"] == "100200300"
        # load_dotenv writes REAL env vars; undo them so other test files
        # never see the pollution.
        for key in applied:
            monkeypatch.delenv(key, raising=False)

    def test_real_env_wins_over_dotenv(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TLA_BOT_TOKEN", "99999:PRIORITY")
        env_file = tmp_path / ".env"
        env_file.write_text("TLA_BOT_TOKEN=11111:LOSES\n", encoding="utf-8")
        load_dotenv(str(env_file))
        assert os.environ["TLA_BOT_TOKEN"] == "99999:PRIORITY"
        monkeypatch.delenv("TLA_BOT_TOKEN", raising=False)

    def test_missing_file_is_noop(self, tmp_path):
        assert load_dotenv(str(tmp_path / "nope.env")) == []

    def test_config_defaults_to_admin_only(self):
        module = _reload_config_module()
        try:
            assert module.DEFAULT_ACCESS_MODE == "admin_only"
            cfg = Config(str(config_module.os.environ["TLA_CONFIG_FILE"]))
            assert cfg.access_mode == "admin_only"
        finally:
            importlib.reload(config_module)

    def test_open_mode_requires_explicit_env(self, monkeypatch):
        monkeypatch.setenv("TLA_ACCESS_MODE", "open")
        module = _reload_config_module()
        try:
            assert module.DEFAULT_ACCESS_MODE == "open"
        finally:
            # Drop the override BEFORE the restore-reload: reload() re-runs
            # the module in place, so it would otherwise bake the override
            # into DEFAULT_ACCESS_MODE for every later test in the process.
            monkeypatch.delenv("TLA_ACCESS_MODE", raising=False)
            importlib.reload(config_module)

    def test_setup_pin_from_env(self, monkeypatch):
        monkeypatch.setenv("TLA_SETUP_PIN", "918273")
        cfg = Config(str(os.environ["TLA_CONFIG_FILE"]))
        assert cfg.setup_pin == "918273"
        monkeypatch.delenv("TLA_SETUP_PIN", raising=False)

    def test_setup_pin_from_file_without_env(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"setup_pin": "554433"}), encoding="utf-8")
        cfg = Config(str(cfg_path))
        assert cfg.setup_pin == "554433"

    def test_env_setup_pin_beats_file_pin(self, tmp_path, monkeypatch):
        monkeypatch.setenv("TLA_SETUP_PIN", "111222")
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"setup_pin": "554433"}), encoding="utf-8")
        cfg = Config(str(cfg_path))
        assert cfg.setup_pin == "111222"
        monkeypatch.delenv("TLA_SETUP_PIN", raising=False)

    def test_setup_pin_round_trips_through_save(self, tmp_path):
        cfg_path = tmp_path / "config.json"
        cfg_path.write_text(json.dumps({"setup_pin": "554433"}), encoding="utf-8")
        cfg = Config(str(cfg_path))
        cfg.set_token("12345:ABCDEF")
        reloaded = json.loads(cfg_path.read_text(encoding="utf-8"))
        # The PIN must survive a config save, never be dropped silently.
        assert reloaded["setup_pin"] == "554433"
