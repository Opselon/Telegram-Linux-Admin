# Security Policy

This document outlines the security posture, threat model, and operational responsibilities for the Telegram Linux Admin Bot.

## Threat Model

The security of this bot is designed to protect the confidentiality and integrity of the server credentials it manages, and to keep a compromised or careless chat session from damaging the managed hosts.

Three threats are addressed:

1. **An attacker gaining read-only access to the bot's database file on disk** — mitigated by authenticated encryption of every secret.
2. **An unauthorised Telegram user reaching the bot** — mitigated by fail-closed access control (`open` / `whitelist` / `admin_only`).
3. **A legitimate user (or a typo) issuing a destructive command** — mitigated by destination policy, SSH host-key pinning and a three-band command policy with one-shot confirmations.

### Authorisation

`src/access_control.py` is the single gate for every handler. `open` restores the historical "any user" behaviour and logs a startup warning; `whitelist` and `admin_only` deny everyone else before the handler runs. Denials are audited with the user ID and handler name.

### The self-host target

The administrator (the first whitelisted user — the installer) can manage the host running the bot. This is the most privileged surface in the product, so it is guarded in three independent places:

1. `TargetAccessPolicy.ensure_can_use` — admin-only, and disabled entirely when `local_target_enabled` is false.
2. `TargetCatalog.find` — the reserved alias `self-host` is resolved from the principal, never from persistence; an SSH server may not claim it.
3. `RoutingCommandManager` — re-checks the policy before executing, so no legacy handler can bypass the catalog.

Local commands run in a new session per command, with a timeout, an output cap and process-group termination. The interactive local shell uses a per-command random marker and is torn down after a timeout, so output from an abandoned command cannot be attributed to the next one. Everything is audited (`connect.local`, `connect.local_failed`, `connect.denied`).

Deployments that must never expose the bot's own host should set `"local_target_enabled": false` in `config.json` (or `TLA_LOCAL_TARGET_ENABLED=0`). Note that the bot process' own privileges become the ceiling of what the administrator can do through this target — run the bot as an unprivileged service user if that matters to you.

### Reachability

`src/destination_policy.py` decides whether a target may be contacted at all (`allow_all`, `deny_public`, `allowlist`). Cloud metadata endpoints (`169.254.169.254`, `fd00:ec2::254`), loopback and link-local addresses are refused in every mode. Resolution happens before the decision, so DNS cannot be used to bypass it.

### Host authenticity

The first connection pins the server's SHA-256 host-key fingerprint in `servers.host_key` (trust on first use). Later connections abort on mismatch before any input is written, so a MITM cannot capture commands or credentials.

### Command safety

`src/command_policy.py` classifies every command as SAFE (run), CONFIRM (one-shot inline approval, 3-minute TTL) or BLOCKED (no execution path at all — formatting filesystems, wiping disks, power state changes, fork bombs, firewall flushes, writes to raw block devices). Nested `sh -c` / `bash -c` / `sudo` payloads and command substitutions are inspected recursively.

### Audit

All privileged actions append a JSONL record (event, user, alias, outcome, timestamp) to a size-rotated file under `TLA_AUDIT_DIR`. No tokens, passwords or key material are ever written. `/audit` exposes the tail to the administrator.

### What Is Protected

-   **Server Credentials at Rest**: If an attacker steals the `database.db` file, they will not be able to access the server passwords or the contents of SSH key files stored within it. This data is encrypted using a strong, authenticated encryption scheme.

### What Is NOT Protected

The security model assumes that the **host environment where the bot is running is secure**. It does **not** protect against an attacker who has compromised the host and can:

-   Read the bot's environment variables.
-   Read files owned by the user running the bot process (including the encryption key file).
-   Inspect the memory of the running bot process.
-   Gain shell access as the user running the bot.

In such a scenario, an attacker could extract the encryption key and decrypt the database. Therefore, securing the host server is the most critical aspect of securing the bot.

## Cryptography

-   **Algorithm**: The bot uses the **Fernet** symmetric encryption scheme from the `cryptography` library. This is a high-level, secure implementation of AES-128-CBC with a an HMAC-SHA256 signature for authentication.
-   **Justification**: While more modern AEAD schemes like AES-GCM exist, Fernet was chosen because it is significantly simpler to implement correctly. It automatically handles complexities like initialization vectors (IVs), which prevents common cryptographic mistakes. Given the threat model, Fernet provides more than sufficient security and is the safer, more maintainable choice for this project.

## Key Management

### Storage

The encryption key(s) are stored in a versioned JSON file, typically located at `var/encryption.key`. On first run, a new key file is generated automatically.

It is **highly recommended** for production deployments (especially in Docker) to provide the key via the `TLA_ENCRYPTION_KEY` environment variable. If this variable is set, the key file on disk will not be used.

### Key Rotation

Key rotation is a manual process that should be performed periodically to enhance security. A script is provided to automate this process safely.

**To rotate the key:**

1.  **Back up your data**: Before proceeding, make a backup of your `database.db` file and your `var/encryption.key` file.
2.  **Run the script**: Execute the following command from the root of the project:
    ```bash
    python3 scripts/rotate_key.py
    ```
3.  The script will guide you through the process. It will:
    -   Generate a new key and add it to your key file.
    -   Set the new key as the "primary" key for all new encryption.
    -   Read all existing server credentials from the database, decrypt them with the old key(s), and re-encrypt them with the new primary key.

This process ensures that you can rotate your key without invalidating any of your existing server credentials.

## Operator Responsibilities

-   **Secure the Host**: The security of the bot depends entirely on the security of the server it runs on. Ensure the host is hardened, and the user account running the bot is not exposed.
-   **Protect the Key**: The encryption key is the most critical secret.
    -   In production, use the `TLA_ENCRYPTION_KEY` environment variable instead of a key file.
    -   If using a key file, ensure its permissions are set to `600` so that only the bot's user can read it.
-   **Backups**: Regularly back up both the `database.db` file and the `var/encryption.key` file. **If you lose the key file, all encrypted data in your database will be permanently unrecoverable.**
-   **Regularly Rotate Keys**: Use the `scripts/rotate_key.py` script to rotate your encryption key on a regular schedule (e.g., every 3-6 months).
