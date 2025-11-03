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

---

## Key Features

-   🚀 **Full Interactive Root Shell**: Start a persistent terminal session with `/shell`. It remembers your current directory (`cd`), environment variables, and more.
-   ⚡ **Convenient Shortcut Commands**: Instantly check server status, apply updates, and manage services without needing a full shell session.
-   🤖 **Intelligent Asynchronous Execution**: Long-running commands (`apt upgrade`, `git clone`) execute in the background, notifying you upon completion without blocking other operations.
-   🛠️ **Automated Server Maintenance**: Keep your server secure with optional, fully automated weekly updates and cleanups.
-   💡 **One-Minute Setup Wizard**: An interactive, user-friendly setup process that configures the script and all necessary cron jobs for you.
-   🔒 **Security First Design**: The script is hardcoded to respond only to your unique Telegram Chat ID, acting as a primary layer of security.

---

## 🚀 One-Command Installation

Getting started is as simple as running a single line in your terminal. This command will download the `linux_secure_manager.sh` script, place it in `/usr/local/bin`, make it executable, and launch the interactive setup wizard.

```bash
sudo bash -c "curl -o /usr/local/bin/linux_secure_manager.sh https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/linux_secure_manager.sh && chmod +x /usr/local/bin/linux_secure_manager.sh && /usr/local/bin/linux_secure_manager.sh --setup"
