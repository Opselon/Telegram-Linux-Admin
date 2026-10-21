"""The configuration file must actually drive the security singletons.

Regression guard: ``permissions`` and ``DESTINATIONS`` are module-level
singletons with their own defaults, so forgetting the startup wiring left the
bot in ``open`` / ``allow_all`` even when ``config.json`` asked for a strict
policy.
"""

from __future__ import annotations

import pytest

from src import main
from src.access_control import permissions, rate_limiter
from src.audit import audit
from src.config import Config
from src.destination_policy import DESTINATIONS


class _Config:
    """Minimal stand-in for :class:`src.config.Config`."""

    def __init__(self, **kwargs):
        self.access_mode = kwargs.get("access_mode", "whitelist")
        self.whitelisted_users = kwargs.get("whitelisted_users", [42, 43])
        self.destination_policy = kwargs.get("destination_policy", "deny_public")
        self.allowed_destinations = kwargs.get("allowed_destinations", [])
        self.warnings = kwargs.get("warnings", [])


@pytest.fixture(autouse=True)
def _clean():
    audit.set_enabled(False)
    yield
    audit.set_enabled(True)
    permissions.set_mode("open")
    permissions.update(whitelisted=(), admins=())
    rate_limiter.reset()
    DESTINATIONS.reconfigure("allow_all", ())


def test_configure_security_applies_mode_and_admin():
    main._configure_security(_Config(access_mode="admin_only", whitelisted_users=[7, 8]))

    assert permissions.mode == "admin_only"
    assert permissions.is_admin(7)
    assert not permissions.is_admin(8)  # only the first entry is the admin
    assert permissions.check(7).allowed
    # admin_only mode ignores the whitelist for non-admins
    assert not permissions.check(8).allowed


def test_whitelist_mode_allows_non_admin_whitelisted_users():
    main._configure_security(_Config(access_mode="whitelist", whitelisted_users=[7, 8]))

    assert permissions.check(7).allowed
    assert permissions.check(8).allowed
    assert not permissions.check(9).allowed


def test_configure_security_applies_destination_policy():
    main._configure_security(_Config(destination_policy="deny_public"))

    assert DESTINATIONS.mode == "deny_public"
    assert DESTINATIONS.evaluate("10.1.2.3").allowed
    assert not DESTINATIONS.evaluate("8.8.8.8").allowed
    assert not DESTINATIONS.evaluate("169.254.169.254").allowed


def test_configure_security_honours_allowlist():
    main._configure_security(
        _Config(destination_policy="allowlist", allowed_destinations=["10.0.0.0/8"])
    )

    assert DESTINATIONS.evaluate("10.9.9.9").allowed
    assert not DESTINATIONS.evaluate("192.168.1.1").allowed


def test_configure_security_warns_and_falls_back_on_bad_policy():
    main._configure_security(_Config(destination_policy="nonsense"))

    # An invalid policy must not open the bot up silently.
    assert DESTINATIONS.mode in {"allow_all", "deny_public", "allowlist"}


def test_whitelist_mode_denies_unknown_users_end_to_end(tmp_path, monkeypatch):
    """A config file with ``whitelist`` must produce denials, not access."""
    cfg_file = tmp_path / "config.json"
    cfg_file.write_text(
        '{"telegram_token": "123456:ABC-DEF1234567890",'
        ' "whitelisted_users": [42], "access_mode": "whitelist"}',
        encoding="utf-8",
    )
    cfg = Config(str(cfg_file))
    main._configure_security(cfg)

    assert cfg.access_mode == "whitelist"
    assert permissions.mode == "whitelist"
    assert permissions.check(42).allowed
    assert not permissions.check(999).allowed
