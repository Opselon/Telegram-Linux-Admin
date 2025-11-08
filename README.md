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

-   **Multi-Server Management:** Connect to and manage multiple servers from a single bot.
-   **Real-Time Terminal:** Execute commands and see the output streamed back to you in real-time.
-   **Secure:** Uses SSH key-based authentication and restricts bot access to a whitelist of user IDs.
-   **Self-Updating:** Can automatically update itself from a Git repository.
-   **Easy Installation:** A simple setup wizard guides you through the configuration process.
-   **Database Persistent:** Server and user configurations are stored in a local SQLite database.
-   **Backup & Restore:** Easily backup and restore your bot's configuration via Telegram.

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
