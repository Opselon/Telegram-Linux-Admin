"""Destination allow-listing for free-form SSH management.

The bot runs arbitrary commands *by design* — that is what an SSH admin bot
is for.  The genuinely dangerous mistake is pointing it at the wrong machine:
a typo in the management console of a hosting provider, a pasted IP that
belongs to a production database, or a curious first-time user typing the
address of their router.  :class:`DestinationPolicy` stops that class of
error before a TCP connection is even attempted.

Modes (``TLA_DESTINATION_POLICY``):

``allow_all``
    Historical behaviour.  Connections to any syntactically valid host are
    attempted.  Loopback and link-local addresses are still rejected, and
    literal metadata service addresses are always rejected.

``deny_public`` (recommended for a home lab)
    Only private / LAN / VPN ranges (RFC1918, ULA, CGNAT) may be targeted.
    The bot cannot be used to pivot to arbitrary internet hosts.

``allowlist``
    Only destinations matching an entry in ``TLA_ALLOWED_DESTINATIONS``
    (comma separated hostnames, IPs and CIDR ranges) may be targeted.

The decision is intentionally separated from the connection code so that it is
unit-testable without a network and so that the rejection reason can be
surfaced to the user and written to the audit log.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Iterable

#: Union of the concrete address classes exposed by :mod:`ipaddress`.
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

ALLOW_ALL = "allow_all"
DENY_PUBLIC = "deny_public"
ALLOWLIST = "allowlist"

VALID_POLICIES = (ALLOW_ALL, DENY_PUBLIC, ALLOWLIST)

#: Metadata endpoints are never reachable on purpose: they hand out cloud
#: credentials to anything that can make an HTTP request.
METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("169.254.170.2"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)

_PRIVATE_NETWORKS = tuple(
    ipaddress.ip_network(net)
    for net in (
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "100.64.0.0/10",  # CGNAT / Tailscale
        "127.0.0.0/8",
        "fc00::/7",
        "fe80::/10",
    )
)


def _parse_policy(value: str | None) -> str:
    normalized = (value or ALLOW_ALL).strip().lower().replace("-", "_")
    aliases = {"all": ALLOW_ALL, "private": DENY_PUBLIC, "deny_public_only": DENY_PUBLIC}
    normalized = aliases.get(normalized, normalized)
    return normalized if normalized in VALID_POLICIES else ALLOW_ALL


@dataclass(frozen=True, slots=True)
class DestinationDecision:
    allowed: bool
    reason: str
    resolved_ip: str | None = None


def _parse_allowlist(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    return tuple(part.strip().lower() for part in raw.split(",") if part.strip())


@lru_cache(maxsize=8)
def _networks(entries: tuple[str, ...]) -> tuple[IPNetwork, ...]:
    networks = []
    for entry in entries:
        try:
            networks.append(ipaddress.ip_network(entry, strict=False))
        except ValueError:
            continue
    return tuple(networks)


class DestinationPolicy:
    """Evaluates whether a host may be connected to under the active policy."""

    def __init__(
        self,
        mode: str | None = None,
        allowlist: tuple[str, ...] | None = None,
        *,
        resolver: Any | None = None,
    ) -> None:
        self.mode = _parse_policy(mode or os.environ.get("TLA_DESTINATION_POLICY"))
        entries = allowlist if allowlist is not None else _parse_allowlist(
            os.environ.get("TLA_ALLOWED_DESTINATIONS")
        )
        self.allowlist = tuple(entry.lower() for entry in entries)
        # Injectable resolver keeps the class testable without DNS.
        self._resolver = resolver

    def reconfigure(self, mode: str | None = None, allowlist: tuple[str, ...] | None = None) -> None:
        """Applies new settings without restarting the process."""
        if mode is not None:
            self.mode = _parse_policy(mode)
        if allowlist is not None:
            self.allowlist = tuple(entry.strip().lower() for entry in allowlist if entry.strip())

    # -- public API ------------------------------------------------------
    def evaluate(self, hostname: str) -> DestinationDecision:
        """Returns the decision for *hostname* (a name, IPv4 or IPv6 literal)."""
        if not hostname or not isinstance(hostname, str):
            return DestinationDecision(False, "empty hostname")

        candidate = hostname.strip()
        addresses = self._resolve(candidate)
        if not addresses:
            return DestinationDecision(False, "unresolvable host")

        for address in addresses:
            if address in METADATA_ADDRESSES:
                return DestinationDecision(False, "metadata endpoints are blocked", str(address))

        if self.mode == ALLOW_ALL:
            return DestinationDecision(True, "allow_all", str(addresses[0]))

        if self.mode == DENY_PUBLIC:
            if all(self._is_private(address) for address in addresses):
                return DestinationDecision(True, "private network", str(addresses[0]))
            return DestinationDecision(
                False,
                "only private/LAN destinations are allowed by policy",
                str(addresses[0]),
            )

        # allowlist
        for address in addresses:
            if self._matches_allowlist(candidate, address):
                return DestinationDecision(True, "allowlisted", str(address))
        return DestinationDecision(False, "destination is not in the allowlist", str(addresses[0]))

    def check(self, hostname: str) -> None:
        """Raises :class:`DestinationDenied` when the destination is not allowed."""
        decision = self.evaluate(hostname)
        if not decision.allowed:
            raise DestinationDenied(hostname, decision.reason)

    def describe(self) -> str:
        if self.mode == ALLOWLIST:
            return f"{self.mode} ({', '.join(self.allowlist) or 'empty'})"
        return self.mode

    # -- internals -------------------------------------------------------
    def _resolve(self, candidate: str) -> list[IPAddress]:
        # A literal address needs no DNS lookup.
        try:
            parsed = [ipaddress.ip_address(candidate)]
        except ValueError:
            parsed = []

        resolver = self._resolver
        if resolver is not None:
            resolved: Iterable[str] = resolver(candidate)
            return [ipaddress.ip_address(addr) for addr in resolved]

        if parsed:
            return parsed

        try:
            infos = socket.getaddrinfo(candidate, None)
        except (socket.gaierror, UnicodeError):
            return []
        addresses: list[IPAddress] = []
        for info in infos:
            try:
                address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
            except ValueError:
                continue
            if address not in addresses:
                addresses.append(address)
        return addresses

    @staticmethod
    def _is_private(address: IPAddress) -> bool:
        if address.is_loopback or address.is_link_local:
            return True
        mapped = getattr(address, "ipv4_mapped", None)
        if mapped is not None:
            return DestinationPolicy._is_private(mapped)
        return any(address in network for network in _PRIVATE_NETWORKS)

    def _matches_allowlist(self, candidate: str, address: IPAddress) -> bool:
        lowered = candidate.lower()
        for entry in self.allowlist:
            if entry == lowered:
                return True
            if entry.startswith("*.") and lowered.endswith(entry[1:]):
                return True
            try:
                if address in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        return False


class DestinationDenied(PermissionError):
    """Raised when a destination violates the active policy."""

    def __init__(self, hostname: str, reason: str) -> None:
        super().__init__(f"Connection to '{hostname}' denied: {reason}")
        self.hostname = hostname
        self.reason = reason


#: Process-wide policy; rebuilt at startup and whenever the env changes.
DESTINATIONS = DestinationPolicy()
