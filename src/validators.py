"""Input validators for user-supplied connection details.

Every value that ends up in an SSH client — alias, hostname, username, key
path — is validated here before it is stored, so a malformed or hostile value
is rejected once at the edge instead of being interpreted later by the SSH
library or a shell.  Validators raise :class:`ValueError` with a message that
is safe to show to the user.
"""

from __future__ import annotations

import ipaddress
import re

#: Aliases are used inside callback payloads and log lines.
_ALIAS_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,31}$")
#: Linux usernames (``useradd`` allows a fairly narrow set).
_USERNAME_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
#: Shell metacharacters that must never appear in a path we hand to asyncssh.
_UNSAFE_PATH_CHARS = set("`$;&|<>*?\"'\n\r\t\x00")

#: Hostnames longer than this cannot resolve (RFC 1035).
MAX_HOSTNAME_LENGTH = 253


def validate_alias(value: str) -> str:
    """Validates a server alias: 1-32 chars, alphanumeric start."""
    if not isinstance(value, str):
        raise ValueError("Alias must be text.")
    alias = value.strip()
    if not alias:
        raise ValueError("Alias cannot be empty.")
    if not _ALIAS_RE.match(alias):
        raise ValueError(
            "Invalid alias. Use 1-32 characters: letters, digits, dot, dash or underscore,"
            " starting with a letter or digit."
        )
    if alias in {".", ".."}:
        raise ValueError("Invalid alias.")
    return alias


def validate_hostname(value: str) -> str:
    """Validates an IPv4/IPv6 literal or a DNS hostname."""
    if not isinstance(value, str):
        raise ValueError("Hostname must be text.")
    hostname = value.strip()
    if not hostname:
        raise ValueError("Hostname cannot be empty.")
    if len(hostname) > MAX_HOSTNAME_LENGTH:
        raise ValueError(f"Hostname is longer than {MAX_HOSTNAME_LENGTH} characters.")
    if any(ch.isspace() for ch in hostname):
        raise ValueError("Hostname must not contain whitespace.")
    if "/" in hostname or "@" in hostname:
        # ``user@host`` and URL-style values hide a second component; require
        # the user to fill the username field instead.
        raise ValueError("Hostname must not contain '/' or '@'.")

    try:
        ipaddress.ip_address(hostname)
        return hostname
    except ValueError:
        pass

    if hostname.endswith("."):
        hostname = hostname[:-1]
    labels = hostname.split(".")
    if any(not label for label in labels):
        raise ValueError("Hostname contains an empty label.")
    for label in labels:
        if len(label) > 63:
            raise ValueError("Hostname labels cannot exceed 63 characters.")
        if label.startswith("-") or label.endswith("-"):
            raise ValueError("Hostname labels cannot start or end with a dash.")
        if not re.fullmatch(r"[A-Za-z0-9-]+", label):
            raise ValueError("Hostname contains invalid characters.")
    return hostname


def validate_username(value: str) -> str:
    """Validates a POSIX username."""
    if not isinstance(value, str):
        raise ValueError("Username must be text.")
    username = value.strip()
    if not username:
        raise ValueError("Username cannot be empty.")
    if not _USERNAME_RE.match(username):
        raise ValueError(
            "Invalid username. Use lowercase letters, digits, '_' or '-' (max 32,"
            " starting with a letter or underscore)."
        )
    return username


def validate_key_path(value: str) -> str:
    """Validates a local private key path passed to asyncssh."""
    if not isinstance(value, str):
        raise ValueError("Key path must be text.")
    path = value.strip()
    if not path:
        raise ValueError("Key path cannot be empty.")
    if len(path) > 1024:
        raise ValueError("Key path is too long.")
    if any(ch in _UNSAFE_PATH_CHARS for ch in path):
        raise ValueError("Key path contains characters that are not allowed.")
    if not path.startswith("/"):
        raise ValueError("Key path must be absolute (start with '/').")
    return path


def validate_port(value: int | str) -> int:
    """Validates a TCP port number."""
    try:
        port = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Port must be an integer.") from exc
    if not 1 <= port <= 65535:
        raise ValueError("Port must be between 1 and 65535.")
    return port
