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

### 1. Clone repo

```bash
git clone https://github.com/Opselon/Telegram-Linux-Admin.git \
 && cd Telegram-Linux-Admin \
 && chmod +x install.sh \
 && sudo ./install.sh
```

### 2. Run setup

```bash
bash install.sh
```

The setup wizard will guide you through:
- Telegram bot token configuration
- Admin user setup
- Systemd service installation (optional)
- Automatic updates configuration (optional)

---

# 🐳 Docker Support

### Build

```bash
docker build -t tla-bot:latest .
```

### Run

```bash
mkdir -p /path/to/appdata

docker run -d \
  --name telegram-admin-bot \
  -v /path/to/appdata:/app/data \
  --restart unless-stopped \
  tla-bot:latest
```

---

# 📖 Usage

## Basic Commands

* `/start` - Start the bot and show main menu
* `/language` - Change language preference
* `/dashboard` - Admin dashboard (admin only)

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

Configuration is stored in `config.json`:

```json
{
  "telegram_token": "YOUR_BOT_TOKEN",
  "whitelisted_users": [YOUR_TELEGRAM_USER_ID]
}
```

Encryption keys are automatically generated and stored securely.

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
