
# Telegram Linux Admin

<div align="center">

<img src="https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/assets/logo.png" alt="Telegram Linux Admin Logo" width="150"/>

### The Ultimate Interactive Linux Terminal in Your Pocket

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-blue.svg)](https://www.linux.org/)
[![Made with Bash](https://img.shields.io/badge/Made%20with-Bash-1f425f.svg)](https://www.gnu.org/software/bash/)

A single, powerful Bash script that transforms your private Telegram chat into a full-featured, persistent, and secure command center for your Linux servers.

</div>


---

## Enterprise-Grade Features

-   🚀 **Full Interactive Root Shell**: A persistent terminal session (`/shell`) that remembers your current directory (`cd`), environment variables, and more.
-   ⚡ **Convenient Shortcut Commands**: Instantly check server status, apply updates, manage services, and check network info without needing a full shell session.
-   📂 **Complete File Management**: Upload files from your server to Telegram (`/upload`) and download files from Telegram to your server by replying with `/download`.
-   🔄 **Self-Updating**: The script can update itself to the latest version from this repository with a single `/selfupdate` command.
-   🤖 **Intelligent Asynchronous Execution**: Long-running commands (`apt upgrade`, `git clone`) execute in the background, notifying you upon completion without blocking other operations.
-   🛠️ **Automated Server Maintenance**: Keep your server secure with optional, fully automated weekly updates and cleanups.
-   💡 **One-Minute Setup Wizard**: An interactive, user-friendly setup process that configures the script and all necessary cron jobs for you.
-   🔒 **Security First Design**: The script is hardcoded to respond only to your unique Telegram Chat ID, acting as a primary layer of security.

---

## 🚀 One-Command Installation

Getting started is as simple as running a single line in your terminal. This command will download the `linux_secure_manager.sh` script, place it in `/usr/local/bin`, make it executable, and launch the interactive setup wizard.

```bash
sudo bash -c "curl -o /usr/local/bin/linux_secure_manager.sh https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/linux_secure_manager.sh && chmod +x /usr/local/bin/linux_secure_manager.sh && /usr/local/bin/linux_secure_manager.sh --setup"
```
The wizard will guide you through getting your Telegram credentials and automatically configure all required cron jobs.

### Prerequisites

All you need is a standard Debian-based Linux server (like Ubuntu) with the following packages:
- `curl`
- `jq`
- If not installed, run: `sudo apt-get update && sudo apt-get install -y curl jq`

---

> ## 🚨🚨🚨 **EXTREME SECURITY WARNING** 🚨🚨🚨
>
> This script provides **full, persistent, un-sandboxed, root-level shell access** to your server. Anyone who gains access to your Telegram account or your bot's token can execute **ANY command** on your server, including reading sensitive files, installing malware, or destroying all data (`rm -rf /`).
>
> ### **Non-Negotiable Security Practices:**
>
> 1.  **SECURE YOUR TELEGRAM ACCOUNT**: You **MUST** enable **Two-Step Verification (2FA)** on your Telegram account. Your Telegram account is now a key to your server's root access.
> 2.  **PROTECT YOUR BOT TOKEN**: Treat your Telegram Bot Token as your private root SSH key. Do not share it, commit it to public repositories, or store it insecurely.
>
> **USE THIS SCRIPT AT YOUR OWN IMMENSE RISK.** The author is not responsible for any damage, data loss, or security breaches resulting from its use.

---

## 🛠️ Management & Diagnostics

After installation, run the script without any flags to open an interactive management menu: `sudo linux_secure_manager.sh`

This menu provides helpful tools for diagnostics and maintenance, including a **self-update** option.

## Command Reference

Type `/start` or `/help` in your bot chat for a full, categorized command list.

| Command | Description | Example |
| :--- | :--- | :--- |
| **System & Updates** | | |
| `/status` | Get a detailed system overview. | `/status` |
| `/checkupdates` | See available package updates. | `/checkupdates` |
| `/runupdates` | Install all updates in the background. | `/runupdates` |
| `/netinfo` | View listening ports & IP addresses. | `/netinfo` |
| **File Management** | | |
| `/upload <path>` | Upload a file from the server to Telegram. | `/upload /var/log/syslog` |
| `/download` | Reply to a file in chat to download it. | Reply to a file with `/download` |
| **Interactive Shell** | | |
| `/shell` | Start a persistent, stateful root shell. | `/shell` |
| `/exit` | Terminate the current shell session. | `/exit` |
| **System Control & Script** | | |
| `/service status <name>` | Get the `systemd` status of a service. | `/service status nginx` |
| `/service restart <name>`| ⚠️ Restart a service. | `/service restart nginx` |
| `/reboot` | ⚠️ Reboot the entire server. | `/reboot` |
| `/shutdown` | ⚠️ Shutdown the entire server. | `/shutdown` |
| `/selfupdate` | Update this script to the latest version. | `/selfupdate` |


---

## 🗑️ Uninstallation

To completely remove the script, its cron jobs, logs, and all related files, run the following command:

```bash
curl -sSL https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/uninstall.sh | sudo bash
```
The uninstaller will ask for final confirmation before proceeding.

---

## Contributing & License

Contributions, issues, and feature requests are welcome! See the [LICENSE](https://github.com/Opselon/Telegram-Linux-Admin/blob/main/LICENSE) file for details.
``````
