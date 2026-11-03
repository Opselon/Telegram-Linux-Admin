# Deployment — Platform 4.0

## What ships

`docker build .` produces a single image that runs the **full platform**:
FastAPI web backend + SPA (port 8080), Bale bot (when `BALE_BOT_TOKEN` is
set), Telegram bot (when `TLA_BOT_TOKEN` is set), plus the legacy health
endpoint (when `TLA_HEALTH_PORT` is set).

## Security defaults

| Setting | Default | Notes |
|---|---|---|
| `TLA_WEB_HOST` | `127.0.0.1` (host installs) | The Dockerfile overrides to `0.0.0.0` because the container isolates the process; **expose the port only via a TLS reverse proxy** (compose below binds to loopback). |
| `TLA_WEB_PORT` | `8080` | |
| Session cookies | `HttpOnly; SameSite=Strict` | `Secure` is set automatically when `TLA_WEB_HOST` is not loopback, or force with `TLA_WEB_SECURE_COOKIES=1` (required behind TLS proxy — set it). |
| First web login | one-time setup | `GET /api/v1/auth/setup` reports whether bootstrap is needed; create the OWNER with `TLA_SETUP_TOKEN` set (env) and `POST /api/v1/auth/setup {username, password, setup_token}`. Remove the env var afterwards. |
| `/metrics`, `/healthz` | health server, `TLA_HEALTH_HOST` | Keep bound to loopback unless you proxy them. |

**Never expose port 8080 directly to the internet without TLS.**

## Compose (production shape)

```yaml
services:
  platform:
    image: ghcr.io/opselon/telegram-linux-admin:latest
    restart: unless-stopped
    env_file: .env
    environment:
      TLA_CONFIG_FILE: /app/data/config.json
      TLA_DB_FILE: /app/data/database.db
      TLA_WEB_SECURE_COOKIES: "1"        # behind TLS proxy
      TLA_PUBLIC_URL: https://admin.example.com   # Bale "Open App" button
    volumes:
      - ./data:/app/data
    ports:
      - "127.0.0.1:8080:8080"            # loopback only; nginx terminates TLS
    stop_grace_period: 15s
```

## Nginx reverse proxy (TLS)

See `deploy/nginx.conf.example`. Key points: proxy `8080` over HTTPS,
`proxy_set_header X-Forwarded-For`, no buffering for the SSE endpoint
(`/api/v1/events/stream`), WebSocket upgrade headers are not required (SSE
only), `client_max_body_size 64m` for file uploads.

## Upgrade / rollback

1. Backups live in `data/var/backups/` (or `TLA_BACKUP_DIR`). Migrations take
   an automatic pre-migration snapshot at `database.db.pre-migration.bak`.
2. Upgrade = pull new image, `docker compose up -d`. Migrations run at boot
   and are additive; existing server records, encryption keys and Telegram
   configuration are preserved.
3. Rollback = stop, restore `database.db.pre-migration.bak` over
   `database.db` (remove `-wal`/`-shm` sidecars), start the previous image tag.

## Systemd (host installs)

`install.sh` continues to install the Telegram bot service. For the full
platform on a host, add e.g.
`ExecStart=/opt/tla/venv/bin/tla-platform` with `Environment=TLA_WEB_HOST=127.0.0.1`
and hardening directives (`NoNewPrivileges=true`, `ProtectSystem=full`,
`ProtectHome=true`, `PrivateTmp=true`). Web stays on loopback behind nginx.
