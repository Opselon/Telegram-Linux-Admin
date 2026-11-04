# Security Model — Platform 4.0

Threat model (extends SECURITY.md): the web surface adds **remote unauthenticated attackers** (internet-facing if misconfigured) and **authenticated low-privilege users** (IDOR / privilege escalation) to the existing threats. Bale adds an untrusted message channel (any Bale user can message the bot) and untrusted mini-app payloads.

## Invariants

1. **Fail closed.** Unknown account, expired session, missing CSRF, unknown role → deny. Default role `read_only`; default access mode stays `admin_only`.
2. **Server-side authorization on every operation.** `services` check ownership + permission; API dependencies enforce session + permission; adapters re-check before rendering actionable UI. Frontend is never trusted.
3. **Ownership isolation (IDOR).** Every server-scoped request resolves the server by `(id, owner)` — a 404 for other users' servers; admin bypass only with explicit `servers.admin` permission held by `admin`/`owner` roles. Cross-tenant access attempts raise audit `security.denied` events.
4. **Command policy is non-negotiable.** SAFE/CONFIRM/BLOCKED bands and profile deny-patterns apply identically on Web/Bale/Telegram. CONFIRM tokens: one-shot, 180s TTL, bound to (account, server, exact command), constant-time compare. BLOCKED is never confirmable by any interface or role.
5. **Secrets.** Argon2id for web passwords (params: OWASP defaults m=64MB t=3 p=4 or environment-adjusted). Session cookie: 256-bit random, `HttpOnly; Secure; SameSite=Strict`, DB stores only SHA-256. Bot tokens, SSH secrets, Fernet keys never in logs (existing redact filter mandatory on all new log paths), never in API responses (masked fields only, write-only inputs).
6. **CSRF** double-submit on all mutating requests; `SameSite=Strict` as second layer; session rotation on login and role change.
7. **Rate limits (default-on):** login 5/min/IP + lockout 15min after 5 consecutive failures per (username,IP); general API 120/min/user; command execution 10/min/user; file upload 6/min. Implemented on the existing `RateLimiter` semantics.
8. **Input hardening:** Pydantic strict models; command ≤4096 bytes via `validate_command_text`; file paths via `FileService` policy (no `..`, no control chars, optional chroot list per server, symlink-resolve check, size caps); uploads validated by name+size, stored under generated names; no Docker socket exposure — docker ops run as whitelisted `docker` CLI commands through the command policy.
9. **Headers/CSP:** `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY` (SAMEORIGIN for the mini-app route if needed), `Referrer-Policy: no-referrer`, strict CSP (`default-src 'self'`; no inline scripts except nonced Vite output), HSTS behind TLS.
10. **Binding to loopback by default:** `TLA_WEB_HOST=127.0.0.1` default; production docs require TLS reverse proxy before exposure. `/metrics` stays on the health server and must not be bound publicly (docs + compose default).
11. **Audit:** every login (success/failure), permission/role change, server CRUD, command request/confirm/deny, file op, backup/restore, security change, denied authorization → `audit.record` with actor kind + correlation ID. No credentials or raw secret payloads persisted.
12. **Bale mini-app auth:** if native init-data exists, verify signature server-side per Bale's actual contract (timestamp + replay guard), bind to account via `bale_bindings`. Otherwise URL fallback + 6-digit single-use account-link code (5-min TTL, max 5 attempts). Never accept client-asserted user IDs.
13. **Dependencies:** pin new deps minimally (`fastapi`, `uvicorn`, `argon2-cffi`, `python-bale-bot`); `pip-audit` gate continues to run on the resolved wheel.

## What is explicitly out of scope

- MFA recovery codes (TOTP optional per account; recovery = owner CLI reset).
- Multi-node / HA deployments; SQLite stays single-writer.
- Encrypting audit JSONL at rest (documented limitation, unchanged from 3.x).
