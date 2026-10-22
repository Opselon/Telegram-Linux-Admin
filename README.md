<p align="center">

  <!-- Core Badges -->

  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge">
  <img src="https://img.shields.io/badge/Asyncio-Enabled-4B8BBE?style=for-the-badge">
  <img src="https://img.shields.io/badge/SSH-Secure-green?style=for-the-badge">
  <img src="https://img.shields.io/badge/Encryption-Post--Quantum-orange?style=for-the-badge">

  <!-- Repo Badges -->

  <img src="https://img.shields.io/github/stars/Opselon/Telegram-Linux-Admin?style=for-the-badge">
  <img src="https://img.shields.io/github/forks/Opselon/Telegram-Linux-Admin?style=for-the-badge">
  <img src="https://img.shields.io/github/watchers/Opselon/Telegram-Linux-Admin?style=for-the-badge">
  <img src="https://img.shields.io/github/repo-size/Opselon/Telegram-Linux-Admin?style=for-the-badge">

  <!-- Versioning / Releases -->

  <img src="https://img.shields.io/github/v/release/Opselon/Telegram-Linux-Admin?style=for-the-badge">
  <img src="https://img.shields.io/github/release-date/Opselon/Telegram-Linux-Admin?style=for-the-badge">
  <img src="https://img.shields.io/github/commits-since/Opselon/Telegram-Linux-Admin/latest?style=for-the-badge">

  <!-- Downloads -->

  <img src="https://img.shields.io/github/downloads/Opselon/Telegram-Linux-Admin/total?style=for-the-badge">
  <img src="https://img.shields.io/github/downloads/Opselon/Telegram-Linux-Admin/latest/total?style=for-the-badge">

  <!-- CI / Code Quality -->

  <img src="https://img.shields.io/github/actions/workflow/status/Opselon/Telegram-Linux-Admin/tests.yml?label=Tests&style=for-the-badge">
  <img src="https://img.shields.io/lgtm/grade/python/github/Opselon/Telegram-Linux-Admin?style=for-the-badge">
  <img src="https://img.shields.io/codefactor/grade/github/Opselon/Telegram-Linux-Admin?style=for-the-badge">

  <!-- Activity -->

  <img src="https://img.shields.io/github/last-commit/Opselon/Telegram-Linux-Admin?style=for-the-badge">
  <img src="https://img.shields.io/github/commit-activity/m/Opselon/Telegram-Linux-Admin?style=for-the-badge">

  <!-- License -->

  <img src="https://img.shields.io/github/license/MIT?style=for-the-badge">

  <!-- Docker -->

  <img src="https://img.shields.io/badge/Docker-Ready-0db7ed?style=for-the-badge">

  <!-- Multi Language -->

  <img src="https://img.shields.io/badge/Multi--Language-15+_Languages-purple?style=for-the-badge">

  <!-- New Features -->

  <img src="https://img.shields.io/badge/Post--Quantum-Encryption-red?style=for-the-badge">
  <img src="https://img.shields.io/badge/Admin-Dashboard-blue?style=for-the-badge">

</p>


# 🚀 Telegram Linux Admin (Python Edition)

A modern, **post-quantum encrypted**, multi-user, multi-language **Telegram SSH management bot** that transforms your Telegram chat into a secure command center for all your Linux servers.

This project is designed for professionals, sysadmins, DevOps engineers, and server owners who want **maximum security** with **zero learning curve** — directly inside Telegram.

---

# 🔐 Extreme Security — Post-Quantum Ready

This bot is built with **enterprise-grade security standards**:

### ✔️ Post-Quantum Encryption

* **Quantum-Resistant Cryptography**: All server credentials encrypted with post-quantum encryption
* **Hybrid Encryption**: Combines quantum-resistant key derivation (SHA-3-512) with AES-256-GCM
* **Future-Proof**: Protection against both classical and quantum attacks
* **Backward Compatible**: Seamlessly works with existing encrypted data
* **Zero Trust Architecture**: Users can trust the bot with their server credentials

### ✔️ Full Encryption for All Secrets

* SSH passwords
* SSH private key paths
* Server configuration
* All encrypted using **Post-Quantum + Fernet 256-bit symmetric crypto** with restricted (`0600`) key file

### ✔️ Per-User Isolation

Each Telegram user only sees **their own servers**.
No one can access another user's machines.

### ✔️ Zero Plaintext Storage

No credentials are ever stored unencrypted.

### ✔️ Hidden Admin-Only Commands

Only the installer/admin user can run:

* Bot update
* Maintenance commands
* Config-level actions
* Admin dashboard (`/dashboard`)

Other users **never see admin options**.

### ✔️ Easy for Anyone Worldwide

The bot now supports **15+ languages** with intelligent parse mode selection — every user can manage their own servers safely and independently.

---

# 📈 SEO-Optimized Feature Overview

This section is optimized for Google ranking on target keywords:

**telegram linux admin bot**, **telegram ssh bot**,
**secure telegram ssh manager**, **linux server telegram bot**,
**remote linux management bot**, **telegram devops tools**,
**telegram server admin**, **telegram ssh terminal bot**,
**post-quantum encryption telegram bot**, **quantum-safe ssh bot**

---

# ⚙️ Features

## 🔧 Server Management

* Add unlimited Linux servers
* Password or SSH-key authentication
* Persistent shell sessions
* **15+ languages** with intelligent parse mode selection
* Per-user server list isolation
* Real-time command execution with live output streaming

## 🔒 Security Layer

* **Post-Quantum Encryption** for all credentials
* Encrypted database secrets
* Auto-generated encryption key
* Per-user sandboxed environments
* Hidden admin controls
* No plaintext secrets stored anywhere
* Quantum-resistant key derivation

## 🛠 System Controls

* Reboot / shutdown
* System info (CPU, RAM, Disk, Network)
* Process manager with kill/inspect
* Service manager (`systemd`)
* Package manager (apt/yum)
* Docker control
* Firewall management (UFW)
* File upload/download
* Real-time command monitoring

## 📦 Persistence & Backup

* **Professional Backup System**:
  - Progress indication
  - File validation and integrity checks
  - SHA-256 checksums
  - Metadata storage
  - Maximum compression
  - ZIP integrity verification
  
* **Advanced Restore System**:
  - Step-by-step progress updates
  - Automatic safety backup before restore
  - Rollback mechanism on failure
  - File validation at every step
  - JSON structure validation
  - Atomic file operations

* Encrypted SQLite database
* Self-updating mechanism
* Auto-updater with rollback

## 📊 Admin Dashboard

* **Comprehensive Statistics**:
  - Total users and active users
  - Users joined today
  - Total servers and servers added today
  - Plan distribution (free/premium)
  - Language distribution (top 5)
  - Recent servers
  - Server statistics (avg/max/min per user)

* **Real-time Analytics**:
  - Live statistics
  - User activity tracking
  - Server usage metrics
  - Language preferences

* Access via `/dashboard` command (admin only)

## 🌐 Multi-Language Support

* **15+ Supported Languages**:
  - English, Arabic, Persian, French, German, Spanish
  - Portuguese, Italian, Russian, Turkish
  - Chinese, Japanese, Korean
  - Hindi, Urdu

* **Intelligent Parse Mode System**:
  - Language-aware formatting
  - Automatic parse mode selection
  - MarkdownV2 for most languages
  - HTML for RTL and CJK languages
  - Proper escaping for all languages
  - Professional message formatting

## 🚨 Error Handling

* **Robust Error Management**:
  - Graceful timeout handling
  - Network error recovery
  - Message modification error handling
  - Comprehensive logging
  - User-friendly error messages
  - Automatic retry mechanisms

---

# 🚀 Installation

## Easy setup — one command

```bash
curl -fsSL https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/install.sh \
  -o install.sh && sudo bash install.sh \
  --token "123456:ABC-DEF...-..." --admin-id 123456789
```

Already cloned the repo?

```bash
sudo ./install.sh --token "123456:ABC-DEF...-..." --admin-id 123456789
```

That one command clones (or updates) the checkout, builds the venv, writes a
fail-closed config in one shot — `access_mode=whitelist`,
`destination_policy=deny_public`, no open destinations, file mode `0600` —,
creates the SQLite database, installs and starts the `telegram_bot.service`
systemd unit plus the auto-update cron job, and prints a summary with the file
list, the applied config, the service state, the last journal lines and the
health reports. The health endpoint binds to `127.0.0.1:8081` and steps up to a
free port if taken (`--port` pins it). `--admin-id` may repeat to whitelist
more users. By default the service runs as the invoking user (`SUDO_USER`),
not root.

Fine-tuning flags (also accepted by `scripts/setup.py --easy`):
`--db-file`, `--audit-dir`, `--service-name`, `--no-service`, `--no-cron`,
`--no-start`, `--destination-policy`, `--port`, `--json`, `--yes`, `-q`.

## Pro setup — interactive wizard

```bash
./install.sh
```

Run it with no flags (and no `TLA_BOT_TOKEN` exported) to get the guided
wizard that walks through:

- Telegram bot token configuration (validated against Telegram on the spot)
- Admin user setup
- Systemd service installation (optional)
- Automatic updates configuration (optional)
- Locked-door policies and manual key editing

## End-to-end test of both paths

```bash
TLA_BOT_TOKEN="123456:ABC-DEF...-..." TLA_ADMIN_ID=123456789 scripts/e2e_setup.sh all
```

`scripts/e2e_setup.sh` exercises both paths with the real Telegram API: each
run works inside a throwaway copy (config never touches the host), drives the
easy one-liner non-interactively and the wizard from stdin, boots the resulting
bot process and verifies `/healthz`, `/readyz`, `/metrics` and `getMe`.
Modes: `easy`, `pro`, or `all` (default). Set `KEEP=1` to keep the scratch
directories for inspection, or `TLA_E2E_SERVICE=1` to also install the real
systemd unit (root required). The unit test side is `tests/test_easy_setup.py`
— run `pytest tests/test_easy_setup.py` with the venv active; the live-token
pytest case runs only when `TLA_E2E_TOKEN` is set.

---

# 🐳 Docker Support

### Build

```bash
docker build -t tla-bot:latest .
```

### Run

```bash
mkdir -p /path/to/appdata
chown -R 1000:1000 /path/to/appdata   # the image runs as uid 1000 (appuser)
cp config.example.json /path/to/appdata/config.json
# edit /path/to/appdata/config.json and set telegram_token + whitelisted_users

docker run -d \
  --name telegram-admin-bot \
  -v /path/to/appdata:/app/data \
  -e TLA_CONFIG_FILE=/app/data/config.json \
  -e TLA_ACCESS_MODE=whitelist \
  -e TLA_DESTINATION_POLICY=deny_public \
  -e TLA_HEALTH_PORT=8081 \
  -e TLA_AUDIT_DIR=/app/data/var/log \
  -p 127.0.0.1:8081:8081 \
  --restart unless-stopped \
  tla-bot:latest
```

The container installs the package under `/usr/local/lib/python3.12/site-packages`
and runs as an unprivileged `appuser`; `/app/data` is the only writable volume
(config, database, encryption key and audit log all live there).

With `TLA_HEALTH_PORT` set the container reports `healthy` from
`GET /healthz`; without it the probe is a no-op, so existing deployments are
never marked unhealthy by a feature they do not use.

The bind-mounted data directory must be owned by uid 1000: the container runs as
the unprivileged `appuser` and cannot create `database.db`, the audit log or the
encryption key in a root-owned mount. If it cannot, `/readyz` reports `503` with
`audit_writable: false` and the log states exactly which path failed — the bot
refuses to call itself ready while it is silently losing audit records.

---

# 📖 Usage

## Basic Commands

* `/start` - Start the bot and show main menu
* `/language` - Change language preference
* `/dashboard` - Admin dashboard (admin only)
* `/audit [count]` - Tail the audit log (admin only)
* `/access` - Show or change the access mode at runtime (admin only)

## Server Management

1. **Add Server**: Use the menu or `/add_server` command
2. **Connect**: Select a server from the list
3. **Execute Commands**: Run commands directly or use interactive shell
4. **Manage**: Use the server menu for system operations

## Admin Features

* `/dashboard` - View comprehensive statistics
* Backup & Restore - Professional backup system with validation
* Bot Updates - Automatic updates with rollback

---

# 🔐 Security Features

## Post-Quantum Encryption

All server credentials are encrypted using post-quantum cryptography:

- **Quantum-Resistant**: Protection against future quantum computers
- **Hybrid Approach**: Combines post-quantum and classical encryption
- **Backward Compatible**: Existing encrypted data continues to work
- **Automatic**: No user action required
- **Transparent**: Works seamlessly in the background

## Encryption Details

- **Key Derivation**: SHA-3-512 (quantum-resistant hash)
- **Symmetric Cipher**: AES-256-GCM
- **Key Storage**: Secure file with 0600 permissions
- **Version Headers**: Future-proof format for upgrades

---

# 📊 Admin Dashboard

The admin dashboard provides comprehensive insights:

## Statistics Available

- **User Metrics**:
  - Total registered users
  - Active users (with servers)
  - New users today

- **Server Metrics**:
  - Total servers
  - Servers added today
  - Average servers per user
  - Maximum/minimum servers per user

- **Distribution**:
  - Plan distribution (free/premium)
  - Language preferences (top 5)

- **Recent Activity**:
  - Last 5 servers added
  - Owner information
  - Creation timestamps

Access the dashboard with `/dashboard` (admin only).

---

# 🌍 Language Support

The bot intelligently selects the best parse mode for each language:

- **MarkdownV2**: English, French, German, Spanish, Portuguese, Italian, Russian, Turkish
- **HTML**: Arabic, Persian, Urdu (RTL support), Chinese, Japanese, Korean (CJK support), Hindi

All messages are properly escaped and formatted for each language.

---

# 🔄 Backup & Restore

## Backup Features

- **Progress Indication**: Real-time updates during backup
- **File Validation**: Checks existence, size limits, and integrity
- **Checksums**: SHA-256 for all files
- **Metadata**: Stores timestamp, version, file info, and checksums
- **Compression**: Maximum compression (level 9)
- **Integrity Verification**: Validates ZIP file after creation

## Restore Features

- **Progress Updates**: Step-by-step progress indication
- **File Validation**: Extension, size, ZIP integrity, required files, JSON validation
- **Safety Backup**: Automatic backup of current state before restore
- **Rollback**: Automatic restore from safety backup if restore fails
- **Atomic Operations**: Files backed up before replacement
- **Error Handling**: Detailed error messages and automatic recovery

---

# 🛠️ System Requirements

- **Python 3.12+** (2026 standards with modern features)
- Linux/Unix system
- Telegram Bot Token
- SSH access to managed servers
- Optional: `psutil` for system monitoring (install with `pip install telegram-linux-admin[monitoring]`)

---

# 📝 Configuration

`config.json` holds the bot token, the whitelist and the security policy:

```json
{
  "telegram_token": "YOUR_BOT_TOKEN",
  "whitelisted_users": [YOUR_TELEGRAM_USER_ID],
  "access_mode": "whitelist",
  "destination_policy": "deny_public",
  "allowed_destinations": ["10.0.0.0/8", "192.168.0.0/16"]
}
```

Unknown keys are preserved verbatim on save, so hand-written settings are never
clobbered. The file is tightened to mode `600` on load. Environment variables
override the file:

| Variable | Default | Purpose |
| --- | --- | --- |
| `TLA_CONFIG_FILE` | `config.json` | Path to the JSON configuration |
| `TLA_DB_FILE` | `database.db` | SQLite database path |
| `TLA_ENCRYPTION_KEY` | *(unset)* | Fernet key, avoids a key file on disk |
| `TLA_ENCRYPTION_KEY_FILE` | `var/encryption.key` | Key file when the env var is unset |
| `TLA_ACCESS_MODE` | `open` | `open`, `whitelist` or `admin_only` |
| `TLA_DESTINATION_POLICY` | `allow_all` | `allow_all`, `deny_public` or `allowlist` |
| `TLA_ALLOWED_DESTINATIONS` | *(empty)* | Comma-separated CIDRs for `allowlist` |
| `TLA_HEALTH_PORT` | *(unset)* | Enables `/healthz`, `/readyz`, `/metrics` |
| `TLA_HEALTH_HOST` | `127.0.0.1` | Health bind address (use `0.0.0.0` in Docker) |
| `TLA_AUDIT_DIR` | `var/log` | Audit log directory |
| `TLA_AUDIT_MAX_BYTES` | `5242880` | Rotate the audit log at this size |
| `TLA_AUDIT_KEEP_FILES` | `3` | Rotated audit files to keep |
| `TLA_AUDIT_ENABLED` | `1` | Set to `0` to disable JSONL audit records | |

Encryption keys are generated automatically on first run and stored with mode
`600`.

---

# 🛡️ Hardening & Operations

## Access control (fail-closed)

Every handler passes through a single gate backed by `src/access_control.py`:

- `open` — legacy behaviour, any Telegram user may talk to the bot. A loud
  warning is logged at startup; use it only for a first smoke test.
- `whitelist` — only IDs in `whitelisted_users` are served.
- `admin_only` — only the first whitelisted ID (the administrator) is served.

Unknown users get a translated denial, the denial is audited, and the handler
never runs. The mode can be changed at runtime with `/access` (admin only).

## Destination policy

Before a TCP connection is attempted, `src/destination_policy.py` classifies the
target host:

- `allow_all` — historical behaviour.
- `deny_public` — private ranges, loopback and link-local only; public IPs are
  refused. Cloud metadata endpoints (`169.254.169.254`, `fd00:ec2::254`) are
  always refused in every mode.
- `allowlist` — only the CIDRs in `allowed_destinations` (plus an exact
  hostname match) are reachable.

This makes an accidental production-IP or router-IP entry a clear error instead
of a live connection. DNS resolution happens before the decision, so a hostname
cannot be used to smuggle a public address past the policy.

## SSH host-key pinning (TOFU)

The first connection to a server records the SHA-256 host-key fingerprint in the
database (`servers.host_key`). Every later connection is compared against it and
a mismatch aborts **before** a single byte of input is sent — a swapped or
man-in-the-middled host cannot receive the bot's commands or credentials.
Removing and re-adding the server re-pins the key.

## Command risk policy

Commands are classified by `src/command_policy.py` into three bands:

- **SAFE** — executed immediately (`ls`, `df -h`, `systemctl status …`).
- **CONFIRM** — destructive but recoverable (`rm -rf`, `apt upgrade`,
  `systemctl restart`, `userdel`, `docker … prune`). The bot shows an inline
  one-shot confirmation button valid for 3 minutes.
- **BLOCKED** — irreversible or out-of-scope (`rm -rf /`, `mkfs`, `dd of=/dev/…`,
  `shutdown`, fork bombs, `iptables -F`, writing to raw disks). There is no
  confirmation path: these can never be executed from chat.

`sh -c`, `bash -c`, `sudo` and command substitution payloads are inspected
recursively, so wrapping a blocked command does not bypass the classifier.
Commands longer than 4096 UTF-8 bytes are rejected with a clear message.

## Audit trail

Every privileged action (connection, command, config change, denial, host-key
mismatch, admin command) is appended to a JSONL file under `TLA_AUDIT_DIR`.
Records contain the event name, user ID, server alias, outcome and a timestamp —
never tokens, passwords or key material. The file rotates at
`TLA_AUDIT_MAX_BYTES` and keeps three generations. Admins can read the tail from
chat with `/audit [count]` (default 15, max 50).

## Health, readiness and metrics

Set `TLA_HEALTH_PORT` to expose a dependency-free HTTP endpoint:

```bash
curl -s localhost:8081/healthz   # {"status": "ok"}             — liveness
curl -s localhost:8081/readyz    # {"status": "ready"}          — DB + config ok
curl -s localhost:8081/metrics   # Prometheus text exposition
```

It defaults to loopback; bind `0.0.0.0` only inside a container or behind a
private network. `/readyz` returns `503` with the failing component when the
database or configuration is unusable, so a load balancer can drain the pod.

## Connection pooling and retries

SSH connections are pooled per `(user, host)` and reused across actions, which
removes the 150–400 ms handshake from every menu click. Idle links are reaped in
the background. Transient transport failures are retried; authentication
failures, host-key mismatches and cancellations never are.

## Rate limiting

Connection attempts are limited to 6 per 30 seconds per user, remote commands
to 12 per minute and admin commands to 10 per minute; exceeding a budget produces a translated `rate_limited`
message instead of a Telegram flood-ban.

---

# 🔧 Advanced Features (2026 Standards)

## Modern Python Features

- **Python 3.12+ Type System**: Using PEP 604 syntax (`X | Y`), `Self`, `TypedDict`
- **Match/Case Statements**: Modern pattern matching for cleaner code
- **Dataclasses with Slots**: Performance-optimized data structures
- **Structured Logging**: JSON-formatted logs for better observability
- **Modern Async Patterns**: `asyncio.timeout()`, `TaskGroup` support
- **Cached Functions**: Using `@cache` for performance
- **Pathlib Everywhere**: Modern path handling instead of string paths

## Error Handling

- **Graceful Timeout Handling**: Modern `asyncio.timeout()` context managers (30-second timeouts)
- **Network Error Recovery**: Automatic retry with exponential backoff
- **Message Modification Error Handling**: Smart detection and graceful handling
- **Structured Logging**: Context-aware logging with structured data
- **User-Friendly Error Messages**: Localized, clear error messages
- **Exception Group Support**: Modern exception handling patterns

## Performance (2026 Standards)

- **Optimized Database Queries**: Using modern SQLite features (WAL mode, prepared statements)
- **Efficient File Operations**: Pathlib-based file handling, chunked processing
- **Maximum Compression**: Level 9 compression for backups
- **Fast Restore Operations**: Parallel processing where applicable
- **Memory Optimization**: Using `__slots__` in dataclasses, cached functions
- **Async Optimization**: Modern async/await patterns with timeout context managers
- **Type Safety**: Full type hints with PEP 604 syntax (`X | Y` instead of `Union[X, Y]`)

## Security (2026 Standards)

- **Post-Quantum Encryption**: Quantum-resistant cryptography
- **Secure Key Management**: Using `secrets` module for cryptographically secure random generation
- **Per-User Isolation**: Complete sandboxing
- **Admin-Only Features**: Role-based access control
- **Zero Plaintext Storage**: All credentials encrypted
- **Modern Cryptography**: SHA-3-512, AES-256-GCM, secure key derivation
- **Path Security**: Using `pathlib` for safer path operations

---

# ❌ Uninstall

```bash
sudo ./scripts/uninstall.sh
```

---

# 🤝 Contributing

Pull requests, improvements, and contributions are welcome.
Please follow security best practices when submitting features.

## Development

1. Fork the repository
2. Create a feature branch
3. Make your changes
4. Add tests if applicable
5. Submit a pull request

---

# 📜 License

MIT License — fully open source.

---

# 🙏 Acknowledgments

- Built with [python-telegram-bot](https://github.com/python-telegram-bot/python-telegram-bot)
- Uses [asyncssh](https://github.com/ronf/asyncssh) for SSH connections
- Post-quantum cryptography implementation
- Multi-language support with intelligent formatting

---

# 🚀 2026 Standards Compliance

This bot is built with **2026 Python standards** and modern best practices:

## Modern Python Features

- ✅ **Python 3.12+** with latest language features
- ✅ **PEP 604 Type Hints**: `str | None` instead of `Optional[str]`
- ✅ **Match/Case Statements**: Modern pattern matching for cleaner code
- ✅ **Dataclasses with Slots**: Performance-optimized data structures (`@dataclass(slots=True)`)
- ✅ **Structured Logging**: JSON-formatted logs with context for better observability
- ✅ **Modern Async Patterns**: `asyncio.timeout()` context managers, proper cancellation
- ✅ **Pathlib Everywhere**: Modern path handling instead of string paths
- ✅ **Secrets Module**: Cryptographically secure random number generation
- ✅ **Type Safety**: Full type coverage with modern type system
- ✅ **Cached Functions**: Using `@cache` decorator for performance

## Code Quality Standards

- ✅ **100% Type Hints**: Complete type coverage
- ✅ **Comprehensive Error Handling**: Modern exception handling with proper recovery
- ✅ **Performance Optimized**: Caching, slots, efficient algorithms
- ✅ **Security First**: Post-quantum encryption, secure key management
- ✅ **Clean Architecture**: Well-organized, maintainable code
- ✅ **Modern Patterns**: Using latest Python idioms and best practices

## Dependencies & Tooling

- ✅ **Latest Versions**: All dependencies updated to 2026 standards
- ✅ **Optional Extras**: Monitoring (`psutil`), development tools
- ✅ **Security Updates**: Regular dependency security updates
- ✅ **Modern Build System**: `pyproject.toml` with proper metadata

## Performance Improvements

- **Memory Efficiency**: Using `__slots__` in dataclasses reduces memory footprint
- **Caching**: Strategic use of `@cache` for expensive operations
- **Async Optimization**: Modern async patterns with proper timeout handling
- **Database Optimization**: WAL mode, prepared statements, efficient queries
- **File Operations**: Chunked processing, pathlib for better performance

---

# 📞 Support

For issues, questions, or contributions:
- Open an issue on GitHub
- Submit a pull request
- Check the documentation

---

**⭐ Star this repo if you find it useful!**
