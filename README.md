# Telegram Linux Admin (Python Edition)

A powerful, asynchronous Python application that transforms your private Telegram chat into a full-featured, multi-server command center for your Linux servers.

---

## 🚨🚨🚨 **EXTREME SECURITY WARNING** 🚨🚨🚨
>
> This application provides **full, un-sandboxed, root-level shell access** to your server. Anyone who gains access to your Telegram account or your bot's token can execute **ANY command** on your server, including reading sensitive files, installing malware, or destroying all data (`rm -rf /`).
>
> ### **Non-Negotiable Security Practices:**
>
> 1.  **SECURE YOUR TELEGRAM ACCOUNT**: You **MUST** enable **Two-Step Verification (2FA)** on your Telegram account. Your Telegram account is now a key to your server's root access.
> 2.  **PROTECT YOUR BOT TOKEN**: Treat your Telegram Bot Token as your private root SSH key. Do not share it, commit it to public repositories, or store it insecurely.
>
> **USE THIS SCRIPT AT YOUR OWN IMMENSE RISK.** The author is not responsible for any damage, data loss, or security breaches resulting from its use.

---

## Features

This bot provides a comprehensive suite of tools for remote server administration, all accessible from a secure Telegram chat.

### Core Functionality

-   **Multi-Server Management:** Securely connect to and manage multiple Linux servers. Switch between servers instantly with a simple inline keyboard.
-   **Interactive Shell:** Open a persistent, real-time SSH shell for any server. Execute commands and see live output, just like a native terminal.
-   **Secure by Design:**
    -   Uses robust SSH key-based authentication.
    -   Restricts all bot access to a pre-approved whitelist of Telegram user IDs.
    -   Features an Admin Role for the primary user, gating access to sensitive operations.
-   **Self-Updating Mechanism:** Keep the bot current with a built-in, one-click updater that fetches the latest version from GitHub, backs up your data, and seamlessly restarts the service.
-   **Automated Installation:** A user-friendly `install.sh` script handles everything: virtual environment creation, dependency installation, and interactive setup.
-   **Persistent Configuration:** Server details and user whitelists are stored in a local SQLite database, ensuring your data persists across restarts.
-   **Backup & Restore:** Easily create and restore full backups of your bot's configuration (`config.json` and `database.db`) directly from the Telegram interface.

### Remote Management Modules

-   **System Commands:**
    -   Safely **reboot** or **shutdown** your server (with confirmation).
    -   Check disk usage with `df -h`.
    -   View network interface information with `ip a`.
-   **Service Management:**
    -   Check the status of any `systemd` service.
    -   **Start**, **stop**, or **restart** services directly from the bot.
-   **Process Management:**
    -   List all running processes using `ps aux`.
    -   **Kill** any process by providing its PID.
-   **Firewall Management (UFW):**
    -   View all active `ufw` firewall rules.
    -   **Allow** or **deny** traffic on specific ports.
    -   **Delete** existing firewall rules.
-   **Docker Management:**
    -   List all running and stopped Docker containers.
    -   View the logs of any container.
    -   **Start** and **stop** containers.
-   **Package Management (APT):**
    -   Run `apt update` or `apt upgrade`.
    -   Install new packages with `apt install`.
-   **File Manager:**
    -   List files and directories.
    -   **Download** files directly from your server to your Telegram chat.
    -   **Upload** files from your chat to a specified path on your server.

---

## Installation

1.  **Clone the repository:**
    ```bash
      git clone https://github.com/Opselon/Telegram-Linux-Admin.git && cd Telegram-Linux-Admin && chmod +x install.sh && sudo ./install.sh
    ```

2.  **Run the installation script:**
    ```bash
    bash install.sh
    ```
    The script will create a Python virtual environment, install the necessary dependencies, and launch an interactive setup wizard to guide you through the rest of the configuration.

---

## Docker

For a containerized deployment, you can use the provided `Dockerfile`.

### 1. Build the Image

From the project root, run the following command:

```bash
docker build -t tla-bot:latest .
```

### 2. Run the Container

When running the container, you must mount a local directory to the `/app/data` volume inside the container. This ensures that your `config.json` and `database.db` files are persisted across container restarts.

```bash
# Create a local directory for your data
mkdir -p /path/to/your/appdata

# Run the container
docker run -d \
  --name telegram-admin-bot \
  -v /path/to/your/appdata:/app/data \
  --restart unless-stopped \
  tla-bot:latest
```

The first time you run the container, it will exit immediately. You will need to copy the `config.json` file into your data directory and run the setup wizard to configure the bot.

---

## Uninstallation

To completely remove the bot and all its data, run the uninstaller script. This will stop the service, remove all application files, and delete the systemd and cron configurations.

```bash
sudo ./scripts/uninstall.sh
```

---

## Contributing & License

Contributions, issues, and feature requests are welcome! See the LICENSE file for details.
