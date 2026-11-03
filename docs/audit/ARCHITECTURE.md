# Telegram Linux Admin — Architecture Map

**Audited commit:** `07fd561` (v3.5.0)
**Auditor:** direct source inspection (Phase Zero discovery)

## 1. What the project is

A single-process Python 3.12 Telegram bot (python-telegram-bot 22.8) that lets
whitelisted Telegram users run shell commands and admin operations on Linux
servers over SSH, plus manage the bot itself (backups, whitelist, updates).

- **Language/runtime:** Python ≥3.12, asyncio, single long-running process
- **Bot framework:** python-telegram-bot (PTB) 22.8, long polling
- **SSH:** asyncssh 2.24 with a custom TOFU host-key layer and connection pool
- **Persistence:** SQLite (stdlib `sqlite3`), one shared connection, WAL mode
- **Crypto:** Fernet (cryptography 50) with a versioned JSON key file
- **Concurrency:** a bounded in-process jobs queue (`src/jobs/queue.py`)
- **Survivability:** PTB's built-in `ConversationHandler` persistence is *not*
  configured — conversation state lives in memory only (see §6)

## 2. Module inventory (25,203 lines of Python)

### Core (`src/`)
| Module | Lines | Role |
| --- | --- | --- |
| `main.py` | 223 | Entry point; thin re-export facade over `src.bot` |
| `config.py` | — | Config loader (`config.json` + env) |
| `database.py` | 666 | SQLite schema, CRUD, dashboard statistics |
| `security.py` | 118 | Fernet encrypt/decrypt with versioned keys |
| `ssh_manager.py` | 483 | SSH pool, TOFU host-key pinning, command runner |
| `access_control.py` | — | Whitelist + setup-PIN enrollment |
| `validators.py` | — | Alias/hostname/username/key-path validators |
| `destination_policy.py` | — | Allow/deny rules for SSH target hosts |
| `command_policy.py` | — | Per-profile command deny patterns + CONFIRMATIONS |
| `localization.py` | 899 | 15 languages, 103 keys |
| `audit.py` | — | Append-only audit log |
| `metrics.py` | — | In-memory counters |
| `health.py` | — | HTTP health server |
| `updater.py` | — | GitHub release download + restart |
| `parse_mode.py` | — | MarkdownV2/HTML message builder |
| `logger_config.py` | — | Logging setup + redaction |

### Bot interaction layer (`src/bot/`) — 16 modules, ~3.8k lines
`application.py` builds the PTB Application and registers **every** handler.
`state.py` owns the mutable singletons (ssh_manager, caches, conversation-state
ints). Handler modules: `servers`, `commands`, `shell`, `system`, `firewall`,
`services`, `processes`, `docker`, `packages`, `files`, `monitoring`,
`navigation`, `backup`, `admin`, `security_center`, `jobs_ui`, `support`,
`host_status`, `errors`, `lifecycle`, `process`, `integration`, `pagination`,
`ui_kit`.

### Subsystems introduced after v3.1
- `src/jobs/` — `queue.py` (446), `models.py`, `errors.py`: serialized bounded
  background command execution
- `src/runtime/` — `resolver.py`, `target.py`, `local.py`, `remote.py`: maps an
  alias to an execution target (`self` = bot host, else pooled SSH)
- `src/policy/` — `engine.py`, `profiles.py`: bounds what an operator profile
  may run
- `src/ops/` — `scheduler.py`, `maintenance.py`, `healthcheck.py`: recurring
  background tasks
- `src/observability/` — `middleware.py`: structured op-id logging

## 3. Data model

```
users (telegram_id UNIQUE, plan)
servers (id, owner_id → users.telegram_id, alias, hostname, user,
         password [Fernet], key_path [Fernet], host_key, created_at)
user_preferences (telegram_id PK, language)
```

**There is no `port` column.** SSH port 22 is hardcoded — see
`docs/audit/DATABASE_AUDIT.md` P1-01.

## 4. Request flow (callback)

```
Telegram update
  → PTB handler graph (src/bot/application.py)
  → ConversationHandler (in-memory state machine)
  → handler module (src/bot/<area>.py)
  → state.<singleton>  (src/bot/state.py)
  → src/database.py (shared sqlite3 conn, WAL)
  → src/ssh_manager.py (pool → asyncssh)
  → remote host
```

## 5. Security architecture

- **Authn:** Telegram user ID against a whitelist; no password for the bot
- **Authz:** admin-only / whitelist modes (`src/access_control.py`)
- **Secrets at rest:** Fernet, key in `var/encryption.key` (0600), versioned
- **Secrets in transit:** never echoed to users; audit log records aliases only
- **SSH host keys:** TOFU pinned to DB (not `known_hosts`), `secrets.compare_digest`
- **Command safety:** base64 envelope (`wrap_remote_command`), policy deny-lists

## 6. Known structural limits (from Phase Zero)

1. **No conversation persistence.** `Application.builder()` has no
   `.persistence(...)`, so every `ConversationHandler` state is lost on
   restart; a user mid-"Add Server" is silently dropped.
2. **No SSH port support.** Schema and flow both lack it.
3. **52 localization keys missing in all 14 non-English languages.**
4. **Backup copies `database.db` while the bot holds an open WAL connection**
   (see `docs/audit/DATABASE_AUDIT.md`).
5. **~415 known mypy findings in `src/bot/*`** are baseline-suppressed in
   `pyproject.toml` rather than fixed.
