"""Bounded per-operator authority over free-form SSH commands.

PURPOSE
    Compose ``src.command_policy`` risk bands (what the *command* can do) with
    policy profiles (what the *operator* may do) into one decision the bot can
    act on: allow now, allow after an explicit confirmation, or refuse.

OWNER
    lane policy (src/policy/**).

CONSUMES
    ``src.command_policy.classify`` (the only place command regexes live),
    ``src.command_policy.validate_command_text`` and
    ``src.policy.profiles``.

PROVIDES
    ``PolicyDecision`` and ``PolicyEngine``.

INVARIANTS
    * Risk classification is never re-derived: ``classify`` decides the band,
      the engine only decides whether the profile may act on that band.
    * Only risk matters, never the command text: two commands in the same band
      produce the same verdict for the same profile.
    * Deny wins: LOCKED, an empty/oversized command, a ``deny_patterns`` hit,
      an off-allowlist service or a band the profile cannot reach all refuse.
    * ``PolicyDecision`` is frozen and JSON-serializable through ``to_dict``.

EXTEND
    New refusal causes go in :meth:`PolicyEngine._refuse` (and ``_deny_hit``)
    so explanation wording stays in one place.  Never add a second classifier.
"""

from __future__ import annotations

import logging
import re
import shlex
from dataclasses import dataclass
from typing import Any, Iterable

from ..command_policy import (
    MAX_COMMAND_BYTES,
    Assessment,
    Risk,
    classify,
    validate_command_text,
)
from .profiles import (
    Profile,
    ProfileRules,
    normalized_services,
    read_only_violation,
    rules_for,
)

logger = logging.getLogger(__name__)

_RULE_LOCKED = "profile-locked"
_RULE_EMPTY = "empty-command"
_RULE_OVERSIZE = "command-too-large"
_RULE_DENY_PATTERN = "profile-deny-pattern"
_RULE_SERVICE = "service-not-allowed"
_RULE_READONLY = "profile-readonly"
_RULE_RISK_BLOCKED = "risk-blocked"
_RULE_RISK_CONFIRM = "risk-confirm"
_RULE_ALLOWED = "allowed"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """The verdict for one command under the engine's current profile."""

    allowed: bool
    risk: str              # "safe" | "confirm" | "blocked"
    rule: str
    reason: str
    requires_confirmation: bool
    explanation: str       # user-facing, one short paragraph

    def to_dict(self) -> dict[str, Any]:
        """Serializes the decision for logs/audit without leaking the command."""

        return {
            "allowed": self.allowed,
            "risk": self.risk,
            "rule": self.rule,
            "requires_confirmation": self.requires_confirmation,
        }


def _tokenize(command: str) -> list[str]:
    """Splits a command the way a shell would, tolerating quoting."""

    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()


def _normalize_target(name: str) -> str:
    """A service name with a ``.service`` suffix compares against the allowlist."""

    return name.strip().lower().removesuffix(".service")


def _replace_allow_blocked(rules: ProfileRules, allow_blocked: bool) -> ProfileRules:
    """Rebuilds *rules* with the catastrophic band toggled; no other field moves."""

    return ProfileRules(
        profile=rules.profile,
        allow_confirm=rules.allow_confirm,
        allow_blocked=allow_blocked,
        max_command_bytes=rules.max_command_bytes,
        deny_patterns=rules.deny_patterns,
        allow_services=rules.allow_services,
        read_only=rules.read_only,
    )


class PolicyEngine:
    """Evaluates commands against the authority of the active profile."""

    def __init__(
        self,
        profile: Profile = Profile.STANDARD,
        *,
        extra_deny: Iterable[str] = (),
        allow_services: Iterable[str] | None = None,
        allow_blocked: bool | None = None,
    ) -> None:
        self._rules = rules_for(profile)  # raises for an unknown profile
        if allow_blocked is not None:
            # Additive, keyword-only extension: the contract's positional
            # signature is unchanged, and no profile turns this on by default.
            # Break-glass needs an explicit, auditable opt-in at construction.
            if not isinstance(allow_blocked, bool):
                raise TypeError("allow_blocked must be a bool when provided")
            self._rules = _replace_allow_blocked(self._rules, allow_blocked)
        self._raw_deny_patterns, self._extra_deny = self._compile_deny(extra_deny)
        # ``allow_services`` is per-engine (session-scoped), while rules_for
        # carries the profile-scoped value.  The engine-level allowlist wins:
        # an operator-configured allowlist must not be silently widened by a
        # profile default, and a profile allowlist plus a narrower engine
        # allowlist would defy the point of an enablelist.
        self._allow_services = self._effective_allow_services(allow_services)

    # ------------------------------------------------------------------ #
    # Configuration
    # ------------------------------------------------------------------ #
    @staticmethod
    def _compile_deny(
        patterns: Iterable[str],
    ) -> tuple[tuple[str, ...], tuple[re.Pattern[str], ...]]:
        """Pairs each usable pattern with its compiled form (indices must match)."""

        raw: list[str] = []
        compiled: list[re.Pattern[str]] = []
        for pattern in patterns:
            if not isinstance(pattern, str) or not pattern.strip():
                continue
            try:
                compiled.append(re.compile(pattern, re.IGNORECASE))
            except re.error as exc:
                logger.warning("Ignoring invalid deny pattern %r: %s", pattern, exc)
                continue
            raw.append(pattern)
        return tuple(raw), tuple(compiled)

    @property
    def profile(self) -> Profile:
        """The profile whose authority this engine currently enforces."""

        return self._rules.profile

    @property
    def rules(self) -> ProfileRules:
        """The rules currently in force (profile defaults plus session config)."""

        return self._rules

    @property
    def allow_services(self) -> tuple[str, ...] | None:
        """The service allowlist in force, or ``None`` when every service is allowed."""

        return self._allow_services

    def _effective_allow_services(
        self, allow_services: Iterable[str] | None
    ) -> tuple[str, ...] | None:
        session = normalized_services(allow_services)
        if session is not None:
            return session
        return self._rules.allow_services

    def set_profile(self, profile: Profile | str) -> Profile:
        """Switches authority; rejects anything that is not a profile."""

        self._rules = rules_for(profile)
        return self._rules.profile

    # ------------------------------------------------------------------ #
    # Evaluation
    # ------------------------------------------------------------------ #
    def evaluate(
        self,
        command: str,
        *,
        alias: str = "",
        service: str | None = None,
    ) -> PolicyDecision:
        """Classifies *command* and decides whether the profile may run it.

        ``alias`` is accepted mainly because the caller already has it; it is
        not used for the verdict, which depends only on the active profile.
        ``service`` additionally constrains ``systemctl``/``docker`` commands
        to an allowlist of unit/container names.
        """

        try:
            validate_command_text(command)  # the shared text/size contract
        except (TypeError, ValueError):
            invalid = self._describe_invalid(command)
            if invalid is not None:
                return self._refuse(*invalid)
            return self._refuse(_RULE_EMPTY, "the command is empty or not text")

        if self._rules.profile is Profile.LOCKED:
            return self._refuse(_RULE_LOCKED, "the session is locked")
        deny_rule = self._deny_hit(command)
        if deny_rule is not None:
            return self._refuse(
                _RULE_DENY_PATTERN, f"it matches the deny pattern '{deny_rule}'"
            )
        if self._service_violation(command, service):
            return self._refuse(
                _RULE_SERVICE,
                f"the service '{service}' is not on this session's allowlist"
                if service
                else "the command targets a service that is not allowlisted",
            )
        if self._too_large(command):
            return self._refuse(
                _RULE_OVERSIZE,
                f"the command is larger than {self._rules.max_command_bytes} bytes",
            )

        assessment = classify(command)  # the single source of risk
        if self._rules.read_only:
            return self._evaluate_read_only(command, assessment)
        return self._evaluate_assessment(assessment)

    # ------------------------------------------------------------------ #
    # Refusal helpers
    # ------------------------------------------------------------------ #
    def _refuse(self, rule: str, reason: str) -> PolicyDecision:
        return PolicyDecision(
            allowed=False,
            risk="blocked",
            rule=rule,
            reason=reason,
            requires_confirmation=False,
            explanation=f"Refused under profile '{self._rules.profile.value}': {reason}.",
        )

    def _describe_invalid(self, command: object) -> tuple[str, str] | None:
        """Names the exact validation failure for an unusable command."""

        if not isinstance(command, str):
            return (_RULE_EMPTY, "the command is empty or not text")
        if not command.strip():
            return (_RULE_EMPTY, "the command is empty or not text")
        if "\x00" in command:
            return (_RULE_EMPTY, "the command contains a NUL byte")
        if len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
            return (
                _RULE_OVERSIZE,
                f"the command is larger than {MAX_COMMAND_BYTES} bytes",
            )
        return None

    def _deny_hit(self, command: str) -> str | None:
        for index, pattern in enumerate(self._extra_deny):
            if pattern.search(command):
                return self._raw_deny_patterns[index]
        return None

    def _too_large(self, command: str) -> bool:
        return len(command.encode("utf-8")) > self._rules.max_command_bytes

    def _service_violation(self, command: str, service: str | None) -> bool:
        if self._allow_services is None:
            return False
        targets = self._service_targets(command, service)
        if not targets:
            return False
        return not targets.issubset(set(self._allow_services))

    @staticmethod
    def _service_targets(command: str, service: str | None) -> set[str]:
        """The unit/container names a command touches, normalized.

        Only explicit arguments count: options such as ``--no-pager`` are not
        service names, so they must not be checked against the allowlist (a
        ``--no-pager`` status call on an allowlisted unit stays allowed).
        """

        targets: set[str] = set()
        if service is not None and isinstance(service, str) and service.strip():
            targets.add(_normalize_target(service))
        tokens = _tokenize(command)
        if not tokens:
            return {name for name in targets if name}
        binary = tokens[0].rsplit("/", 1)[-1].lower()
        rest = [token for token in tokens[1:] if not token.startswith("-")]
        if binary in ("systemctl", "docker") and len(rest) >= 2:
            # Skip the subcommand verb (`status`, `restart`, `rm`, ...) and take
            # the unit/container name.  Anything the allowlist does not name is
            # refused, so a misparsed token fails closed.
            targets.add(_normalize_target(rest[1]))
        return {name for name in targets if name}

    # ------------------------------------------------------------------ #
    # Profile bands
    # ------------------------------------------------------------------ #
    def _evaluate_read_only(
        self, command: str, assessment: Assessment
    ) -> PolicyDecision:
        """READONLY: only provably observational commands pass, and never CONFIRM."""

        if assessment.risk is not Risk.SAFE:
            return self._refuse(
                _RULE_RISK_CONFIRM if assessment.risk is Risk.CONFIRM else _RULE_RISK_BLOCKED,
                f"command_policy ranked it as {assessment.risk.value}",
            )
        violation = read_only_violation(command)
        if violation is not None:
            return self._refuse(_RULE_READONLY, violation)
        return self._allow("safe", _RULE_ALLOWED, "it is a read-only command")

    def _evaluate_assessment(self, assessment: Assessment) -> PolicyDecision:
        """STANDARD/FULL: SAFE runs, CONFIRM needs a tap, BLOCKED needs allow_blocked."""

        if assessment.risk is Risk.BLOCKED:
            if self._rules.allow_blocked:
                return self._allow(
                    "blocked", _RULE_RISK_BLOCKED, assessment.reason or "a blocked-class command"
                )
            return self._refuse(
                _RULE_RISK_BLOCKED,
                assessment.reason or "the command is in the blocked band",
            )
        if assessment.risk is Risk.CONFIRM:
            if self._rules.allow_confirm:
                return PolicyDecision(
                    allowed=True,
                    risk="confirm",
                    rule=_RULE_RISK_CONFIRM,
                    reason=assessment.reason or "the command is disruptive",
                    requires_confirmation=True,
                    explanation=(
                        f"Profile '{self._rules.profile.value}' may run this, but it is "
                        f"{assessment.reason or 'disruptive'}, so an explicit confirmation "
                        "is required first."
                    ),
                )
            return self._refuse(
                _RULE_RISK_CONFIRM,
                assessment.reason or "the command is in the confirm band",
            )
        return self._allow("safe", _RULE_ALLOWED, assessment.reason or "it ranked as safe")

    def _allow(self, risk: str, rule: str, reason: str) -> PolicyDecision:
        return PolicyDecision(
            allowed=True,
            risk=risk,
            rule=rule,
            reason=reason,
            requires_confirmation=False,
            explanation=f"Allowed under profile '{self._rules.profile.value}': {reason}.",
        )
