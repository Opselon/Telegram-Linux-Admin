# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning.

## [4.0.0] — 2026-10-21

**Self-managed host, layered architecture and a fully styled, fully localized
UI.** The administrator who installs the bot can now manage the machine the bot
runs on, side by side with remote SSH servers.

### Added

- **Self-host target** — the installer (first whitelisted user = administrator)
  gets a virtual `self-host` target: run commands, open a terminal, manage
  services, Docker, packages, files, firewall, processes and disks on the bot's
  own machine without configuring SSH to itself.
  - Fail-closed access: admin-only, plus `local_target_enabled` /
    `TLA_LOCAL_TARGET_ENABLED=0` to remove the target entirely.
  - The reserved alias can never be stored as a remote server, and is never
    shown to the user (the UI renders it as “⚡ This server”).
  - Local execution is session-isolated, timeout-bound, output-capped and
    killed by process group; audit events `connect.local` /
    `connect.local_failed` record every use.
- **Persistent local shell** for the terminal feature: per-user `bash` session
  with cwd/env continuity, per-command random end markers (output can never
  forge a boundary) and session teardown on timeout.
- **Layered architecture** (`domain` / `application` / `infrastructure` /
  `presentation`) with ports for persistence and command transports, a
  composition root and a strangler adapter for the legacy handlers. See
  `ARCHITECTURE.md`.
- **Redesigned UI**: one declarative screen layer (`View` + renderer), a single
  keyboard kit, grouped dashboards (command / system / workloads / storage /
  network), new Servers, Tools, Settings, Help and Administration screens,
  consistent back/home navigation and ✅-marked language picker.
- **Full localization of the interface** — 75 new keys shipped in all 15
  languages, with catalog parity (identical keys *and* placeholders) enforced by
  tests. Hardcoded English removed from the connect flow, main menu, language
  picker and disconnect flow.
- `/menu`, `/help` commands; `/audit` is now actually registered and restricted
  to administrators (it was implemented but unreachable, and unguarded).
- `scripts/verify_wheel.py` + CI step: fails the build if the wheel is missing
  subpackages or language catalogs.

### Changed

- Language catalogs moved from a Python dict to `src/presentation/i18n/catalog/
  <lang>.json`; `src/localization.py` is now a compatibility facade.
- `ssh_manager` is a `RoutingCommandManager`: every existing feature works on
  both SSH and local targets with no per-menu branching.
- Menus render through `parse_mode`-aware escaping, so MarkdownV2 and HTML
  languages can no longer break each other's screens.
- `pyproject.toml` packaging switched to explicit package discovery + package
  data (previously a wheel omitted subpackages).
- Version 4.0.0.

### Fixed

- `main_menu`, the language picker and the connect flow leaked English text
  into RTL and CJK sessions.
- Connection errors surfaced raw English strings and internal exception text.
- Adding a server could claim the internal self-host alias.
- The restore test relied on `os.rename` and failed across filesystems.

### Security

- Administrator actions on the self-host target are audited; a non-admin
  request for the reserved alias is denied before any lookup and flagged with
  `connect.denied`.
- `/audit` now requires the administrator role.

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
