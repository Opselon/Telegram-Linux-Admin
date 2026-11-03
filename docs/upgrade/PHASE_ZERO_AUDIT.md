# Phase Zero Audit — Telegram Linux Admin

**Audited commit:** `b0cd3a1cdc3f1289b889c8d84467d8f7b84ac375` (`main`, 2026-11-03) — verified against the actual source tree, not prior audit documents.
**Baseline test run:** 784 passed, 13 skipped (`pytest`, local, Windows).

The four documents in `docs/audit/` are pinned to commit `07fd561` (v3.5.0) and are **stale on two points**: custom SSH-port support was added in `24d7c4d` (`tests/test_add_server_port.py`, `servers.py` PORT state), and the CONFIRM-band confirm/cancel buttons are wired again (`src/bot/application.py:278-279`, regression-tested in `test_handler_graph_wiring.py`). Everything below reflects the current HEAD.

---

## 1. Existing architecture

Single asyncio Python process; ~25k lines of Python.

- **Entry:** `src/bot/process.py:main()` — lock file → token preflight → `_configure_security` → `initialize_database()` → `SSHManager` → `integration.install_subsystems()` → PTB `run_polling()`. Health server (stdlib asyncio HTTP) starts only when `TLA_HEALTH_PORT` is set (`src/bot/lifecycle.py:158`), bind default `127.0.0.1`; `SMOKE_TEST=1` runs health-only.
- **Telegram surface:** `src/bot/application.py:build_application()` registers 10 commands, 9 ConversationHandlers, ~45 CallbackQueryHandlers. Callback patterns are a wire contract (buttons already in user chats); registration order is load-bearing (ConversationHandlers first, `jobscancel_` before `jobsrefresh`, `remove_` after `remove_server_menu`). A global TEXT handler (`whitelist_add_capture`, `application.py:249`) no-ops unless armed by `sec_add_start` — moving it into an earlier dispatch group would steal all text.
- **Composition root:** `src/bot/integration.py:install_subsystems()` builds `TargetResolver`, `JobQueue`, policy profiles onto `src/bot/state.py` singletons.
- **Modules (all transport-agnostic, zero Telegram imports):** `src/database.py`, `src/access_control.py`, `src/command_policy.py`, `src/policy/`, `src/jobs/`, `src/runtime/`, `src/ssh_manager.py`, `src/localization.py`, `src/security.py` (Fernet), `src/audit.py` (JSONL), `src/metrics.py`, `src/observability/` (redaction filter, correlation-ID middleware), `src/health.py`, `src/ops/` (scheduler/maintenance/healthcheck).

## 2. Existing working functionality

Servers CRUD (add/connect/disconnect/remove, custom SSH port, host-key pinning with TOFU/strict policies), free-form command execution with SAFE/CONFIRM/BLOCKED classification + one-shot confirmations (3-min TTL, single-use, constant-time compare), interactive shell, service/process/package/docker/firewall/file management, live monitoring, backups (WAL-consistent snapshot + atomic restore with safety backup and self-restart), Security Center (access mode, whitelist management, audit view), jobs UI (in-memory bounded queue), dashboard, i18n (15 languages), health/metrics endpoints, Prometheus metrics, structured logging with secret redaction.

## 3. Broken functionality

Nothing verified as outright broken at HEAD. Prior-audit issues that were fixed: dead confirm buttons (3.2 regression), missing SSH-port column. Two stale-doc claims corrected above.

## 4. Missing functionality

- **No web application / HTTP API** — the only listening component is the zero-dependency health server (no routing, no auth, no body parsing).
- **No Bale integration** of any kind.
- **No RBAC / roles** — authorization is a flat admin allowlist + whitelist + access mode (`src/access_control.py:PermissionManager`).
- **No web sessions, CSRF, Argon2id password storage, MFA.**
- Server **rename/edit/test-connection** absent in bot UI; no server labels.
- No rename/edit of servers via any interface; no notification/alerting system (thresholds, cooldowns, dedup).
- No schema versioning / migration runner (`SCHEMA_VERSION = 1` is decorative; evolution is ad-hoc `_ensure_column`).
- **Job engine not used by free-form commands** — `src/bot/commands.py` streams directly via `integration.run_target`; the JobQueue is wired but bypassed on that path.

## 5. Security issues (verified at HEAD)

1. `TLA_ENCRYPTION_KEY` env var is **documented** (SECURITY.md, README) but **never read** by `src/security.py` — only `TLA_ENCRYPTION_KEY_FILE` exists. Doc/code drift that will bite container deployments.
2. `/metrics` and `/readyz` are unauthenticated; Docker sets `TLA_HEALTH_HOST=0.0.0.0` — information disclosure when deployed per current defaults.
3. `security/update_manifest.json` HMAC signature is a placeholder (`REPLACE_WITH_HMAC`); `security/requirements.lock` is empty — the self-update chain-of-trust is aspirational.
4. `src/bot/files.py:163` — upload uses the Telegram document's original filename directly as local temp path (hostile filename can escape CWD).
5. File manager has **no path-scoping/chroot** — any path on the remote host is reachable (by design for an admin bot, but this must not be reused as-is for a web file API without a policy layer).
6. Rate limiting is opt-in per call site (only shell + security-center decorated); a web API needs default-on limits.
7. `linux_secure_manager.sh` installs a systemd unit running as **root**; bot unit has no hardening directives.
8. No CSRF/session infrastructure exists at all (nothing to reuse for web auth).

Controls already strong: Fernet-encrypted SSH secrets with versioned key rotation, base64 command envelope (no shell-quoting breakage), TOFU host-key pinning with `secrets.compare_digest`, cloud-metadata/SSRF destination policy, parameterized SQL throughout, log redaction filter, fail-closed `admin_only` default, PIN enrollment with lockout.

## 6. Performance issues

- No `busy_timeout` pragma on SQLite (relies on a process-wide `RLock`).
- Single shared connection with `check_same_thread=False`; autocommit mode; fine for current load, must remain single-writer.
- Monitoring polling does remote `ps`-class commands per refresh; live monitoring tasks are per-user and unbounded beyond a cap (needs bounds when web clients poll).
- In-memory job/confirmation state is lost on restart (acceptable for jobs; noted for migration).

## 7. Database problems

- Tables: `users(telegram_id UNIQUE, plan)`, `servers(owner_id→users.telegram_id, alias, hostname, user, password, key_path, host_key, port, UNIQUE(owner_id,alias))`, `user_preferences(telegram_id, language)`.
- **Telegram-coupled schema naming** (`owner_id`/`telegram_id`) — Bale bindings and web users need either new tables or a platform-neutral account model.
- No migrations runner; no foreign_keys in older DBs is fine (pragma is on), but there is no integrity-check routine on startup.

## 8. Deployment limitations

- Docker: multi-stage py3.12-slim, non-root `appuser`, `VOLUME /app/data`, healthcheck only when `TLA_HEALTH_PORT` set. No reverse-proxy docs, no TLS story, no web port.
- install.sh: venv + systemd, no hardening directives, no web configuration.
- CD (`cd.yml`) deploys via SSH secrets, gracefully skips when unset.
- pyproject `packages` list is **explicit** — any new subpackage must be added there or the wheel silently omits it.

## 9. Proposed target architecture

One process, one management core, three adapters:

```text
src/services/      ← NEW shared management core (authorization, servers, commands,
                     jobs, monitoring, security ops) — the ONLY place business
                     rules live; Telegram handlers, Bale adapter, and the web
                     API all call it.
src/api/           ← NEW FastAPI application (cookie sessions, CSRF, RBAC,
                     REST + WebSocket), mounted by the same runtime.
src/adapters/bale/ ← NEW Bale bot adapter (python-bale-bot), calling services.
src/bot/           ← Existing Telegram handlers, refactored to call services
                     (incremental; callback wire format unchanged).
web/               ← NEW React/TypeScript/Vite frontend (served by FastAPI or
                     a reverse proxy), consumes /api/v1.
```

- **Migrations:** new `schema_migrations` table with a real runner; additive DDL; pre-migration snapshot via existing `snapshot_database_to`. New tables: `accounts` (platform-neutral identity), `web_users` (Argon2id), `sessions`, `roles/permissions` mapping, `bale_bindings`, `notification_prefs`, `audit` stays JSONL. Existing Telegram tables are preserved and keep working.
- **Runtime:** single coordinated asyncio process: PTB polling + Bale polling + FastAPI (uvicorn-in-process on loopback by default) + health server. One `SSHManager`, one `JobQueue`, one DB connection manager. No Redis/Kafka.
- **Confirmations:** reuse `ConfirmationStore` semantics; web gets the same one-shot tokens bound to (user, server, command, expiry); BLOCKED stays blocked everywhere.

## 10. Migration risks

1. `pyproject packages` omission → wheel missing `src.services`/`src.api`/`src.adapters` (silent until install-smoke).
2. mypy per-module baselines (`pyproject.toml:163-249`) — new modules must be fully typed or explicitly baselined; changing existing modules can shift baseline counts.
3. Handler-graph regressions — mitigated by `tests/test_handler_graph_wiring.py` (real dispatch graph) + `EXPECTED_CALLBACK_ROUTES`.
4. Coverage floor 50% — adding untested code lowers coverage below the gate; every new module ships with tests.
5. DB migration on live installs — must snapshot first (WAL-aware) and stay transactional; additive-only.
6. Python 3.14 local-dev patch incompatibilities observed (broken pip/setuptools on the dev machine) — CI matrix (3.12/3.13) is the source of truth.

## 11. Current test baseline

45 test files, 784 collected tests passing, 13 skipped (platform-gated). Style: direct-handler-invocation with `AsyncMock`, fake SSH connections; real-graph wiring test; conftest autouse fixture resets security singletons. Coverage measured 55.4% (floor 50). CI gates: ruff, mypy (blocking), pytest+coverage (ubuntu+windows), wheel install smoke, docker build+health smoke, pip-audit, CodeQL/gitleaks/dependency-review; `ci-summary` is the branch-protection status.

## 12. Prioritized implementation plan

1. **Shared service layer** (`src/services/`) extracted from the transport-agnostic modules — no behavior change; Telegram keeps working.
2. **DB migrations** runner + new tables (accounts, web_users, sessions, roles, bale_bindings, notification_prefs).
3. **FastAPI backend** `/api/v1`: auth (Argon2id + secure cookies + CSRF + rate limits + lockout), RBAC (OWNER/ADMIN/OPERATOR/READ_ONLY with granular permissions), servers/commands/jobs/monitoring/services/processes/docker/firewall/files/backups/security/audit routers, WebSocket for job progress, OpenAPI.
4. **Bale adapter** (`src/adapters/bale/`): bot with interactive menu, shared authorization/confirmations, notifications with dedup/cooldown, Mini App launch via verified SDK capability with secure fallback (account-link OTP) if native init-data is unavailable.
5. **Web frontend** (`web/`): React+TS+Vite+Tailwind, dark/light, RTL/fa first-class, dashboard/servers/console/security/backups, WebSocket job stream.
6. **Tests** mirroring the above + cross-interface e2e + Telegram regression (existing suite stays green).
7. **Deployment/docs**: Dockerfile update (web build stage), compose, nginx example, systemd hardening, HTTPS guide; fix `TLA_ENCRYPTION_KEY` gap; `/metrics` auth note.
8. **Security review** of every new surface; CI updates; PR.
