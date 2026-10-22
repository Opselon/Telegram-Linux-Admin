"""PURPOSE: Forward-compatible value objects for the wave 3.4 alerts lane.

OWNER: lane `alerts` of wave 3.4 (branch feat/3.4-alerts).
CONSUMES: src.localization.translate (kind labels only — contract law L4
  forbids hard-coded user-visible English outside localization.py).
PROVIDES: ``ALERT_KINDS``, ``AlertRule``, ``AlertEvent`` and
  ``alert_kind_label`` for checker/storage/notifier/ui.
INVARIANTS:
  * Both types are frozen: an evaluator can never mutate the rule it is
    judging, so one bad reading cannot corrupt a sweep.
  * Validation happens in ``__post_init__``: an unknown kind or a threshold
    outside 0..100 raises ``ValueError`` at construction time, so an invalid
    rule can never reach a probe or the database.
  * ``payload`` is accepted for forward compatibility and IGNORED for
    storage: the ``kind`` alone selects which probe reads the value.
  * ``AlertRule.id`` is ``None`` until :mod:`src.alerts.storage` persists it.
EXTEND: a new probe kind goes into ``ALERT_KINDS``, a label key into
  src/localization.py and a probe into the orchestrator's wiring — nothing
  in this module knows how a reading is measured.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from ..localization import translate

__all__ = [
    "ALERT_KINDS",
    "MIN_THRESHOLD",
    "MAX_THRESHOLD",
    "AlertEvent",
    "AlertRule",
    "alert_kind_label",
]

#: Kinds a rule may watch.  ``kind`` — never ``payload`` — picks the probe.
ALERT_KINDS: Final[tuple[str, ...]] = ("mem_high", "disk_high", "cpu_high")

MIN_THRESHOLD: Final = 0
MAX_THRESHOLD: Final = 100

#: kind -> localization key for the human label rendered in menus/messages.
_KIND_LABEL_KEYS: Final[dict[str, str]] = {
    "mem_high": "alerts_kind_mem",
    "disk_high": "alerts_kind_disk",
    "cpu_high": "alerts_kind_cpu",
}


def _validated_kind(kind: object) -> str:
    """Returns ``kind`` when it is a known probe, else raises ValueError."""
    if not isinstance(kind, str) or kind not in ALERT_KINDS:
        raise ValueError(f"unknown alert kind {kind!r}; expected one of {ALERT_KINDS}")
    return kind


def _validated_threshold(threshold: object) -> int:
    """Returns a whole percentage in 0..100, else raises ValueError.

    ``bool`` is rejected explicitly: ``True`` is an ``int`` in Python and a
    rule with threshold ``True`` would fire at 1 %, which is never intended.
    """
    if isinstance(threshold, bool) or not isinstance(threshold, int):
        raise ValueError(f"threshold must be an int percentage, got {threshold!r}")
    if not MIN_THRESHOLD <= threshold <= MAX_THRESHOLD:
        raise ValueError(
            f"threshold {threshold} out of range {MIN_THRESHOLD}-{MAX_THRESHOLD}"
        )
    return threshold


def _validated_owner(owner_id: object) -> int:
    """Returns the owning Telegram user id, else raises ValueError."""
    if isinstance(owner_id, bool) or not isinstance(owner_id, int):
        raise ValueError(f"owner_id must be a Telegram user id, got {owner_id!r}")
    return owner_id


def _validated_alias(alias: object) -> str:
    """Returns a non-empty server alias (surrounding space stripped)."""
    if not isinstance(alias, str) or not alias.strip():
        raise ValueError("alias must be a non-empty server alias")
    return alias.strip()


def alert_kind_label(kind: str, language: str | None = None) -> str:
    """Localized human label for a probe kind (``mem_high`` -> ``memory``)."""
    key = _KIND_LABEL_KEYS.get(kind)
    if key is None:
        return kind
    return translate(key, language)


@dataclass(frozen=True, kw_only=True)
class AlertRule:
    """One proactive alert configured by an operator.

    ``payload`` is opaque and unused by this wave (see the module docstring);
    it exists so a future kind can carry a sub-selector without a schema or
    dataclass migration.
    """

    owner_id: int
    alias: str
    kind: str
    threshold: int
    payload: str | None = None
    enabled: bool = True
    id: int | None = None

    def __post_init__(self) -> None:
        _validated_owner(self.owner_id)
        # Normalize once, here, so " web-1 " and "web-1" cannot become two
        # rules (and UNIQUE(owner_id, alias, kind) cannot be bypassed by
        # padding).  Frozen dataclasses need object.__setattr__.
        object.__setattr__(self, "alias", _validated_alias(self.alias))
        _validated_kind(self.kind)
        _validated_threshold(self.threshold)
        if not isinstance(self.enabled, bool):
            raise ValueError(f"enabled must be a bool, got {self.enabled!r}")
        if self.payload is not None and not isinstance(self.payload, str):
            raise ValueError("payload must be a string or None")
        if self.id is not None and (
            isinstance(self.id, bool) or not isinstance(self.id, int)
        ):
            raise ValueError("id must be an int or None")


@dataclass(frozen=True, kw_only=True)
class AlertEvent:
    """A rule that fired: the rule's snapshot plus the reading that tripped it.

    The snapshot (``alias``/``kind``/``threshold``) is copied, not referenced,
    so an event stays truthful after the rule is edited or deleted.  It is
    also what storage persists to the audit trail, which is why every field
    is a plain JSON-representable scalar.
    """

    owner_id: int
    alias: str
    kind: str
    threshold: int
    value: float
    fired_at: float
    message: str = ""
    rule_id: int | None = None

    def __post_init__(self) -> None:
        _validated_owner(self.owner_id)
        _validated_alias(self.alias)
        _validated_kind(self.kind)
        _validated_threshold(self.threshold)
        if isinstance(self.value, bool) or not isinstance(self.value, (int, float)):
            raise ValueError(f"value must be a number, got {self.value!r}")
        if isinstance(self.fired_at, bool) or not isinstance(self.fired_at, (int, float)):
            raise ValueError(f"fired_at must be an epoch timestamp, got {self.fired_at!r}")
        if not isinstance(self.message, str):
            raise ValueError("message must be a string")
        if self.rule_id is not None and (
            isinstance(self.rule_id, bool) or not isinstance(self.rule_id, int)
        ):
            raise ValueError("rule_id must be an int or None")
