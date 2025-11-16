<p align="center">

  <!-- Core Badges -->

  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?style=for-the-badge">
  <img src="https://img.shields.io/badge/Asyncio-Enabled-4B8BBE?style=for-the-badge">
  <img src="https://img.shields.io/badge/SSH-Secure-green?style=for-the-badge">
  <img src="https://img.shields.io/badge/Encryption-Enabled-orange?style=for-the-badge">

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

  <img src="https://img.shields.io/github/license/Opselon/Telegram-Linux-Admin?style=for-the-badge">

  <!-- Docker -->

  <img src="https://img.shields.io/badge/Docker-Ready-0db7ed?style=for-the-badge">

  <!-- Multi Language -->

  <img src="https://img.shields.io/badge/Multi--Language-15+_Languages-purple?style=for-the-badge">

</p>


# 🚀 Telegram Linux Admin (Python Edition)

A modern, encrypted, multi-user, multi-language **Telegram SSH management bot** that transforms your Telegram chat into a secure command center for all your Linux servers.

This project is designed for professionals, sysadmins, DevOps engineers, and server owners who want **secure remote administration** with **zero learning curve** — directly inside Telegram.
---

# 🔐 Extreme Security — But Easy to Use

This bot is built with **professional security standards**:

### ✔️ Full encryption for all secrets

* SSH passwords
* SSH private key paths
* Server configuration
  All encrypted using **Fernet 256-bit symmetric crypto** with a restricted (`0600`) key file.

### ✔️ Per-user isolation

Each Telegram user only sees **their own servers**.
No one can access another user's machines.

### ✔️ Zero plaintext storage

No credentials are ever stored unencrypted.

### ✔️ Hidden admin-only commands

Only the installer/admin user can run:

* Bot update
* Maintenance commands
* Config-level actions

Other users **never see admin options**.

### ✔️ Easy for anyone worldwide

The bot now supports **global usage** — every user can manage their own servers safely and independently.

---

# 📈 SEO-Optimized Feature Overview

This section is optimized for Google ranking on target keywords:

**telegram linux admin bot**, **telegram ssh bot**,
**secure telegram ssh manager**, **linux server telegram bot**,
**remote linux management bot**, **telegram devops tools**,
**telegram server admin**, **telegram ssh terminal bot**

---

# ⚙️ Features

## 🔧 Server Management

* Add unlimited Linux servers
* Password or SSH-key authentication
* Persistent shell sessions
* Multi-language prompts & menus
* Per-user server list isolation

## 🔒 Security Layer

* Encrypted database secrets
* Auto-generated encryption key
* Per-user sandboxed environments
* Hidden admin controls
* No plaintext secrets stored anywhere

## 🛠 System Controls

* Reboot / shutdown
* System info (CPU, RAM, Disk, Network)
* Process manager with kill/inspect
* Service manager (`systemd`)
* Package manager (apt)
* Docker control
* Firewall management (UFW)
* File upload/download

## 📦 Persistence & Backup

* Encrypted SQLite database
* Backup & restore tools
* Self-updating mechanism
* Auto-updater with rollback

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

# ❌ Uninstall

```bash
sudo ./scripts/uninstall.sh
```

---

# 🤝 Contributing

Pull requests, improvements, and contributions are welcome.
Please follow security best practices when submitting features.

---

# 📜 License

MIT License — fully open source.

---
