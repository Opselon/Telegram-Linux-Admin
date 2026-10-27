#!/usr/bin/env bash
# scripts/remote-deploy.sh — Turnkey Continuous Deployment runner for Telegram Linux Admin
#
# Designed to run remotely via GitHub Actions (appleboy/ssh-action) or directly
# on any Linux host. Ingests GitHub Secrets passed as environment variables,
# writes/syncs config.json and .env securely, pulls the latest GHCR image,
# cleanly tears down any previous container and systemd instance, starts the
# fresh Docker stack, and verifies container health.
#
set -euo pipefail

DEPLOY_DIR="${DEPLOY_DIR:-/root/telegram-admin-bot}"
COMPOSE_PROJECT="telegram-admin-bot"
IMAGE_TAG="${IMAGE_TAG:-latest}"
IMAGE_REPO="${IMAGE_REPO:-ghcr.io/opselon/telegram-linux-admin}"
FULL_IMAGE="${IMAGE_REPO}:${IMAGE_TAG}"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-60}"
HEALTH_PORT="${TLA_HEALTH_PORT:-8082}"

echo "============================================================"
echo "  Telegram Linux Admin — Continuous Deployment Runner"
echo "  Target directory: $DEPLOY_DIR"
echo "  Target image:     $FULL_IMAGE"
echo "  Health port:      $HEALTH_PORT"
echo "============================================================"

# 1. Prepare deploy directory and data volume directory
mkdir -p "$DEPLOY_DIR/data" "$DEPLOY_DIR/data/var/log"
cd "$DEPLOY_DIR"

# 2. Write docker-compose.yml if not already present
if [ ! -f "$DEPLOY_DIR/docker-compose.yml" ]; then
  echo "== creating default docker-compose.yml at $DEPLOY_DIR =="
  cat > "$DEPLOY_DIR/docker-compose.yml" << 'COMPOSE_EOF'
name: telegram-admin-bot

services:
  bot:
    image: ${BOT_IMAGE:-ghcr.io/opselon/telegram-linux-admin:latest}
    container_name: telegram-admin-bot
    restart: unless-stopped
    env_file: .env
    environment:
      TLA_CONFIG_FILE: /app/data/config.json
      TLA_DB_FILE: /app/data/database.db
      TLA_AUDIT_DIR: /app/data/var/log
      TLA_HEALTH_PORT: "${HEALTH_PORT:-8082}"
      TLA_HEALTH_HOST: "0.0.0.0"
      TLA_ACCESS_MODE: "${TLA_ACCESS_MODE:-whitelist}"
      TLA_DESTINATION_POLICY: "${TLA_DESTINATION_POLICY:-deny_public}"
    volumes:
      - ./data:/app/data
      - /var/run/docker.sock:/var/run/docker.sock:ro
    ports:
      - "127.0.0.1:${HEALTH_PORT:-8082}:${HEALTH_PORT:-8082}"
    stop_grace_period: 15s
    healthcheck:
      test: ["CMD", "python", "-c", "import os, urllib.request; p = os.environ.get('TLA_HEALTH_PORT', '8082'); urllib.request.urlopen('http://127.0.0.1:' + p + '/healthz', timeout=4)"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"
COMPOSE_EOF
fi

# 3. Synchronize environment variables into .env securely
ENV_FILE="$DEPLOY_DIR/.env"
TMP_ENV="$(mktemp "$DEPLOY_DIR/.env.tmp.XXXXXX")"
trap 'rm -f "$TMP_ENV"' EXIT

if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$TMP_ENV"
else
  touch "$TMP_ENV"
fi

upsert_env_key() {
  local key="$1"
  local val="$2"
  if grep -q "^${key}=" "$TMP_ENV"; then
    python3 -c "
import sys
key = sys.argv[1]
val = sys.argv[2]
path = sys.argv[3]
with open(path, 'r') as f:
    lines = f.readlines()
with open(path, 'w') as f:
    for line in lines:
        if line.startswith(key + '='):
            f.write(f'{key}={val}\n')
        else:
            f.write(line)
" "$key" "$val" "$TMP_ENV"
  else
    echo "${key}=${val}" >> "$TMP_ENV"
  fi
}

# Ingest secrets passed from environment
[ -n "${TELEGRAM_BOT_TOKEN:-}" ] && upsert_env_key "TELEGRAM_BOT_TOKEN" "$TELEGRAM_BOT_TOKEN"
[ -n "${ADMIN_USER_ID:-}" ] && upsert_env_key "ADMIN_USER_ID" "$ADMIN_USER_ID"
[ -n "${TLA_ACCESS_MODE:-}" ] && upsert_env_key "TLA_ACCESS_MODE" "$TLA_ACCESS_MODE"
[ -n "${TLA_DESTINATION_POLICY:-}" ] && upsert_env_key "TLA_DESTINATION_POLICY" "$TLA_DESTINATION_POLICY"
[ -n "${HEALTH_PORT:-}" ] && upsert_env_key "HEALTH_PORT" "$HEALTH_PORT"
upsert_env_key "BOT_IMAGE" "$FULL_IMAGE"

mv "$TMP_ENV" "$ENV_FILE"
chmod 0600 "$ENV_FILE"
echo "== synced environment variables to .env (chmod 0600) =="

# 4. Generate/sync config.json inside data volume
DATA_CONFIG="$DEPLOY_DIR/data/config.json"
python3 - <<PY
import json, os

config_path = "$DATA_CONFIG"
data = {}
if os.path.exists(config_path):
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {}

token = os.environ.get("TELEGRAM_BOT_TOKEN") or data.get("telegram_token", "")
admin_id = os.environ.get("ADMIN_USER_ID", "")
users = []
if admin_id:
    for u in admin_id.split(","):
        u = u.strip()
        if u.isdigit():
            users.append(int(u))
elif "whitelisted_users" in data:
    users = data["whitelisted_users"]

access_mode = os.environ.get("TLA_ACCESS_MODE") or data.get("access_mode", "whitelist")
dest_policy = os.environ.get("TLA_DESTINATION_POLICY") or data.get("destination_policy", "deny_public")
health_port = int(os.environ.get("HEALTH_PORT") or data.get("health_port", 8082))

payload = {
    "telegram_token": token,
    "whitelisted_users": users,
    "access_mode": access_mode,
    "destination_policy": dest_policy,
    "allowed_destinations": data.get("allowed_destinations", []),
    "health_port": health_port
}

with open(config_path, "w", encoding="utf-8") as f:
    json.dump(payload, f, indent=2)

os.chmod(config_path, 0o600)
print(f"== config.json synced at {config_path} with whitelist users {users} ==")
PY

# Ensure data directory permissions are accessible by appuser (UID 1000)
chown -R 1000:1000 "$DEPLOY_DIR/data" 2>/dev/null || true

# 5. Stop host-level systemd service if active to avoid getUpdates polling conflict
if systemctl is-active --quiet telegram_bot.service 2>/dev/null; then
  echo "== stopping host-level telegram_bot.service to prevent polling conflict =="
  systemctl stop telegram_bot.service 2>/dev/null || true
  systemctl disable telegram_bot.service 2>/dev/null || true
fi

# 6. Pull target Docker image
echo "== pulling fresh Docker image '$FULL_IMAGE' =="
if ! docker pull "$FULL_IMAGE"; then
  if docker image inspect "$FULL_IMAGE" >/dev/null 2>&1; then
    echo "warn: docker pull failed but local image '$FULL_IMAGE' exists; proceeding with local image."
  else
    echo "error: docker pull failed and no local image '$FULL_IMAGE' found." >&2
    exit 1
  fi
fi

# 7. Clean container teardown ("همیشه داکر قبلی رو بخوابونه جدید و تمیز بیاد")
echo "== cleanly stopping and removing previous container =="
docker stop -t 15 telegram-admin-bot 2>/dev/null || true
docker rm -f telegram-admin-bot 2>/dev/null || true

# 8. Start fresh container
echo "== launching clean container =="
docker compose -p "$COMPOSE_PROJECT" --env-file "$ENV_FILE" up -d --remove-orphans

# 9. Verify container health
echo "== verifying container health (timeout: ${HEALTH_TIMEOUT}s) =="
deadline=$((SECONDS + HEALTH_TIMEOUT))
healthy=0

while [ "$SECONDS" -lt "$deadline" ]; do
  status=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' telegram-admin-bot 2>/dev/null || echo missing)
  if [ "$status" = "healthy" ]; then
    echo "== container is healthy! =="
    healthy=1
    break
  fi
  if curl -fsS "http://127.0.0.1:${HEALTH_PORT}/healthz" >/dev/null 2>&1; then
    echo "== healthz endpoint responded ok! =="
    healthy=1
    break
  fi
  sleep 3
done

if [ "$healthy" -ne 1 ]; then
  echo "error: container failed to report healthy within ${HEALTH_TIMEOUT}s (status: $status)" >&2
  docker logs --tail=30 telegram-admin-bot >&2 || true
  exit 1
fi

# 10. Prune dangling Docker images
echo "== pruning dangling docker images =="
docker image prune -f || true

echo "============================================================"
echo "  Continuous Deployment Completed Successfully!"
echo "  Active Container: telegram-admin-bot"
echo "  Image: $FULL_IMAGE"
echo "============================================================"
docker compose -p "$COMPOSE_PROJECT" ps
docker logs --tail=10 telegram-admin-bot
