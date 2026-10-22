#!/usr/bin/env bash
#
# Telegram Linux Admin — one-command installer.
#
# Easy (non-interactive, secure defaults, systemd + cron + health probe):
#   ./install.sh --token 123456:ABC-DEF --admin-id 123456789
#
# From anywhere, without a checkout (this is the whole install):
#   curl -fsSL https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/install.sh \
#     | sudo bash -s -- --token 123456:ABC-DEF --admin-id 123456789
#
# Pro / interactive (guided wizard, unchanged behaviour):
#   ./install.sh
#
# Everything is idempotent: re-running upgrades the checkout in place and
# rewrites config.json, the unit and the cron job.
set -euo pipefail

REPO_URL="${TLA_REPO_URL:-https://github.com/Opselon/Telegram-Linux-Admin.git}"
REPO_SLUG="${TLA_REPO_SLUG:-Opselon/Telegram-Linux-Admin}"
BRANCH="${TLA_BRANCH:-main}"
MIN_PY_MINOR=12

if [ -t 1 ]; then
    C_BLUE=$'\033[34m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
    C_RED=$'\033[31m'; C_BOLD=$'\033[1m'; C_RESET=$'\033[0m'
else
    C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_BOLD=""; C_RESET=""
fi

step()  { printf '\n%s==>%s %s\n' "$C_BOLD$C_BLUE" "$C_RESET" "$*"; }
ok()    { printf '%s  ok%s %s\n' "$C_GREEN" "$C_RESET" "$*"; }
warn()  { printf '%s  !!%s %s\n' "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()   { printf '%s error:%s %s\n' "$C_RED" "$C_RESET" "$*" >&2; exit 1; }

usage() {
    cat <<'USAGE'
Telegram Linux Admin installer

Usage:
  ./install.sh [options]

Easy install (no prompts):
  --token TOKEN          Telegram bot token from @BotFather.
  --admin-id ID          Telegram user ID allowed to administer the bot.
                         Repeatable or comma-separated; required for a new
                         install (the whitelist is fail-closed).

Options:
  --easy                 Force non-interactive mode even without a token.
  --port PORT            Loopback port for /healthz, /readyz, /metrics.
                         Default: first free port from 8081.
  --dir DIR              Install/checkout directory when the script is not
                         run from a checkout (default: /opt/telegram-linux-admin
                         as root, ~/.telegram-linux-admin otherwise).
  --branch NAME          Git branch to install (default: main).
  --no-service           Do not install/start the systemd unit.
  --no-cron              Do not install the daily auto-update cron job.
  --no-start             Install but do not (re)start the service.
  --verbose              Show pip output.
  -h, --help             This help.

Examples:
  ./install.sh --token 123456:ABC-DEF --admin-id 123456789

  # on a fresh host, no checkout required:
  curl -fsSL https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/install.sh \
    | sudo bash -s -- --token 123456:ABC-DEF --admin-id 123456789

Environment equivalents:
  TLA_BOT_TOKEN, TLA_ADMIN_ID, TLA_HEALTH_PORT, TLA_HOME, TLA_BRANCH

Exit codes: 0 success, 1 runtime failure, 2 invalid arguments.
USAGE
}

# ---------------------------------------------------------------------------
# Arguments
# ---------------------------------------------------------------------------
TOKEN="${TLA_BOT_TOKEN:-}"
PORT="${TLA_HEALTH_PORT:-}"
TARGET_DIR="${TLA_HOME:-}"
EASY=0; NO_SERVICE=0; NO_CRON=0; NO_START=0; VERBOSE=0
ADMIN_IDS=()

if [ -n "${TLA_ADMIN_ID:-}" ]; then
    IFS=', ' read -r -a _env_ids <<< "$TLA_ADMIN_ID"
    for _id in "${_env_ids[@]}"; do [ -n "$_id" ] && ADMIN_IDS+=("$_id"); done
fi

need_value() { if [ $# -lt 2 ] || [ -z "${2:-}" ]; then die "option $1 requires a value"; fi; }

while [ $# -gt 0 ]; do
    case "$1" in
        --token)        need_value "$1" "${2:-}"; TOKEN="$2"; shift 2 ;;
        --token=*)      TOKEN="${1#*=}"; shift ;;
        --admin-id|--user-id|--telegram-id)
                        need_value "$1" "${2:-}"; ADMIN_IDS+=("$2"); shift 2 ;;
        --admin-id=*|--user-id=*|--telegram-id=*)
                        ADMIN_IDS+=("${1#*=}"); shift ;;
        --easy)         EASY=1; shift ;;
        --port|--health-port)
                        need_value "$1" "${2:-}"; PORT="$2"; shift 2 ;;
        --port=*|--health-port=*)
                        PORT="${1#*=}"; shift ;;
        --dir)          need_value "$1" "${2:-}"; TARGET_DIR="$2"; shift 2 ;;
        --dir=*)        TARGET_DIR="${1#*=}"; shift ;;
        --branch)       need_value "$1" "${2:-}"; BRANCH="$2"; shift 2 ;;
        --branch=*)     BRANCH="${1#*=}"; shift ;;
        --no-service)   NO_SERVICE=1; shift ;;
        --no-cron)      NO_CRON=1; shift ;;
        --no-start)     NO_START=1; shift ;;
        --verbose|-v)   VERBOSE=1; shift ;;
        -h|--help)      usage; exit 0 ;;
        *)              usage >&2; die "unknown option: $1" ;;
    esac
done

if [ -n "$PORT" ] && ! printf '%s' "$PORT" | grep -Eq '^[0-9]+$'; then
    die "--port must be numeric"
fi

# ---------------------------------------------------------------------------
# Locate (or fetch) the checkout
# ---------------------------------------------------------------------------
is_checkout() { [ -f "${1:-}/pyproject.toml" ] && [ -d "${1:-}/src" ]; }

APP_DIR=""
if is_checkout "$PWD"; then
    APP_DIR="$PWD"
    step "Using the current checkout: $APP_DIR"
else
    if [ -z "$TARGET_DIR" ]; then
        if [ "$(id -u)" -eq 0 ]; then
            TARGET_DIR="/opt/telegram-linux-admin"
        else
            TARGET_DIR="$HOME/.telegram-linux-admin"
        fi
    fi
    step "Fetching Telegram Linux Admin into $TARGET_DIR"

    if [ -d "$TARGET_DIR/.git" ]; then
        ok "Existing checkout found; updating it"
        if git -C "$TARGET_DIR" fetch --depth 1 origin "$BRANCH" >/dev/null 2>&1; then
            git -C "$TARGET_DIR" checkout -q "$BRANCH" >/dev/null 2>&1 || true
            git -C "$TARGET_DIR" merge --ff-only "origin/$BRANCH" >/dev/null 2>&1 \
                || warn "Could not fast-forward; using the existing tree as-is"
        else
            warn "git fetch failed; using the existing tree as-is"
        fi
    elif [ -e "$TARGET_DIR" ] && [ -n "$(ls -A "$TARGET_DIR" 2>/dev/null)" ]; then
        is_checkout "$TARGET_DIR" || die "$TARGET_DIR exists and is not a checkout"
        ok "Using existing directory $TARGET_DIR"
    else
        mkdir -p "$(dirname "$TARGET_DIR")"
        if command -v git >/dev/null 2>&1; then
            git clone --depth 1 --branch "$BRANCH" "$REPO_URL" "$TARGET_DIR" >/dev/null 2>&1 \
                || die "git clone failed ($REPO_URL, branch $BRANCH)"
        else
            command -v curl >/dev/null 2>&1 || die "git and curl are both missing"
            tmp_tar="$(mktemp)"
            curl -fsSL "https://codeload.github.com/$REPO_SLUG/tar.gz/refs/heads/$BRANCH" -o "$tmp_tar" \
                || die "download failed (no git, no tarball)"
            mkdir -p "$TARGET_DIR"
            tar -xzf "$tmp_tar" -C "$TARGET_DIR" --strip-components=1 \
                || die "could not unpack the source archive"
            rm -f "$tmp_tar"
        fi
    fi
    APP_DIR="$TARGET_DIR"
fi
cd "$APP_DIR"
ok "Install directory: $APP_DIR"

# ---------------------------------------------------------------------------
# Python 3.12+ and the virtualenv
# ---------------------------------------------------------------------------
find_python() {
    local candidate
    for candidate in python3.14 python3.13 python3.12 python3; do
        if command -v "$candidate" >/dev/null 2>&1 \
            && "$candidate" -c "import sys; raise SystemExit(0 if sys.version_info >= (3, $MIN_PY_MINOR) else 1)" 2>/dev/null; then
            printf '%s' "$candidate"
            return 0
        fi
    done
    return 1
}

PYTHON="$(find_python)" || die "Python 3.$MIN_PY_MINOR+ is required. Install it (e.g. apt install python3.12 python3.12-venv) and re-run."
step "Python: $("$PYTHON" --version 2>&1) ($(command -v "$PYTHON"))"

if [ ! -x "$APP_DIR/venv/bin/python" ]; then
    step "Creating virtual environment (venv/)"
    "$PYTHON" -m venv "$APP_DIR/venv" || die "python -m venv failed (install the venv module)"
fi
VENV_PY="$APP_DIR/venv/bin/python"
[ -x "$VENV_PY" ] || die "virtualenv is unusable: $VENV_PY"

step "Installing the bot (editable) and its dependencies"
"$VENV_PY" -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
if [ "$VERBOSE" -eq 1 ]; then
    "$VENV_PY" -m pip install -e "$APP_DIR" || die "pip install failed"
else
    "$VENV_PY" -m pip install --quiet -e "$APP_DIR" || die "pip install failed (re-run with --verbose)"
fi
ok "Package installed"

# A root-run bootstrap leaves root-owned files in a directory whose service
# account is the invoking user: hand ownership back before the unit starts.
if [ "$(id -u)" -eq 0 ] && [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
    chown -R "$SUDO_USER:$SUDO_USER" "$APP_DIR" 2>/dev/null || true
fi

# ---------------------------------------------------------------------------
# Configure + install the service
# ---------------------------------------------------------------------------
setup_args=()
[ "$EASY" -eq 1 ] && setup_args+=(--easy)
[ "$NO_SERVICE" -eq 1 ] && setup_args+=(--no-service)
[ "$NO_CRON" -eq 1 ] && setup_args+=(--no-cron)
[ "$NO_START" -eq 1 ] && setup_args+=(--no-start)
[ -n "$PORT" ] && setup_args+=(--health-port "$PORT")

admin_env=""
if [ "${#ADMIN_IDS[@]}" -gt 0 ]; then
    admin_env="$(IFS=,; printf '%s' "${ADMIN_IDS[*]}")"
fi

if [ -n "$TOKEN" ] || [ "$EASY" -eq 1 ] || [ -n "$admin_env" ]; then
    step "Configuring the bot (non-interactive)"
    # The token and IDs travel in the environment, never in argv, so they do
    # not show up in `ps` output or shell history.
    TLA_BOT_TOKEN="$TOKEN" TLA_ADMIN_ID="$admin_env" \
        "$VENV_PY" "$APP_DIR/scripts/setup.py" "${setup_args[@]}" \
        || die "setup failed"
else
    step "Starting the interactive setup wizard"
    "$VENV_PY" "$APP_DIR/scripts/setup.py" "${setup_args[@]}" \
        || die "setup failed"
fi

# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------
step "Result"
if systemctl list-unit-files telegram_bot.service >/dev/null 2>&1 \
    && systemctl is-enabled --quiet telegram_bot.service 2>/dev/null; then
    if systemctl is-active --quiet telegram_bot.service; then
        ok "telegram_bot.service: active (enabled at boot)"
    else
        warn "telegram_bot.service: installed but not active — sudo journalctl -u telegram_bot.service -n 40"
    fi
    printf '     logs:    sudo journalctl -u telegram_bot.service -f\n'
    printf '     restart: sudo systemctl restart telegram_bot.service\n'
else
    warn "systemd unit not installed (--no-service or systemd unavailable)"
    printf '     run manually: %s/%s\n' "$APP_DIR" "venv/bin/tla-bot"
fi

HEALTH_PORT="$("$VENV_PY" - "$APP_DIR" <<'PY' || true
import json, os, sys
path = os.path.join(sys.argv[1], "config.json")
try:
    with open(path, encoding="utf-8") as handle:
        print(json.load(handle).get("health_port", ""))
except Exception:
    print("")
PY
)"
if [ -n "$HEALTH_PORT" ]; then
    printf '     health:  curl -s 127.0.0.1:%s/readyz\n' "$HEALTH_PORT"
fi
printf '     data:    %s/config.json (0600), %s/database.db\n' "$APP_DIR" "$APP_DIR"
printf '%s\n' "${C_GREEN}${C_BOLD}Telegram Linux Admin is installed.${C_RESET} Open Telegram, send /start to your bot."
