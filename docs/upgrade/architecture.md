# Target Architecture — Unified Management Core

This document is the **binding contract** for the 4.0 upgrade. Parallel workstreams implement against it; deviations require orchestrator sign-off.

## 1. Runtime topology

One asyncio process (`src/runtime_app.py` → console script `tla-platform`). Components, in startup order:

1. Config + security configuration (existing `_configure_security`).
2. Database (existing `initialize_database()` + new migration runner).
3. `SSHManager` (existing singleton), `JobQueue`, `TargetResolver` (existing).
4. **FastAPI** via `uvicorn.Server` in-process (default bind `127.0.0.1:8080`, env `TLA_WEB_HOST/TLA_WEB_PORT`; loopback unless explicitly overridden).
5. Bale polling task (only when `BALE_BOT_TOKEN` configured).
6. Telegram polling (only when Telegram token configured) — unchanged behavior when Bale/web are absent.
7. Health server unchanged (`TLA_HEALTH_PORT`).

Graceful shutdown in reverse order. Every component reports readiness into `/readyz` (existing `_readiness_probe` extended: `web`, `bale` keys).

If a component fails to start (bad token), it degrades: log ERROR, mark unhealthy in readiness, do not kill the other adapters. Telegram remains fully functional standalone.

## 2. Service layer — `src/services/`

The single owner of business rules. Adapters never import `src.runtime`/`src.ssh_manager`/`src.command_policy` directly — they call services. Services are plain async classes holding references to the existing singletons; no new I/O primitives.

```python
# src/services/__init__.py exports a module-level accessor:
def get_services() -> PlatformServices   # built once by the runtime; raises if unbuilt

@dataclass
class PlatformServices:
    auth: AuthService           # accounts, sessions, roles, enrollment
    servers: ServerService      # CRUD + connect/disconnect/test + metadata
    commands: CommandService    # classify → confirm-token → execute via JobQueue
    jobs: JobQueryService       # job listing/cancel shared with JobQueue
    monitor: MonitoringService  # cached resource snapshots (bounded refresh)
    security: SecurityService   # access mode, whitelist, audit reads, alerts
    backups: BackupService      # create/list/verify/restore (extracted from bot/backup.py core)
    files: FileService          # scoped remote file ops (browse/read/download/upload/delete)
    notifications: NotificationService  # threshold + cooldown + dedup fan-out
    bale: BaleLinkService       # bale account binding, mini-app auth exchange
```

### 2.1 Identity model (breaking the Telegram-only assumption)

- New table `accounts(id INTEGER PK, kind TEXT CHECK(kind IN ('telegram','bale','web')), external_id TEXT NOT NULL, display_name TEXT, created_at, UNIQUE(kind, external_id))`.
- Existing `servers.owner_id` keeps its Telegram-ID semantics for legacy rows. Services resolve **owner** as `account_id`; a backfill migration maps each existing `users.telegram_id` to an `accounts` row and adds `servers.owner_account_id` (additive column, FK to accounts, nullable for legacy; filled by backfill). Read paths prefer `owner_account_id`, fall back to `owner_id`.
- Roles: `roles(account_id TEXT PK … )` — simpler: column `accounts.role TEXT NOT NULL DEFAULT 'read_only'` with values `owner|admin|operator|read_only`; the first enrolled account becomes `owner`. Granular permissions derive from role via a static table in `src/services/permissions.py`:

```text
owner:    * (all permissions, includes security.manage, users.manage)
admin:    servers.*, commands.*, services.*, processes.*, docker.*, firewall.read+modify,
          files.*, backups.*, monitoring.*, security.read, jobs.*
operator: servers.read, commands.execute+confirm (SAFE/CONFIRM), services.read+start+stop+restart,
          processes.read+kill, docker.read+start+stop+restart, firewall.read, files.read+upload+download,
          backups.create, monitoring.*, jobs.*
read_only: *.read / monitoring.* only
```

Permissions are checked **server-side on every request** (decorator/dependency in the API; explicit check in adapters). Frontend hiding is cosmetic only.

### 2.2 CommandService flow (shared by all three interfaces)

```python
async def request_execution(self, actor: Actor, alias: str, command: str) -> ExecutionPlan:
    # 1. actor.resolve_owner() → ownership/permission check (commands.execute)
    # 2. validate_command_text + PolicyEngine.evaluate (profiles) + command_policy.classify
    # 3. BLOCKED → deny (audit "command.blocked")
    # 4. SAFE  → enqueue via JobQueue, return job handle
    # 5. CONFIRM → create one-shot token bound to (account, alias, command, exp=180s),
    #    return pending plan; adapters render their own confirm UI

async def confirm(self, actor: Actor, token: str) -> JobHandle:
    # pops token (single-use, TTL, binding-checked), enqueues, audits

async def cancel(self, actor: Actor, job_id: str) -> bool   # owner-or-admin check
```

`Actor` dataclass: `account_id, kind ('telegram'|'bale'|'web'), display_name, role`. The existing Telegram `CONFIRMATIONS` store stays for the Telegram path until its handlers are migrated; new code uses the service store. Both enforce: single-use, 3-min TTL, constant-time compare, one pending per actor, BLOCKED never confirmable.

### 2.3 MonitoringService

Bounded cache: per-server snapshot refreshed at most every N seconds (default 15, env `TLA_MONITOR_INTERVAL`); concurrent refreshes coalesce; historical series kept in-memory ring buffer (last 120 samples) — **no new persistent metrics tables**. Dashboard reads cache; explicit "refresh now" bypasses cooldown with a per-actor rate limit.

### 2.4 NotificationService

Emits alerts (server_offline, ssh_auth_failure, cpu_high, ram_high, disk_low, service_failed, backup_failed, security_violation, command_failed). Dedup key `(server, kind)`; cooldown default 300s (env `TLA_ALERT_COOLDOWN`); per-account category opt-out stored in `notification_prefs`. Fan-out to: Bale accounts (bot message), Telegram admins (existing pattern), web (SSE/WS event).

## 3. HTTP API contract — `/api/v1`

FastAPI, typed Pydantic schemas in `src/api/schemas.py`, OpenAPI at `/api/docs`. Consistent error envelope: `{"error": {"code": str, "message": str, "details": obj|null}}`; HTTP codes: 400 validation, 401 unauthenticated, 403 unauthorized/forbidden, 404 unknown-or-not-yours (never leak existence), 409 conflict, 423 locked (login), 429 rate-limited.

### Auth & sessions

- `POST /api/v1/auth/login` `{username, password}` → sets `HttpOnly; Secure; SameSite=Strict; Path=/` session cookie. Argon2id hashes (`argon2-cffi`). Failures share one error; 5 failures → 15-min lockout per username+IP; all events audited. Optional TOTP (`TOTP_ENABLED` per account) → `POST /auth/mfa` second step.
- `POST /auth/logout`, `GET /auth/me` → `{account, role, permissions}`.
- Web admin enrollment: first-run bootstrap — when no web account exists, `GET /setup` status + `POST /setup` creates the initial OWNER with a one-time setup token printed by the CLI (`tla-platform setup-token`) or env `TLA_SETUP_TOKEN`. Setup endpoint is disabled once an owner exists.
- CSRF: double-submit cookie (`tla_csrf`, non-HttpOnly) + `X-CSRF-Token` header required on all mutating methods; sessions rotate on login/privilege change; revocation = delete session row.
- Session store: `web_sessions(token_hash PK, account_id, created_at, expires_at, last_seen, revoked)`; cookie holds random 256-bit token, DB stores an HMAC-SHA256 of it keyed with a process-local random key. Default idle expiry 12h, absolute 7d. Sessions do not survive a process restart (the key is regenerated), which is the safe direction: a restarted service drops its sessions rather than honoring hashes an attacker could have computed from a stolen DB.

### Endpoints (all under `/api/v1`, auth + CSRF + per-route permission)

```text
GET    /servers                          list (owner-scoped; admin sees all)
POST   /servers                          create (validate; test-connection optional pre-save)
GET    /servers/{id}                     details
PATCH  /servers/{id}                     rename/edit (alias, hostname, port, user, creds)
DELETE /servers/{id}
POST   /servers/{id}/connect|disconnect|test
GET    /servers/{id}/hostkey             pinned fingerprint + policy state

GET    /servers/{id}/monitoring          snapshot + ring-buffer history
GET    /servers/{id}/services            POST /services/{name}/{start|stop|restart|enable|disable}
GET    /servers/{id}/processes           ?search=&sort=   DELETE /processes/{pid} (confirm for dangerous)
GET    /servers/{id}/docker/containers   POST .../containers/{id}/{start|stop|restart}; GET .../logs; GET /images
GET    /servers/{id}/firewall/rules      POST /firewall/rules  DELETE /firewall/rules/{id}

GET    /servers/{id}/files?path=         browse (server-side path policy; traversal rejected)
GET    /servers/{id}/files/content?path= download (streamed)
POST   /servers/{id}/files/content       upload (multipart, size cap 50MB default)
DELETE /servers/{id}/files?path=         delete (requires files.delete)
POST   /servers/{id}/files/mkdir|rename

POST   /commands                         {server_id, command} → plan (safe→job, confirm→token, blocked→403)
POST   /commands/{token}/confirm         one-shot
GET    /jobs                             ?active=true&user=me
GET    /jobs/{id}                        GET /jobs/{id}/output
POST   /jobs/{id}/cancel

GET    /backups                          POST /backups  GET /backups/{id}/download
POST   /backups/{id}/verify              POST /backups/{id}/restore  DELETE /backups/{id}

GET    /security/overview                GET /security/events (audit tail)
GET    /users                            POST /users  PATCH /users/{id}  DELETE /users/{id}
GET|PUT /settings/security                access mode, whitelist, alert prefs

GET    /events/stream                    SSE: job updates + alerts (auth; per-event authz)
WS     /ws/jobs                          job progress frames (same auth as REST)
```

Pagination: `?limit=&cursor=` capped at 200. Request body cap 1MB (50MB uploads). Correlation-ID middleware reused from `src/observability`.

### Realtime

SSE for dashboard/alerts (simple, survives proxies), WS only for command output streaming. Both authenticate via the session cookie (and CSRF-exempt since read-only), per-event server-side permission check.

## 4. Bale adapter — `src/adapters/bale/`

Modules: `client.py` (Bot instance factory), `lifecycle.py` (start/stop polling task), `handlers.py` (commands `/start /status /servers /services /jobs /security /help`), `callbacks.py` (menu buttons → `CommandService` etc.), `keyboards.py`, `authorization.py` (Bale user → account binding; admin must pre-enroll: owner links Bale account from Web Security Center → 6-digit code flow), `notifications.py` (NotificationService fan-out), `miniapp.py`.

**Mini App policy:** implement launch via what the installed `python-bale-bot` SDK verifiably supports. Research step (Agent must do BEFORE writing miniapp code): install `python-bale-bot`, inspect source + the v2.4.5 docs for (a) URL-button/inline-button `url` field, (b) any WebApp/mini-app launch field, (c) `sendMessage`/callback payload limits. If a native mini-app init-data contract exists and is verifiable server-side, implement it; otherwise ship the **URL-button fallback** opening the responsive web app in Bale's in-app browser, with account linking by one-time code. Document which path shipped in `docs/upgrade/bale-miniapp-notes.md`. Do not fabricate API methods; do not monkey-patch the SDK.

## 5. Web frontend — `web/`

React 18 + TypeScript + Vite + Tailwind; no state library beyond React Query (server state) — keep deps lean. i18n via `i18next` with `fa` (RTL, default alongside en) and `en`; `dir` switches at the html root. Theme: dark default, light toggle. Mobile-first layout with drawer navigation; Bale WebView treated as mobile Safari-class browser.

Routes: `/login`, `/` dashboard, `/servers`, `/servers/:id` (tabs: overview/monitoring/services/processes/docker/firewall/files), `/console` (command center), `/jobs`, `/backups`, `/security`, `/settings`. Typed API client generated against the OpenAPI schema (hand-rolled minimal fetch wrapper is acceptable; no codegen dependency in CI).

Build output `web/dist` served by FastAPI `StaticFiles` (SPA fallback) when present; reverse-proxy docs cover the alternative.

## 6. File ownership (no cross-editing)

| Area | Owner | Paths |
|---|---|---|
| Orchestrator/core | Orchestrator | `src/services/`, `src/api/`, migrations, `src/runtime_app.py`, pyproject, CI, Docker, docs/upgrade |
| Frontend | Frontend agent | `web/**` only |
| Bale | Bale agent | `src/adapters/bale/**`, `docs/upgrade/bale-miniapp-notes.md` |
| Tests | respective owners | `tests/test_web_*.py`, `tests/test_bale_*.py`, `tests/test_services_*.py` |

Shared touchpoints (both agents may append, orchestrator resolves): `pyproject.toml` (only via orchestrator — declare needed deps in your report), `.env.example`, docs. **Never** edit `src/bot/**` (except orchestrator), never add a dependency by editing pyproject directly — request it.

## 7. Compatibility invariants

1. Telegram callback patterns and handler registration order unchanged; `EXPECTED_CALLBACK_ROUTES` test stays green.
2. Existing DB rows remain valid; migrations additive + backfill.
3. `tla-bot` entry point unchanged (Telegram-only mode); new `tla-platform` adds web+bale.
4. Coverage floor 50% maintained; new modules ≥80%.
5. No secret ever logged (reuse `src.observability.redact`).
