# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
semantic versioning.

## [3.4.0] — 2026-10-22

Setup gets out of the way: one command configures a production-grade bot, and
both the one-liner and the guided wizard are covered by an end-to-end suite.

### Added

- **One-command setup** — `install.sh --token … --admin-id …` (or
  `curl …/install.sh | sudo bash -s -- …`, or
  `scripts/setup.py --easy`) takes a fresh host to a running systemd service:
  checkout update, venv, fail-closed config (`whitelist` + `deny_public`, no
  destinations, mode `0600`), database, `telegram_bot.service`, auto-update
  cron, start and readiness wait. Idempotent — re-running updates in place.
- **`--json` summary** for automation: paths, service state, health payloads,
  and a redacted token (`1234…cdef`); the real token never leaves the `0600`
  config.
- **Health port selection** (`--port`, default `8081`, bound to `127.0.0.1`)
  that walks upward when the port is taken and tells systemd the chosen value.
- **`scripts/e2e_setup.sh`** — end-to-end harness that runs the easy path and
  the interactive wizard in throwaway copies, boots each resulting bot and
  checks `/healthz`, `/readyz`, `/metrics` plus a live Telegram `getMe`.
- **`tests/test_easy_setup.py`** — pytest coverage of argument parsing,
  credential discovery, the fail-closed config, systemd unit rendering,
  the full `easy_setup` pipeline, both wizard routes, and (opt-in via
  `TLA_E2E_TOKEN`) a real bot process against the real API.

### Fixed

- `scripts/setup.py` no longer aborts the management menu with an
  indentation error on the final branch.
- Percent signs and quotes in paths are escaped in generated systemd units,
  so a checkout under `100% complete/` no longer produces a broken unit.
- The service account follows `SUDO_USER`, so `sudo ./install.sh` runs the
  unit as the checkout owner instead of root.

## [3.3.0] — 2026-10-22

The maintenance wave: the bot now runs its own housekeeping, shows live job
state, and renders menus through one hardened UI toolkit.

### Added

- **Ops scheduler** (`src/ops/scheduler.py`) — zero-dependency asyncio
  recurring-task engine: per-task status (`last_run`, `next_run`,
  `last_error`, `run_count`), errors captured with tracebacks without
  stopping the loop, deterministic manual-tick mode for tests, clean
  `stop()` with no leaked tasks.
- **Maintenance jobs** (`src/ops/maintenance.py`) — audit-log rotation
  (gzip old files, keep a bounded set, config-driven, **off by default**),
  hourly SSH pool reaper, minute-scale metrics ticker pushing
  `ops_scheduler_tasks` / `ops_queue_*` gauges into `METRICS`. Every job
  degrades via a `MaintenanceResult` (+ `ops_maintenance_degraded_total`)
  instead of raising.
- **Deep health check** (`src/ops/healthcheck.py`) — `deep_readiness()`
  adds DB connectivity, audit-dir writability, and token-shape checks (the
  secret is never echoed into a diagnostic) to the process health
  surface; folded into the bot's readiness probe.
- **`/jobs` panel** (`src/bot/jobs_ui.py`) — per-user view of running and
  waiting jobs with inline cancel buttons, refresh row, and a degraded
  answer when the queue did not start. `jobscancel_<id>` /
  `jobsrefresh` callback prefixes, answer-before-edit throughout.
- **Job queue hardening** — timeout overruns resolve to a clean
  `JobResult(exit_ok=False, error="timeout")` (including the Python 3.12
  `BaseExceptionGroup` edge); new `stats_per_user()` breakdown embedded
  in `stats()`; METRICS bridge proven with labeled counters.
- **UI toolkit** (`src/bot/ui_kit.py` + `src/bot/pagination.py`) —
  `menu()` / `kv_panel()` / `paged()` builders: deterministic ordering,
  truncation and clamping, fence-triple defanging so user data cannot
  close a code block early, stateless pagination helpers with absolute
  callback data. The language keyboard and the dashboard metric block now
  render through it.
- **Bot wiring** — the scheduler starts in `post_init` (failure is logged,
  never fatal) and stops in `post_shutdown`; `/jobs` and its callbacks are
  registered exactly once each.

### Tests

573 → 715 tests (new suites: jobs UI, ui_kit, pagination, ops scheduler,
maintenance, healthcheck) — plus the wiring covered by the existing
surface tests.

## [3.2.0] — 2026-10-22

The remaster: one file became a package, and the bot learned to run commands
on the machine it lives on.

### Added

- **Modular architecture** (`src/bot/`) — the 3,779-line `src/main.py` was
  split into 15+ focused modules (`state`, `application`, `process`, `servers`,
  `commands`, `monitoring`, `docker`, `files`, `firewall`, `backup`, `packages`,
  `processes`, `services`, `shell`, `admin`, `system`, `support`). The old
  entry point stays importable: every 3.1 name re-exports from `src.main`, and
  `unittest.mock.patch("src.main.<name>")` keeps working (the facade forwards
  `__setattr__` into the shared state module, which `__getattr__` alone cannot
  observe).
- **Self-VPS execution** (`src/runtime/`) — one `ExecutionTarget` contract for
  "run here" and "run over SSH". The reserved alias `self` executes commands
  directly on the bot's own host with the same pid-first streaming, byte cap
  and timeout semantics as the SSH path. Enabled by default;
  `TLA_LOCAL_EXECUTION=0` disables it.
- **Job queue** (`src/jobs/`) — bounded asyncio queue: one running command per
  user, global concurrency cap, pending caps per user and total, priority with
  FIFO, `dedupe_key` coalescing, cancellation, graceful drain, per-user stats.
- **Operator policy** (`src/policy/`) — `readonly` / `standard` / `full` /
  `locked` authority profiles layered on top of the command risk classifier,
  with per-alias binding through the `command_profiles` config key, service
  allowlists, extra deny regexes and per-alias `command_profiles`.
- **Observability** (`src/observability/`) — JSON-lines file logging with
  correlation ids and `contextvars` field binding, handler instrumentation
  (duration, outcome, error type) and `redact()` for bot tokens, passwords,
  bearer headers and private-key blocks.
- **Wiring** (`src/bot/integration.py`) — the single place subsystems meet the
  handlers: policy evaluation runs before the risk classifier on every
  free-form command, execution is routed through the runtime resolver (local
  and remote behind one call), queue and metrics are installed at startup and
  drained at shutdown. Every integration accessor degrades to the 3.1 behavior
  when its subsystem is absent.
- **CI** — one `ci.yml` gate (ruff, mypy, workflow validation, tests on Linux
  and Windows, wheel build + clean-venv install smoke, Docker build with
  health-endpoint probe, pip-audit on the wheel's resolved dependencies) and a
  rebuilt `release.yml` (three-OS PyInstaller matrix, deb + rpm packages,
  SHA256 checksums, generated release notes on tag push).
- 80+ tests covering the runtime targets, queue semantics, policy matrices and
  observability contracts; 17 new integration tests drive the real handler
  against a real local subprocess.

### Changed

- `/readyz` reports `transport` instead of an internal module path, and always
  includes the SSH pool shape.
- The wheel ships every subpackage; the console script and health endpoints
  are verified in CI from a clean venv, not just compiled.

### Fixed

- `httpx`/`httpcore` read-timeout exception types are resolved to real classes;
  3.1 referenced undefined names on every non-timeout error path.
- Duplicate handler imports and the `post_init` parameter shadowing that
  mypy's baseline had absorbed are gone; the per-module mypy baseline now
  tracks each `src/bot` module, and the new subsystems are fully typed.

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
