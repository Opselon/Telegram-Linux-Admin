#!/usr/bin/env bash
#
# End-to-end test of both installation paths, against the real Telegram API.
#
#   TLA_BOT_TOKEN=123456:ABC-DEF TLA_ADMIN_ID=123456789 scripts/e2e_setup.sh all
#
# Modes:
#   easy  — install.sh --token/--admin-id (one command, no prompts)
#   pro   — install.sh with the interactive wizard driven from stdin
#   all   — both (default)
#
# Each mode gets a throwaway copy of the checkout, a fresh venv, its own
# config/database, and a real bot process whose /healthz and /readyz are
# probed over HTTP. The token's validity is proven with a live getMe call.
#
# systemd and cron are skipped by default so the test never touches the host;
# set TLA_E2E_SERVICE=1 to exercise those units too (root + systemd required).
#
# Exit code: 0 when every check passed, 1 otherwise. KEEP=1 keeps the
# scratch directories for inspection.
set -uo pipefail

MODE="${1:-all}"
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
ADMIN="${TLA_ADMIN_ID:-}"
KEEP="${KEEP:-0}"
WITH_SERVICE="${TLA_E2E_SERVICE:-0}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/tla-e2e.XXXXXX")"

if [ -t 1 ]; then
    G=$'\033[32m'; R=$'\033[31m'; Y=$'\033[33m'; B=$'\033[1m'; N=$'\033[0m'
else
    G=""; R=""; Y=""; B=""; N=""
fi

PASS=0; FAIL=0; WARN=0
pass() { PASS=$((PASS+1)); printf '%s  PASS%s %s\n' "$G" "$N" "$*"; }
fail() { FAIL=$((FAIL+1)); printf '%s  FAIL%s %s\n' "$R" "$N" "$*"; }
warn() { WARN=$((WARN+1)); printf '%s  WARN%s %s\n' "$Y" "$N" "$*"; }
head1() { printf '\n%s== %s ==%s\n' "$B" "$*" "$N"; }

cleanup() {
    if [ "$KEEP" = "1" ]; then
        printf '\nscratch kept at %s\n' "$WORK"
    else
        rm -rf "$WORK"
    fi
}
trap cleanup EXIT

if [ -z "${TLA_BOT_TOKEN:-}" ]; then
    printf '%sTLA_BOT_TOKEN is required (a real token from @BotFather).%s\n' "$R" "$N" >&2
    exit 2
fi
if [ -z "$ADMIN" ]; then
    printf '%sTLA_ADMIN_ID is required (the Telegram user ID that will own the bot).%s\n' "$R" "$N" >&2
    exit 2
fi
case "$MODE" in easy|pro|all) ;; *) printf 'mode must be easy, pro or all\n' >&2; exit 2 ;; esac

free_port() {
    python3 - <<'PY'
import socket
with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

make_copy() {  # $1 = destination
    mkdir -p "$1"
    tar -C "$REPO_ROOT" \
        --exclude=.git --exclude=venv --exclude=.venv \
        --exclude=__pycache__ --exclude=.pytest_cache --exclude=.ruff_cache \
        --exclude=dist --exclude=build --exclude=.atf \
        -cf - . 2>/dev/null | tar -x -C "$1"
}

wait_ready() {  # $1 = port, $2 = timeout seconds
    local port="$1" timeout="${2:-90}" body=""
    for _ in $(seq 1 "$timeout"); do
        body="$(curl -fsS --max-time 3 "http://127.0.0.1:$port/readyz" 2>/dev/null || true)"
        if printf '%s' "$body" | grep -q '"status": *"ready"'; then
            printf '%s' "$body"
            return 0
        fi
        sleep 1
    done
    printf '%s' "$body"
    return 1
}

start_bot() {  # $1 = dir, $2 = port, $3 = log; echoes PID
    local dir="$1" port="$2" log="$3"
    mkdir -p "$dir/var/log"
    (
        cd "$dir" || exit 1
        TLA_CONFIG_FILE="$dir/config.json" \
        TLA_DB_FILE="$dir/database.db" \
        TLA_AUDIT_DIR="$dir/var/log" \
        TLA_HEALTH_PORT="$port" \
        TLA_HEALTH_HOST="127.0.0.1" \
        PYTHONPATH="$dir" \
        exec "$dir/venv/bin/tla-bot"
    ) >"$log" 2>&1 &
    printf '%s' "$!"
}

stop_bot() {  # $1 = pid
    [ -n "${1:-}" ] || return 0
    kill "$1" 2>/dev/null || true
    for _ in $(seq 1 20); do
        kill -0 "$1" 2>/dev/null || return 0
        sleep 0.25
    done
    kill -9 "$1" 2>/dev/null || true
}

telegram_get_me() {
    curl -fsS --max-time 20 "https://api.telegram.org/bot${TLA_BOT_TOKEN}/getMe" 2>/dev/null
}

# The harness itself runs with TLA_* exported (that is how it learns the
# credentials); the setup scripts must not inherit them, or the "pro" wizard
# would silently take the non-interactive branch and install a systemd unit.
sandbox_env() {
    env -u TLA_BOT_TOKEN -u TLA_ADMIN_ID -u TLA_ADMIN_IDS -u TLA_CONFIG_FILE \
        -u TLA_DB_FILE -u TLA_AUDIT_DIR -u TLA_HEALTH_PORT -u TLA_HEALTH_HOST \
        -u TLA_HOME -u TLA_BRANCH -u TLA_E2E_TOKEN "$@"
}

send_test_message() {  # $1 = text
    curl -sS --max-time 20 -o /dev/null -w '%{http_code}' \
        -X POST "https://api.telegram.org/bot${TLA_BOT_TOKEN}/sendMessage" \
        -d "chat_id=${ADMIN}" --data-urlencode "text=$1" 2>/dev/null
}

probe_endpoints() {  # $1 = dir, $2 = port, $3 = label
    local dir="$1" port="$2" label="$3" health ready
    health="$(curl -fsS --max-time 5 "http://127.0.0.1:$port/healthz" 2>/dev/null || true)"
    if printf '%s' "$health" | grep -q '"status": *"ok"'; then
        pass "$label: /healthz answered ok"
    else
        fail "$label: /healthz did not answer ok (got: ${health:-nothing})"
    fi

    ready="$(wait_ready "$port" 90)"
    if [ $? -eq 0 ] && printf '%s' "$ready" | grep -q '"token_configured": *true'; then
        pass "$label: /readyz reported ready with the token configured"
    else
        fail "$label: /readyz not ready (got: ${ready:-nothing})"
    fi

    if curl -fsS --max-time 5 "http://127.0.0.1:$port/metrics" 2>/dev/null | grep -q '^tla_' ; then
        pass "$label: /metrics exported tla_* samples"
    else
        fail "$label: /metrics did not export tla_* samples"
    fi

    if [ -f "$dir/database.db" ]; then
        pass "$label: database.db created"
    else
        fail "$label: database.db missing"
    fi
    if [ "$(stat -c '%a' "$dir/config.json" 2>/dev/null || echo '?')" = "600" ]; then
        pass "$label: config.json is mode 0600"
    else
        warn "$label: config.json mode is $(stat -c '%a' "$dir/config.json" 2>/dev/null || echo unknown)"
    fi
}

# ---------------------------------------------------------------------------
# easy: one command, no prompts
# ---------------------------------------------------------------------------
run_easy() {
    head1 "easy setup (one command)"
    local dir="$WORK/easy" port log pid setup_flags=()
    make_copy "$dir"
    port="$(free_port)"
    log="$dir/bot.log"

    [ "$WITH_SERVICE" = "1" ] || setup_flags+=(--no-service --no-cron)

    if ( cd "$dir" && sandbox_env bash install.sh --token "$TLA_BOT_TOKEN" --admin-id "$ADMIN" \
            --port "$port" "${setup_flags[@]}" ) >"$dir/install.log" 2>&1; then
        pass "easy: install.sh exited 0"
    else
        fail "easy: install.sh failed (see $dir/install.log)"
        tail -25 "$dir/install.log"
        return
    fi

    if [ -f "$dir/config.json" ] && python3 - "$dir" "$ADMIN" <<'PY'
import json, sys
with open(sys.argv[1] + "/config.json", encoding="utf-8") as handle:
    data = json.load(handle)
assert data["access_mode"] == "whitelist", data
assert data["destination_policy"] == "deny_public", data
assert int(sys.argv[2]) in [int(u) for u in data["whitelisted_users"]], data
PY
    then
        pass "easy: config is fail-closed (whitelist + deny_public) with the admin ID"
    else
        fail "easy: config content is not fail-closed"
        cat "$dir/config.json" 2>/dev/null
    fi

    if [ "$WITH_SERVICE" = "1" ]; then
        if systemctl is-active --quiet telegram_bot.service; then
            pass "easy: telegram_bot.service is active"
        else
            fail "easy: telegram_bot.service is not active"
        fi
        if [ -f /etc/cron.d/telegram_bot_update ]; then
            pass "easy: auto-update cron job installed"
        else
            fail "easy: auto-update cron job missing"
        fi
        # The unit itself owns the process; probe the port it was given.
        probe_endpoints "$dir" "$port" "easy"
        return
    fi

    pid="$(start_bot "$dir" "$port" "$log")"
    sleep 2
    probe_endpoints "$dir" "$port" "easy"
    stop_bot "$pid"
}

# ---------------------------------------------------------------------------
# pro: the guided wizard, driven from stdin
# ---------------------------------------------------------------------------
run_pro() {
    head1 "pro setup (interactive wizard)"
    local dir="$WORK/pro" port log pid
    make_copy "$dir"
    port="$(free_port)"
    log="$dir/bot.log"

    # Answers: token, admin id, no systemd service, no cron, exit the wizard.
    if ( cd "$dir" && printf '%s\n%s\nn\nn\n\n' "$TLA_BOT_TOKEN" "$ADMIN" | sandbox_env bash install.sh ) \
            >"$dir/install.log" 2>&1; then
        pass "pro: wizard exited 0"
    else
        fail "pro: wizard failed (see $dir/install.log)"
        tail -25 "$dir/install.log"
        return
    fi

    if [ -f "$dir/config.json" ] && python3 - "$dir" "$ADMIN" <<'PY'
import json, sys
with open(sys.argv[1] + "/config.json", encoding="utf-8") as handle:
    data = json.load(handle)
assert int(sys.argv[2]) in [int(u) for u in data["whitelisted_users"]], data
assert data["telegram_token"], "token missing"
PY
    then
        pass "pro: wizard wrote the token and the admin ID"
    else
        fail "pro: wizard did not write a usable config"
        cat "$dir/config.json" 2>/dev/null
    fi

    pid="$(start_bot "$dir" "$port" "$log")"
    sleep 2
    probe_endpoints "$dir" "$port" "pro"
    stop_bot "$pid"
}

# ---------------------------------------------------------------------------
# Live Telegram round trip (shared, once)
# ---------------------------------------------------------------------------
run_telegram() {
    head1 "Telegram API"
    local me
    me="$(telegram_get_me)"
    if printf '%s' "$me" | grep -q '"ok": *true'; then
        pass "getMe accepted the token"
    else
        fail "getMe rejected the token: ${me:-no response}"
        return
    fi

    local code
    code="$(send_test_message "Telegram Linux Admin E2E test: setup and bot are working.")"
    case "$code" in
        200) pass "sendMessage delivered a test message to $ADMIN" ;;
        400|403)
            warn "sendMessage returned HTTP $code — open the bot in Telegram and press /start, then re-run" ;;
        *)
            warn "sendMessage returned HTTP ${code:-no response}" ;;
    esac
}

head1 "Telegram Linux Admin — end-to-end setup test"
printf 'repo: %s\nmode: %s\nadmin: %s\nscratch: %s\n' "$REPO_ROOT" "$MODE" "$ADMIN" "$WORK"

case "$MODE" in
    easy) run_easy ;;
    pro) run_pro ;;
    all) run_easy; run_pro ;;
esac
run_telegram

head1 "Summary"
printf 'passed: %d   failed: %d   warnings: %d\n' "$PASS" "$FAIL" "$WARN"
if [ "$FAIL" -ne 0 ]; then
    printf '%sE2E FAILED%s\n' "$R" "$N"
    exit 1
fi
printf '%sE2E PASSED%s\n' "$G" "$N"
