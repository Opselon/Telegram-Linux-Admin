"""Tests for the policy profile table, the config loader and read-only detection.

PURPOSE
    Pin the authority envelope of each profile, prove the ``config.json``
    loader tolerates every kind of garbage an operator can put in it, and pin
    the read-only allowlist (the one place READONLY gets its power from).

OWNER
    lane policy (tests/test_policy_*.py).

CONSUMES
    ``src.policy.profiles`` and, for the integration case, ``src.config``.

PROVIDES
    Regression cover for profile rules, ``load_profiles``, ``profile_for`` and
    ``is_read_only_command``.

INVARIANTS
    * No real host, path or token is used: temp dirs and example.com only.
    * Tests assert public behaviour, never private attribute names.

EXTEND
    New read-only binaries belong in the parametrized allow case; new
    write-capable subcommands belong in the reject case.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config import Config
from src.policy.profiles import (
    COMMAND_PROFILES_KEY,
    Profile,
    ProfileRules,
    is_read_only_command,
    load_profiles,
    normalized_services,
    parse_profile,
    profile_for,
    read_only_violation,
    rules_for,
)

ALL_PROFILES = (Profile.READONLY, Profile.STANDARD, Profile.FULL, Profile.LOCKED)


# ---------------------------------------------------------------------------
# Profile enum + rules table
# ---------------------------------------------------------------------------


def test_profile_values_are_the_contract_strings() -> None:
    """Wiring and config files carry these literals, so they must not drift."""

    assert {member.value for member in Profile} == {
        "readonly",
        "standard",
        "full",
        "locked",
    }
    assert isinstance(Profile.STANDARD, str)  # JSON-friendly without .value


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_rules_for_every_profile_is_self_consistent(profile: Profile) -> None:
    """Every profile has a rules object whose profile field matches its key."""

    rules = rules_for(profile)
    assert isinstance(rules, ProfileRules)
    assert rules.profile is profile
    assert rules.max_command_bytes > 0
    assert isinstance(rules.deny_patterns, tuple)


@pytest.mark.parametrize(
    "profile, allow_confirm, allow_blocked, read_only",
    [
        (Profile.READONLY, False, False, True),
        (Profile.STANDARD, True, False, False),
        (Profile.FULL, True, False, False),
        (Profile.LOCKED, False, False, True),
    ],
)
def test_profile_authority_matrix(
    profile: Profile, allow_confirm: bool, allow_blocked: bool, read_only: bool
) -> None:
    """The four profiles differ exactly in the bands they admit."""

    rules = rules_for(profile)
    assert rules.allow_confirm is allow_confirm
    assert rules.allow_blocked is allow_blocked
    assert rules.read_only is read_only
    assert rules.allow_services is None  # no service allowlist by default


def test_readonly_caps_command_size_below_the_global_ceiling() -> None:
    """A read-only session is for inspection, so it takes a tighter size cap."""

    assert rules_for(Profile.READONLY).max_command_bytes < rules_for(Profile.STANDARD).max_command_bytes


def test_full_does_not_allow_blocked_by_default() -> None:
    """Break-glass means unconfirmed, not catastrophic: BLOCKED stays off."""

    assert rules_for(Profile.FULL).allow_blocked is False


@pytest.mark.parametrize("garbage", ["", "god", "ROOT", None, 3, object(), ["full"]])
def test_rules_for_rejects_unknown_profiles(garbage: object) -> None:
    """An unknown profile must fail loudly instead of inheriting STANDARD."""

    with pytest.raises(ValueError):
        rules_for(garbage)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("full", Profile.FULL),
        (" FULL ", Profile.FULL),
        ("ReadOnly", Profile.READONLY),
        (Profile.LOCKED, Profile.LOCKED),
    ],
)
def test_parse_profile_accepts_config_style_values(raw: object, expected: Profile) -> None:
    """Config values arrive as loosely-typed strings; normalize, don't crash."""

    assert parse_profile(raw) is expected


@pytest.mark.parametrize("raw", ["", "standrad", None, 7, {"profile": "full"}, []])
def test_parse_profile_returns_none_for_garbage(raw: object) -> None:
    """The loader uses ``None`` as its skip signal, so garbage must map to it."""

    assert parse_profile(raw) is None


# ---------------------------------------------------------------------------
# Read-only detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "cat /var/log/syslog",
        "tail -n 20 /var/log/syslog",
        "head -n 5 /etc/hosts",
        "grep -r example /etc/hosts",
        "journalctl -u ssh --no-pager",
        "df -h",
        "du -sh /var",
        "free -m",
        "uptime",
        "ps aux",
        "systemctl status ssh",
        "systemctl is-active ssh",
        "docker ps",
        "docker logs web",
        "dmesg",
        "whoami",
        "uname -a",
    ],
)
def test_read_only_allowlist_accepts_observational_commands(command: str) -> None:
    """These are the commands an on-call operator needs at 3am."""

    assert is_read_only_command(command) is True
    assert read_only_violation(command) is None


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf /tmp/cache",
        "systemctl restart ssh",
        "systemctl stop ssh",
        "docker rm -f web",
        "journalctl --vacuum-time=1d",
        "date -s 2020-01-01",
        "hostname box2",
        "ls | grep hosts",
        "ls > /tmp/listing",
        "cat /etc/hosts && rm -rf /tmp/x",
        "echo $(rm -rf /tmp/x)",
        "python3 /tmp/script.py",
        "bash -c 'ls -la'",
    ],
)
def test_read_only_denies_mutating_or_composed_commands(command: str) -> None:
    """Anything not positively provably observational is refused."""

    assert is_read_only_command(command) is False
    assert read_only_violation(command)


@pytest.mark.parametrize("command", ["", "   ", "\x00ls"])
def test_read_only_denies_degenerate_input(command: str) -> None:
    """Empty and NUL-bearing commands have no read-only interpretation."""

    assert is_read_only_command(command) is False


def test_read_only_violation_explains_the_reason() -> None:
    """The operator must be told which binary or metacharacter tripped it."""

    assert "systemctl" in (read_only_violation("systemctl restart ssh") or "")
    assert "metacharacter" in (read_only_violation("ls | grep x") or "")


# ---------------------------------------------------------------------------
# config.json loader
# ---------------------------------------------------------------------------


def _write_config(path: Path, payload: object) -> Path:
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_load_profiles_reads_the_config_section(tmp_path: Path) -> None:
    """The documented shape: alias -> profile under 'command_profiles'."""

    config_path = _write_config(
        tmp_path / "config.json",
        {
            "telegram_token": "123456:TEST-TOKEN",
            "servers": [{"alias": "prod", "hostname": "10.0.0.5", "user": "ops"}],
            COMMAND_PROFILES_KEY: {"prod": "readonly", "lab": "full", "self": "standard"},
        },
    )

    assert load_profiles(config_path) == {
        "prod": Profile.READONLY,
        "lab": Profile.FULL,
        "self": Profile.STANDARD,
    }


def test_load_profiles_skips_garbage_entries(tmp_path: Path) -> None:
    """One bad alias must not disable profiling for every other host."""

    config_path = _write_config(
        tmp_path / "config.json",
        {
            COMMAND_PROFILES_KEY: {
                "good": "full",
                "unknown-profile": "root",
                "int-value": 5,
                "null-value": None,
                "": "full",
                "  spaced  ": " READONLY ",
            }
        },
    )

    assert load_profiles(config_path) == {"good": Profile.FULL, "spaced": Profile.READONLY}


def test_load_profiles_missing_file_is_empty(tmp_path: Path) -> None:
    """A bot must boot before its config file exists."""

    assert load_profiles(tmp_path / "absent.json") == {}


def test_load_profiles_broken_json_is_empty(tmp_path: Path) -> None:
    """Corrupt JSON must degrade to defaults, never raise at import time."""

    broken = tmp_path / "config.json"
    broken.write_text("{not json", encoding="utf-8")
    assert load_profiles(broken) == {}


@pytest.mark.parametrize("payload", [["full"], "full", 3, None])
def test_load_profiles_non_mapping_section_is_empty(tmp_path: Path, payload: object) -> None:
    """A list or a bare string where a mapping belongs is ignored."""

    config_path = _write_config(tmp_path / "config.json", {COMMAND_PROFILES_KEY: payload})
    assert load_profiles(config_path) == {}


def test_load_profiles_section_absent_is_empty(tmp_path: Path) -> None:
    """Existing config files predate this feature and have no key at all."""

    config_path = _write_config(tmp_path / "config.json", {"telegram_token": ""})
    assert load_profiles(config_path) == {}


def test_load_profiles_accepts_preloaded_data() -> None:
    """Callers that already parsed config.json avoid a second read."""

    data = {COMMAND_PROFILES_KEY: {"prod": "readonly"}}
    assert load_profiles(data=data) == {"prod": Profile.READONLY}
    assert load_profiles("/nonexistent/ignored.json", data=data) == {"prod": Profile.READONLY}


def test_load_profiles_works_with_the_real_config_object(tmp_path: Path) -> None:
    """Integration: the loader consumes what ``Config`` actually exposes."""

    config_path = _write_config(
        tmp_path / "config.json",
        {
            "telegram_token": "123456:TEST-TOKEN",
            "whitelisted_users": [123456789],
            "access_mode": "whitelist",
            COMMAND_PROFILES_KEY: {"prod": "readonly"},
        },
    )
    config = Config(str(config_path))

    assert config.last_error is None
    assert load_profiles(data=config.extra) == {"prod": Profile.READONLY}
    assert load_profiles(config_path) == {"prod": Profile.READONLY}


@pytest.mark.parametrize(
    "alias, expected",
    [("prod", Profile.READONLY), ("absent", Profile.STANDARD), ("", Profile.STANDARD), (None, Profile.STANDARD)],
)
def test_profile_for_falls_back_to_the_default(alias: str | None, expected: Profile) -> None:
    """An unprofiled host must get the safe default, not READONLY or FULL."""

    assert profile_for(alias, {"prod": Profile.READONLY}) is expected
    assert profile_for(alias, None) is Profile.STANDARD


def test_profile_for_allows_a_custom_default() -> None:
    """Self-hosted installs may want FULL-by-default for their own box."""

    assert profile_for("absent", {}, default=Profile.FULL) is Profile.FULL


@pytest.mark.parametrize(
    "services, expected",
    [
        (None, None),
        ("nginx", ("nginx",)),
        (["nginx", " ssh.service "], ("nginx", "ssh")),
        (["", "  "], ()),
        ((), ()),
    ],
)
def test_normalized_services(
    services: object, expected: tuple[str, ...] | None
) -> None:
    """``None`` means all services; an empty tuple means none."""

    assert normalized_services(services) == expected  # type: ignore[arg-type]
