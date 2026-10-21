"""Bounded per-operator command authority for the Telegram admin bot.

PURPOSE
    Public surface of the policy package: profiles that bound what an operator
    may do, and an engine that combines those bounds with the risk bands from
    ``src.command_policy``.

OWNER
    lane policy (src/policy/**).

CONSUMES
    ``src.command_policy`` (risk bands, size ceiling) and the package modules
    ``profiles`` / ``engine``.

PROVIDES
    ``Profile``, ``ProfileRules``, ``PolicyDecision``, ``PolicyEngine`` plus the
    helpers ``rules_for``, ``parse_profile``, ``load_profiles``,
    ``profile_for``, ``is_read_only_command`` and ``read_only_violation``.

INVARIANTS
    * Importing this package has no side effects: no file reads, no config
      load, no global engine.
    * Everything re-exported here is stable; wiring code imports from
      ``src.policy`` rather than from the submodules.

EXTEND
    Add new public names to ``__all__`` here and keep the submodule import
    order acyclic (``engine`` depends on ``profiles``, never the reverse).
"""

from __future__ import annotations

from .engine import PolicyDecision, PolicyEngine
from .profiles import (
    COMMAND_PROFILES_KEY,
    Profile,
    ProfileRules,
    is_read_only_command,
    load_profiles,
    parse_profile,
    profile_for,
    read_only_violation,
    rules_for,
)

__all__ = [
    "COMMAND_PROFILES_KEY",
    "PolicyDecision",
    "PolicyEngine",
    "Profile",
    "ProfileRules",
    "is_read_only_command",
    "load_profiles",
    "parse_profile",
    "profile_for",
    "read_only_violation",
    "rules_for",
]
