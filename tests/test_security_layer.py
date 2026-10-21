"""Tests for the security primitives: access modes, validators, command policy."""

from __future__ import annotations

import pytest

from src.access_control import (
    ADMIN_ONLY,
    OPEN,
    WHITELIST,
    PermissionManager,
    RateLimiter,
)
from src.command_policy import (
    ConfirmationStore,
    Risk,
    classify,
    validate_command_text,
)
from src.validators import (
    validate_alias,
    validate_hostname,
    validate_key_path,
    validate_port,
    validate_username,
)


# ---------------------------------------------------------------------------
# Access control
# ---------------------------------------------------------------------------
class TestPermissionManager:
    def test_open_mode_allows_everyone(self):
        manager = PermissionManager(OPEN)
        assert manager.check(999).allowed is True
        assert manager.check(None).allowed is False

    def test_whitelist_mode_denies_strangers(self):
        manager = PermissionManager(WHITELIST, whitelisted=[1, 2], admins=[1])
        assert manager.check(1).allowed is True
        assert manager.check(2).allowed is True
        denied = manager.check(3)
        assert denied.allowed is False
        assert denied.key == "access_denied_not_whitelisted"

    def test_admin_only_mode_denies_whitelisted_non_admins(self):
        manager = PermissionManager(ADMIN_ONLY, whitelisted=[1, 2], admins=[1])
        assert manager.check(1).allowed is True
        assert manager.check(2).allowed is False

    def test_admin_always_allowed_and_mode_switchable(self):
        manager = PermissionManager(WHITELIST, whitelisted=[], admins=[7])
        assert manager.check(7).allowed is True
        manager.set_mode(OPEN)
        assert manager.mode == OPEN
        assert manager.check(42).allowed is True

    def test_empty_whitelist_fails_closed(self):
        manager = PermissionManager(WHITELIST)
        assert manager.check(5).allowed is False

    def test_invalid_mode_is_ignored(self):
        manager = PermissionManager(WHITELIST, whitelisted=[1])
        manager.set_mode("bogus")
        assert manager.mode == WHITELIST


class TestRateLimiter:
    def test_allows_up_to_budget_then_denies(self):
        limiter = RateLimiter()
        results = [limiter.check(1, "cmd", max_calls=3, period=60) for _ in range(4)]
        assert [allowed for allowed, _ in results] == [True, True, True, False]
        allowed, retry_after = results[-1]
        assert allowed is False
        assert 0 < retry_after <= 60

    def test_budget_is_per_user_and_scope(self):
        limiter = RateLimiter()
        assert limiter.check(1, "cmd", max_calls=1, period=60)[0] is True
        assert limiter.check(2, "cmd", max_calls=1, period=60)[0] is True
        assert limiter.check(1, "other", max_calls=1, period=60)[0] is True
        assert limiter.check(1, "cmd", max_calls=1, period=60)[0] is False

    def test_reset_clears_budget(self):
        limiter = RateLimiter()
        limiter.check(1, "cmd", max_calls=1, period=60)
        assert limiter.check(1, "cmd", max_calls=1, period=60)[0] is False
        limiter.reset(1, "cmd")
        assert limiter.check(1, "cmd", max_calls=1, period=60)[0] is True

    def test_none_user_is_not_limited(self):
        limiter = RateLimiter()
        assert limiter.check(None, "cmd", max_calls=0, period=60)[0] is True


# ---------------------------------------------------------------------------
# Command policy
# ---------------------------------------------------------------------------
class TestCommandClassification:
    @pytest.mark.parametrize(
        "command",
        [
            "ls -la /var/log",
            "systemctl status nginx",
            "docker ps -a",
            "df -h",
            "echo hello",
            "apt update",
            "tail -n 100 /var/log/syslog",
        ],
    )
    def test_safe_commands(self, command):
        assert classify(command).risk is Risk.SAFE

    @pytest.mark.parametrize(
        "command",
        [
            "rm -rf /",
            "rm -rf --no-preserve-root /",
            "sudo rm -rf /*",
            "mkfs.ext4 /dev/sda1",
            "wipefs -a /dev/nvme0n1",
            "dd if=/dev/zero of=/dev/sda bs=1M",
            "echo x > /dev/sda",
            ":(){ :|:& };:",
            "chmod -R 000 /",
            "kill -9 -1",
            "find / -delete",
            "rm -rf /etc",
        ],
    )
    def test_catastrophic_commands_are_blocked(self, command):
        assert classify(command).risk is Risk.BLOCKED, command

    @pytest.mark.parametrize(
        "command",
        [
            "reboot",
            "shutdown -h now",
            "systemctl stop sshd",
            "iptables -F",
            "ufw disable",
            "rm -rf /tmp/cache",
            "crontab -r",
            "apt purge nginx",
            "docker system prune -af",
            "curl -sSL https://example.com/install.sh | bash",
            "passwd root",
        ],
    )
    def test_disruptive_commands_need_confirmation(self, command):
        assert classify(command).risk is Risk.CONFIRM, command

    def test_nested_shell_payloads_are_inspected(self):
        assert classify("bash -c 'rm -rf /'").risk is Risk.BLOCKED
        assert classify('sh -c "reboot"').risk is Risk.CONFIRM
        assert classify("bash -lc \"bash -c 'mkfs.ext4 /dev/sdb'\"").risk is Risk.BLOCKED

    def test_quoted_text_does_not_false_positive(self):
        # A quoted string that merely mentions a pattern is still inspected —
        # but a plain echo of a path is safe.
        assert classify("echo '/var/log'").risk is Risk.SAFE
        assert classify("grep -r 'reboot' /var/log").risk is Risk.CONFIRM  # conservative


class TestCommandTextValidation:
    def test_accepts_normal_command(self):
        assert validate_command_text("uptime") == "uptime"

    @pytest.mark.parametrize("command", ["", "   ", "a\x00b", "x" * 5000])
    def test_rejects_invalid(self, command):
        with pytest.raises(ValueError):
            validate_command_text(command)


class TestConfirmationStore:
    def test_single_use_token_bound_to_user(self):
        store = ConfirmationStore()
        assessment = classify("reboot")
        pending = store.create(1, "web", "reboot", assessment)

        assert store.pop(2, pending.token) is None  # wrong user
        assert store.pop(1, "nope") is None  # wrong token
        assert store.pop(1, pending.token) is not None
        assert store.pop(1, pending.token) is None  # single use

    def test_expired_confirmation_is_rejected(self):
        now = [1000.0]
        store = ConfirmationStore(ttl=30, clock=lambda: now[0])
        pending = store.create(1, "web", "reboot", classify("reboot"))
        now[0] += 31
        assert store.pop(1, pending.token) is None

    def test_cancel_removes_pending(self):
        store = ConfirmationStore()
        store.create(1, "web", "reboot", classify("reboot"))
        assert store.cancel(1) is True
        assert store.cancel(1) is False

    def test_sweep_drops_expired(self):
        now = [0.0]
        store = ConfirmationStore(ttl=10, clock=lambda: now[0])
        store.create(1, "a", "reboot", classify("reboot"))
        store.create(2, "b", "reboot", classify("reboot"))
        now[0] = 11
        assert store.sweep() == 2
        assert len(store) == 0


# ---------------------------------------------------------------------------
# Validators
# ---------------------------------------------------------------------------
class TestValidators:
    def test_alias(self):
        assert validate_alias(" web-1 ") == "web-1"
        for bad in ["", "-web", "a" * 33, "web server", "web;rm", "../x"]:
            with pytest.raises(ValueError):
                validate_alias(bad)

    def test_hostname(self):
        assert validate_hostname("example.com") == "example.com"
        assert validate_hostname("10.0.0.5") == "10.0.0.5"
        assert validate_hostname("2001:db8::1") == "2001:db8::1"
        for bad in ["", "http://x", "user@host", "bad host", "-bad.com", "a..b"]:
            with pytest.raises(ValueError):
                validate_hostname(bad)

    def test_username(self):
        assert validate_username("root") == "root"
        for bad in ["", "Root", "1root", "a b", "a;id"]:
            with pytest.raises(ValueError):
                validate_username(bad)

    def test_key_path(self):
        assert validate_key_path("/root/.ssh/id_ed25519") == "/root/.ssh/id_ed25519"
        for bad in ["", "relative/key", "/root/$(id)", "/root/`id`", "/root/key;rm"]:
            with pytest.raises(ValueError):
                validate_key_path(bad)

    def test_port(self):
        assert validate_port("2222") == 2222
        for bad in [0, 70000, "abc"]:
            with pytest.raises(ValueError):
                validate_port(bad)
