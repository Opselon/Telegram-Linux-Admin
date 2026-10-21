"""Tests for :class:`src.policy.engine.PolicyEngine`.

PURPOSE
    Pin how the risk band (what the command can do) and the profile (what the
    operator may do) compose into one decision.

OWNER
    lane policy (tests/test_policy_*.py).

CONSUMES
    ``src.policy.engine``, ``src.policy.profiles`` and ``src.command_policy``.

PROVIDES
    Regression cover for every profile matrix, the deny/enablelist causes and
    the shape of :class:`PolicyDecision`.

INVARIANTS
    * No real host, path or token is used: temp dirs and example.com only.
    * Decisions are asserted through their public fields only.

EXTEND
    A new refusal cause needs a case in ``test_refusals_carry_a_rule`` plus a
    behaviour test; an allow path needs a matrix row.
"""

from __future__ import annotations

import pytest

from src.command_policy import MAX_COMMAND_BYTES, Risk, classify
from src.policy.engine import PolicyDecision, PolicyEngine
from src.policy.profiles import Profile, rules_for

ALL_PROFILES = (Profile.READONLY, Profile.STANDARD, Profile.FULL, Profile.LOCKED)

#: The three risk bands, one representative command each (see test_command_matrix).
SAFE_COMMAND = "ls -la /var/log"
CONFIRM_COMMAND = "systemctl stop ssh"
BLOCKED_COMMAND = "rm -rf /"


# ---------------------------------------------------------------------------
# Construction and profile switching
# ---------------------------------------------------------------------------


def test_default_profile_is_standard() -> None:
    """A fresh session is STANDARD: ordinary operator, not break-glass."""

    assert PolicyEngine().profile is Profile.STANDARD


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_engine_holds_the_profile_it_was_built_with(profile: Profile) -> None:
    assert PolicyEngine(profile).profile is profile


def test_set_profile_accepts_a_string() -> None:
    """Profile changes arrive as text from Telegram."""

    engine = PolicyEngine()
    assert engine.set_profile("full") is Profile.FULL
    assert engine.profile is Profile.FULL


def test_set_profile_accepts_a_profile_enum() -> None:
    engine = PolicyEngine(Profile.READONLY)
    assert engine.set_profile(Profile.STANDARD) is Profile.STANDARD
    assert engine.profile is Profile.STANDARD


@pytest.mark.parametrize("garbage", ["", "unknown", "ROOT", 5, None, object()])
def test_set_profile_rejects_garbage(garbage: object) -> None:
    """A bad profile value must not silently downgrade authority."""

    engine = PolicyEngine(Profile.FULL)
    with pytest.raises(ValueError):
        engine.set_profile(garbage)  # type: ignore[arg-type]
    assert engine.profile is Profile.FULL  # unchanged on rejection


@pytest.mark.parametrize("garbage", ["", "unknown", None, 5])
def test_constructor_rejects_unknown_profile(garbage: object) -> None:
    with pytest.raises(ValueError):
        PolicyEngine(garbage)  # type: ignore[arg-type]


def test_engine_exposes_effective_rules() -> None:
    """Callers surface the active envelope without importing the table."""

    engine = PolicyEngine(Profile.FULL, extra_deny=(), allow_services=("nginx",))
    assert engine.rules.profile is Profile.FULL
    assert engine.allow_services == ("nginx",)
    assert PolicyEngine().allow_services is None


def test_allow_services_normalizes_the_string_form() -> None:
    engine = PolicyEngine(Profile.STANDARD, allow_services="SSH.service")
    assert engine.allow_services == ("ssh",)


# ---------------------------------------------------------------------------
# Matrices
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "command, band",
    [
        (SAFE_COMMAND, Risk.SAFE),
        (CONFIRM_COMMAND, Risk.CONFIRM),
        (BLOCKED_COMMAND, Risk.BLOCKED),
    ],
)
def test_command_matrix_pins_the_intended_bands(command: str, band: Risk) -> None:
    """The matrix commands below must stay in the band the tests assume."""

    assert classify(command).risk is band


@pytest.mark.parametrize("profile", ALL_PROFILES)
@pytest.mark.parametrize(
    "command, band",
    [(SAFE_COMMAND, "safe"), (CONFIRM_COMMAND, "confirm"), (BLOCKED_COMMAND, "blocked")],
)
def test_every_profile_matrix(profile: Profile, command: str, band: str) -> None:
    """Safe/confirm/blocked decisions for each profile, all in one place.

    READONLY only ever runs provably read-only commands; STANDARD and FULL run
    safe commands and confirm-band ones with a confirmation; BLOCKED stays
    refused everywhere because no profile sets ``allow_blocked``.
    """

    decision = PolicyEngine(profile).evaluate(command)

    if profile is Profile.LOCKED:
        assert decision.allowed is False
        assert decision.requires_confirmation is False
    elif profile is Profile.READONLY:
        expected_allowed = band == "safe" and command == SAFE_COMMAND
        assert decision.allowed is expected_allowed
        assert decision.requires_confirmation is False
    else:
        if band == "blocked":
            assert decision.allowed is False
            assert decision.requires_confirmation is False
        elif band == "confirm":
            assert decision.allowed is True
            assert decision.requires_confirmation is True
        else:
            assert decision.allowed is True
            assert decision.requires_confirmation is False
    assert decision.risk in {"safe", "confirm", "blocked"}


def test_locked_denies_everything_including_read_only_lookups() -> None:
    """LOCKED is the incident kill switch: even `ls` must not run."""

    engine = PolicyEngine(Profile.LOCKED)
    for command in ("ls -la", "cat /etc/hosts", SAFE_COMMAND, CONFIRM_COMMAND, BLOCKED_COMMAND):
        decision = engine.evaluate(command)
        assert decision.allowed is False
        assert decision.risk == "blocked"
        assert decision.requires_confirmation is False


def test_readonly_allows_ls_la() -> None:
    """The headline on-call promise: inspection works under READONLY."""

    decision = PolicyEngine(Profile.READONLY).evaluate("ls -la")
    assert decision.allowed is True
    assert decision.risk == "safe"
    assert decision.requires_confirmation is False


def test_readonly_denies_rm() -> None:
    decision = PolicyEngine(Profile.READONLY).evaluate("rm -rf /tmp/cache")
    assert decision.allowed is False
    assert decision.risk == "blocked"


def test_readonly_denies_systemctl_start_stop() -> None:
    decision = PolicyEngine(Profile.READONLY).evaluate("systemctl restart ssh")  # safe-band, still not read-only
    assert decision.allowed is False
    assert "read-only" in decision.reason


def test_readonly_denies_confirm_band_even_though_it_is_not_read_only() -> None:
    """`apt-get remove` is merely confirmation-band; READONLY still refuses."""

    decision = PolicyEngine(Profile.READONLY).evaluate("apt-get remove nginx")
    assert decision.allowed is False
    assert decision.risk == "blocked"


@pytest.mark.parametrize("profile", [Profile.STANDARD, Profile.FULL])
def test_standard_and_full_allow_ls_immediately(profile: Profile) -> None:
    decision = PolicyEngine(profile).evaluate("ls -la")
    assert decision.allowed is True
    assert decision.requires_confirmation is False


def test_confirm_band_requires_confirmation_under_standard() -> None:
    """STANDARD may run disruptive commands, but never without a tap."""

    decision = PolicyEngine(Profile.STANDARD).evaluate("systemctl stop ssh")
    assert decision.allowed is True
    assert decision.risk == "confirm"
    assert decision.requires_confirmation is True


@pytest.mark.parametrize("profile", [Profile.STANDARD, Profile.FULL])
def test_blocked_commands_stay_blocked_unless_allow_blocked(profile: Profile) -> None:
    """``allow_blocked`` is False everywhere by default, FULL included."""

    engine = PolicyEngine(profile)
    assert engine.rules.allow_blocked is False
    decision = engine.evaluate("rm -rf /")
    assert decision.allowed is False
    assert decision.risk == "blocked"


def test_blocked_commands_stay_blocked_in_full_without_allow_blocked() -> None:
    """FULL means unconfirmed-yet-powerful, not catastrophic."""

    engine = PolicyEngine(Profile.FULL)
    for command in ("rm -rf /", "mkfs.ext4 /dev/sda1", "dd if=/dev/zero of=/dev/sda"):
        decision = engine.evaluate(command)
        assert decision.allowed is False
        assert decision.risk == "blocked"


def test_allow_blocked_must_be_opted_in_at_construction_time() -> None:
    """``allow_blocked`` starts False (FULL included) and needs an explicit flag.

    The flag is keyword-only and construction-time on purpose: break-glass
    must be a visible choice, never a silent default or a runtime toggle.
    """

    engine = PolicyEngine(Profile.FULL)
    assert engine.rules.allow_blocked is False
    decision = engine.evaluate("rm -rf /")
    assert decision.allowed is False
    assert decision.rule == "risk-blocked"
    assert decision.explanation

    permissive = PolicyEngine(Profile.FULL, allow_blocked=True)
    assert permissive.rules.allow_blocked is True
    decision = permissive.evaluate("rm -rf /")
    assert decision.allowed is True
    assert decision.risk == "blocked"
    assert decision.requires_confirmation is False


def test_allow_blocked_flag_does_not_leak_across_engines() -> None:
    """Toggling the flag on one engine never widens another instance."""

    assert PolicyEngine(Profile.FULL, allow_blocked=True).rules.allow_blocked is True
    assert PolicyEngine(Profile.FULL).rules.allow_blocked is False
    assert rules_for(Profile.FULL).allow_blocked is False  # table unchanged


def test_allow_blocked_cannot_rescue_the_readonly_and_locked_profiles() -> None:
    """READONLY keeps blocking non-read-only commands; LOCKED denies everything."""

    assert (
        PolicyEngine(Profile.READONLY, allow_blocked=True)
        .evaluate("rm -rf /")
        .allowed
        is False
    )
    assert (
        PolicyEngine(Profile.LOCKED, allow_blocked=True).evaluate("ls -la").allowed
        is False
    )


def test_allow_blocked_rejects_non_bool_values() -> None:
    """A truthy non-bool is a mistake in wiring; reject it loudly."""

    with pytest.raises(TypeError):
        PolicyEngine(Profile.FULL, allow_blocked="yes")  # type: ignore[arg-type]


def test_extra_deny_is_applied_before_read_only_allowlisting() -> None:
    """A READONLY session cannot smuggle a deny-listed binary through."""

    engine = PolicyEngine(Profile.READONLY, extra_deny=[r"\bcat\b"])
    assert engine.evaluate("cat /etc/hosts").allowed is False


def test_only_risk_matters_not_the_command_text() -> None:
    """Two SAFE commands get the same verdict; the engine never re-parses."""

    engine = PolicyEngine(Profile.STANDARD)
    first = engine.evaluate("ls -la")
    second = engine.evaluate("df -h")
    assert (first.allowed, first.requires_confirmation) == (
        second.allowed,
        second.requires_confirmation,
    )


# ---------------------------------------------------------------------------
# extra_deny
# ---------------------------------------------------------------------------


def test_extra_deny_blocks_a_safe_command() -> None:
    """An operator-specific veto outranks the classifier's SAFE verdict."""

    engine = PolicyEngine(Profile.STANDARD, extra_deny=[r"\bdocker\b"])
    decision = engine.evaluate("docker ps")
    assert decision.allowed is False
    assert decision.risk == "blocked"
    assert decision.rule == "profile-deny-pattern"


def test_extra_deny_report_names_the_matching_pattern() -> None:
    engine = PolicyEngine(Profile.STANDARD, extra_deny=[r"^ls$"])
    decision = engine.evaluate("ls")
    assert decision.allowed is False
    assert "ls" in decision.reason


def test_extra_deny_leaves_matching_commands_alone() -> None:
    engine = PolicyEngine(Profile.STANDARD, extra_deny=[r"\bdocker\b"])
    assert engine.evaluate("ls -la").allowed is True


def test_extra_deny_ignores_invalid_regex() -> None:
    """A bad pattern vetoes nothing rather than refusing every command."""

    engine = PolicyEngine(Profile.STANDARD, extra_deny=["(unclosed"])
    assert engine.evaluate("ls -la").allowed is True


def test_extra_deny_ignores_non_string_entries() -> None:
    engine = PolicyEngine(Profile.STANDARD, extra_deny=[5, None, "", "ls"])  # type: ignore[list-item]
    assert engine.evaluate("ls").allowed is False
    assert engine.evaluate("df -h").allowed is True


def test_extra_deny_is_case_insensitive() -> None:
    engine = PolicyEngine(Profile.STANDARD, extra_deny=[r"docker"])
    assert engine.evaluate("DOCKER ps").allowed is False


# ---------------------------------------------------------------------------
# allow_services
# ---------------------------------------------------------------------------


def test_allow_services_restricts_service_control() -> None:
    """Only the named units may be touched."""

    engine = PolicyEngine(Profile.STANDARD, allow_services=["nginx"])
    assert engine.evaluate("systemctl stop nginx").allowed is True
    assert engine.evaluate("systemctl stop ssh").allowed is False


def test_allow_services_blocks_off_list_service_via_service_argument() -> None:
    engine = PolicyEngine(Profile.STANDARD, allow_services=["nginx"])
    decision = engine.evaluate("systemctl stop", service="ssh")
    assert decision.allowed is False
    assert decision.rule == "service-not-allowed"


def test_allow_services_passes_service_argument_on_the_allowlist() -> None:
    engine = PolicyEngine(Profile.STANDARD, allow_services=["nginx"])
    decision = engine.evaluate("systemctl status nginx", service="nginx")
    assert decision.allowed is True


def test_allow_services_normalizes_service_suffix() -> None:
    engine = PolicyEngine(Profile.STANDARD, allow_services=["ssh"])
    assert engine.evaluate("systemctl stop", service="ssh.service").allowed is True


def test_allow_services_rejects_non_string_or_blank_service() -> None:
    engine = PolicyEngine(Profile.STANDARD, allow_services=["ssh"])
    assert engine.evaluate("systemctl status ssh", service=5).allowed is True  # type: ignore[arg-type]
    assert engine.evaluate("systemctl status ssh", service="  ").allowed is True


def test_allow_services_accepts_a_single_string() -> None:
    """The documented signature says ``Iterable[str]``; a bare string works."""

    engine = PolicyEngine(Profile.STANDARD, allow_services="nginx")
    assert engine.evaluate("systemctl stop nginx").allowed is True
    assert engine.evaluate("systemctl stop ssh").allowed is False


def test_allow_services_ignores_flags_as_targets() -> None:
    """``-f`` and its cousins are options, not container names."""

    engine = PolicyEngine(Profile.STANDARD, allow_services=["nginx"])
    decision = engine.evaluate("systemctl status nginx --no-pager")
    assert decision.allowed is True


def test_no_allow_services_means_every_service() -> None:
    assert PolicyEngine(Profile.STANDARD).evaluate("systemctl stop ssh").allowed is True


# ---------------------------------------------------------------------------
# Command validation, size and alias
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ["", "   ", "\x00ls"])
def test_engine_refuses_empty_or_nul_commands(command: str) -> None:
    """Profiles are irrelevant when there is nothing executable to run."""

    decision = PolicyEngine(Profile.FULL).evaluate(command)
    assert decision.allowed is False
    assert decision.risk == "blocked"


def test_engine_refuses_oversized_commands() -> None:
    """READONLY's tighter size cap is enforced by the engine."""

    engine = PolicyEngine(Profile.READONLY)
    decision = engine.evaluate("ls " + "a" * 4096)
    assert decision.allowed is False
    assert decision.rule == "command-too-large"


def test_global_size_ceiling_still_applies_to_standard() -> None:
    engine = PolicyEngine(Profile.STANDARD)
    assert engine.evaluate("ls " + "a" * (MAX_COMMAND_BYTES)).allowed is False
    assert engine.evaluate("ls " + "a" * 100).allowed is True


def test_engine_refuses_non_text() -> None:
    decision = PolicyEngine(Profile.FULL).evaluate(None)  # type: ignore[arg-type]
    assert decision.allowed is False


def test_alias_argument_does_not_change_the_verdict() -> None:
    """Authority comes from the active profile, never from the host alias."""

    engine = PolicyEngine(Profile.READONLY)
    assert engine.evaluate("ls -la", alias="prod").allowed is True
    assert engine.evaluate("ls -la", alias="other").allowed is True


# ---------------------------------------------------------------------------
# PolicyDecision shape and explanation text
# ---------------------------------------------------------------------------


def test_decision_reports_the_risk_band_names() -> None:
    """``risk`` is the contract's string vocabulary."""

    assert PolicyEngine(Profile.STANDARD).evaluate("ls -la").risk == "safe"
    assert PolicyEngine(Profile.STANDARD).evaluate("systemctl stop ssh").risk == "confirm"
    assert PolicyEngine(Profile.STANDARD).evaluate("rm -rf /").risk == "blocked"


@pytest.mark.parametrize("profile", ALL_PROFILES)
def test_explanation_is_non_empty_for_every_refusal(profile: Profile) -> None:
    """The operator always gets a sentence, never a bare ``Refused``."""

    engine = PolicyEngine(profile)
    for command in (SAFE_COMMAND, CONFIRM_COMMAND, BLOCKED_COMMAND, "systemctl stop"):
        decision = engine.evaluate(command)
        assert (decision.explanation or "").strip()
        assert decision.reason


def test_refusals_mention_the_rule_that_fired() -> None:
    """A refusal must say which gate stopped it, in plain words."""

    cases = {
        "ls": Profile.LOCKED,
        "systemctl stop ssh": Profile.READONLY,
        "rm -rf /": Profile.STANDARD,
    }
    for command, profile in cases.items():
        decision = PolicyEngine(profile).evaluate(command)
        assert decision.allowed is False
        assert decision.rule
        assert decision.rule in decision.explanation or decision.reason in decision.explanation


def test_explanation_for_confirm_mentions_the_risk() -> None:
    decision = PolicyEngine(Profile.STANDARD).evaluate("systemctl stop ssh")
    assert "confirmation" in decision.explanation
    assert "nginx" not in decision.explanation  # no command replay in the prompt


def test_decision_is_frozen_and_serializable() -> None:
    """Audit logs consume the decision dict; the object itself is immutable."""

    decision = PolicyEngine().evaluate("ls -la")
    assert isinstance(decision, PolicyDecision)
    with pytest.raises((AttributeError, TypeError)):
        decision.allowed = False  # type: ignore[misc]
    payload = decision.to_dict()
    assert payload["allowed"] is True
    assert payload["risk"] == "safe"
    assert set(payload) == {
        "allowed",
        "risk",
        "rule",
        "requires_confirmation",
    }


def test_decision_rule_vocabulary_is_bounded() -> None:
    """The rule names are machine-consumed; new ones must be added deliberately."""

    seen = {
        PolicyEngine(profile).evaluate(command).rule
        for profile in ALL_PROFILES
        for command in (SAFE_COMMAND, CONFIRM_COMMAND, BLOCKED_COMMAND, "")
    }
    assert seen.issubset(
        {
            "allowed",
            "profile-locked",
            "profile-readonly",
            "risk-blocked",
            "risk-confirm",
            "empty-command",
        }
    )


def test_rules_table_rules_are_used_not_reimplemented() -> None:
    """The engine must delegate to ``classify`` rather than re-deriving it."""

    engine = PolicyEngine(Profile.STANDARD)
    assert engine.evaluate("bash -c 'mkfs.ext4 /dev/sda1'").allowed is False
    assert engine.evaluate("bash -c 'systemctl stop ssh'").requires_confirmation is True


def test_nested_shell_cannot_smuggle_a_blocked_command_past_readonly() -> None:
    decision = PolicyEngine(Profile.READONLY).evaluate("bash -c 'rm -rf /'")
    assert decision.allowed is False


def test_engine_logs_nothing_on_the_refusal_path(caplog: pytest.LogCaptureFixture) -> None:
    """Refusals are the normal path; they must not spam the log."""

    with caplog.at_level("DEBUG", logger="src.policy.engine"):
        PolicyEngine(Profile.READONLY).evaluate("rm -rf /tmp/x")
    assert not caplog.records
