"""Domain errors raised by the target catalog and transports.

They carry i18n keys (not rendered sentences) so the presentation layer can
translate them; keeping rendered text out of the domain is what lets every
handler stay language agnostic.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Base class for domain failures with an i18n key and context."""

    i18n_key = "error_generic_connection"

    def __init__(self, message: str = "", **context: Any) -> None:
        super().__init__(message or self.i18n_key)
        self.context: dict[str, Any] = dict(context)


class TargetNotFound(DomainError):
    """No target with the given alias exists in the owner's catalog."""

    i18n_key = "server_not_found"

    def __init__(self, alias: str) -> None:
        super().__init__(alias, alias=alias)
        self.alias = alias
        self.kwargs = {"alias": alias}


class TargetAccessDenied(DomainError):
    """The user may not use the requested target at all (policy, not transport)."""

    i18n_key = "admin_only"

    def __init__(self, alias: str, reason: str) -> None:
        super().__init__(f"{alias}: {reason}", alias=alias, reason=reason)
        self.alias = alias


class TargetDisabled(DomainError):
    """The target kind is configured off for this deployment."""

    i18n_key = "self_host_disabled"

    def __init__(self, alias: str) -> None:
        super().__init__(alias, alias=alias)
        self.alias = alias


class TransportUnavailable(DomainError):
    """The transport layer could not reach or complete the operation."""

    i18n_key = "error_generic_connection"

    def __init__(self, alias: str, detail: str = "") -> None:
        super().__init__(detail or alias, alias=alias)
        self.alias = alias
        self.detail = detail
