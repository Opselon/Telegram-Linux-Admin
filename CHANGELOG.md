# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning.

## [3.1.0] — 2026-10-21

Hardening release: the bot now fails closed, pins host keys, refuses dangerous
commands by class, and can be monitored without a shell.

### Added

- **Access control** (`src/access_control.py`) — `open` / `whitelist` /
  `admin_only` modes enforced by one decorator on every handler, plus an
  `/access` command to change the mode at runtime (admin only).
- **Rate limiting** — token budgets per user: 6 connections / 30 s,
  12 commands / minute, 10 admin commands / minute.
- **SSH host-key pinning** — trust-on-first-use fingerprints stored in
  `servers.host_key`; mismatches abort before any input is sent.
- **Destination policy** (`src/destination_policy.py`) — `allow_all`,
  `deny_public` and `allowlist` modes; cloud metadata endpoints always refused.
- **Command risk policy** (`src/command_policy.py`) — SAFE / CONFIRM / BLOCKED
  classification with recursive inspection of nested shells, one-shot inline
  confirmations (3-minute TTL) and a hard block list.
- **Audit log** (`src/audit.py`) — JSONL, size-rotated, `/audit [count]` tail
  for administrators.
- **Health endpoint** (`src/health.py`) — `/healthz`, `/readyz`, `/metrics`,
  disabled unless `TLA_HEALTH_PORT` is set; Docker `HEALTHCHECK` included.
- **Metrics registry** (`src/metrics.py`) — dependency-free Prometheus
  exposition.
- **Input validation** (`src/validators.py`) — alias, username, hostname,
  key path and port validation on every add-server path.
- **Connection pooling** — SSH sessions are reused per `(user, host)` with a
  background idle reaper; transient failures are retried, auth and host-key
  failures are not.
- Configuration keys `access_mode`, `destination_policy` and
  `allowed_destinations`, with unknown keys preserved on save.
- 60+ new tests covering access control, command classification, destination
  policy, audit rotation, the health endpoint, parse-mode escaping and the
  admin command paths.

### Changed

- Command envelope is base64-encoded, so quotes, `$`, backticks, newlines and
  UTF-8 text no longer break the remote invocation.
- Authorization is fail-closed: an unknown or unparsable user ID is denied.
- `README.md` and `SECURITY.md` document the new layers, env vars and the
  Docker deployment that actually works.

### Fixed

- `Dockerfile` installed the wheel after `USER appuser`, so console scripts
  landed in `~/.local/bin` and `ENTRYPOINT ["tla-bot"]` failed; the package was
  also missing from the wheel entirely (`ModuleNotFoundError: No module named
  'src'`). Both are fixed and verified by running the image.
- `/dashboard` and `/update_bot` raised `NameError` (`parse_mode`, `user_id`).
- `admin_authorized` referenced an undefined `_safe_send_message`, so a denied
  admin command crashed instead of replying.
- `format_code_block` was used but never imported.
- `parse_mode.py` defined `escape_html`, `escape_text`, `format_bold` and
  `format_code` twice; the second (shadowing) copies silently dropped
  `escape_html(quote=...)` and mangled HTML link attributes.
- `src/health.py` was missing its `os` import.
- `src/updater.py` used an unimported `List`.
- `sqlite3` timestamp converter deprecation warning.
- Repository hygiene: `.vs/` and `updater.log` are no longer tracked.
