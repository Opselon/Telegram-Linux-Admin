#!/usr/bin/env bash
# Linux Secure Manager - Pro Edition
# Adds: extensive debug logging, inline keyboard callbacks, async task handling, systemd installer
# Version: 9.2-pro
set -euo pipefail
IFS=$'\n\t'
umask 0077
shopt -s inherit_errexit

# Paths
SCRIPT_PATH="$(realpath "$0")"
CONFIG_FILE="/etc/linux_secure_manager.conf"
LOG_FILE="/var/log/linux_secure_manager.log"
DEBUG_LOG="/var/log/linux_secure_manager.debug.log"
SESSION_DIR="/run/linux_secure_manager/session"
CONFIRM_DIR="/run/linux_secure_manager/confirm"
OFFSET_FILE="/run/linux_secure_manager/offset.dat"
REPO_URL="https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/linux_secure_manager.sh"
LOCKFILE="/run/lock/linux_secure_manager.lock"
SYSTEMD_UNIT="/etc/systemd/system/linux_secure_manager.service"

# create runtime directories
mkdir -p "$SESSION_DIR" "$CONFIRM_DIR" "$(dirname "$OFFSET_FILE")" "$(dirname "$LOCKFILE")"
chmod 700 "$SESSION_DIR" "$CONFIRM_DIR" || true
touch "$LOG_FILE" "$DEBUG_LOG"
chmod 600 "$LOG_FILE" "$DEBUG_LOG" || true

# Color
C_BOLD="\033[1m"; C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_BLUE="\033[34m"; C_RED="\033[31m"; C_RESET="\033[0m"

# -------------------------
# Error handling & debug
# -------------------------
err_handler() {
    local rc=$?; local lastcmd="${BASH_COMMAND:-unknown}"
    local lineno="${BASH_LINENO[0]:-unknown}"
    local funcstack=""
    for (( i=0; i<${#FUNCNAME[@]}; i++ )); do funcstack+=" ${FUNCNAME[$i]}(${BASH_LINENO[$i]})"; done
    local ts; ts=$(date --iso-8601=seconds)
    local host; host=$(hostname --short)
    printf '%s\n' "{\"ts\":\"$ts\",\"host\":\"$host\",\"level\":\"ERROR\",\"rc\":$rc,\"line\":$lineno,\"cmd\":\"$(echo "$lastcmd" | sed 's/"/\\"/g')\",\"func_stack\":\"$funcstack\"}" >> "$DEBUG_LOG"
    # extra context for config issues or filesystem failures
    echo "----- DEBUG CONTEXT -----" >> "$DEBUG_LOG"
    echo "User: $(whoami) UID: $(id -u)" >> "$DEBUG_LOG"
    echo "PWD: $(pwd)" >> "$DEBUG_LOG"
    echo "Disk free:" >> "$DEBUG_LOG"
    df -h >> "$DEBUG_LOG" 2>&1 || true
    echo "ls -l $(dirname "$CONFIG_FILE"):" >> "$DEBUG_LOG"
    ls -la "$(dirname "$CONFIG_FILE")" >> "$DEBUG_LOG" 2>&1 || true
    # if config exists, show attributes
    if [ -f "$CONFIG_FILE" ]; then
        echo "Config file stat:" >> "$DEBUG_LOG"
        stat "$CONFIG_FILE" >> "$DEBUG_LOG" 2>&1 || true
        echo "lsattr:" >> "$DEBUG_LOG"
        lsattr "$CONFIG_FILE" 2>/dev/null || true
    fi
    # log last 200 lines of main log
    echo "----- last lines of main log -----" >> "$DEBUG_LOG"
    tail -n 200 "$LOG_FILE" >> "$DEBUG_LOG" 2>&1 || true
    # notify admin if telegram configured
    if [ -n "${TELEGRAM_API:-}" ] && [ -n "${TELEGRAM_CHAT_ID:-}" ]; then
        # best-effort: notify that a failure happened; don't include secrets
        curl --max-time 10 -s -X POST "${TELEGRAM_API}/sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=⚠️ *LSM Error*: An internal error occurred on $(hostname). Check debug log." -d "parse_mode=Markdown" >/dev/null 2>&1 || true
    fi
    exit $rc
}
trap err_handler ERR

# -------------------------
# Pro logger (JSON-ish)
# -------------------------
pro_log() {
    local level="${1:-INFO}"; shift || true; local msg="${*:-}"
    local ts; ts=$(date --iso-8601=seconds)
    local host; host=$(hostname --short)
    printf '%s\n' "{\"ts\":\"$ts\",\"host\":\"$host\",\"level\":\"$level\",\"pid\":$$,\"msg\":\"$(echo "$msg" | sed 's/"/\\"/g')\"}" >> "$LOG_FILE"
    # also copy verbose to debug log for errors and warnings
    if [ "$level" = "ERROR" ] || [ "$level" = "WARN" ]; then
        printf '%s\n' "{\"ts\":\"$ts\",\"host\":\"$host\",\"level\":\"$level\",\"pid\":$$,\"msg\":\"$(echo "$msg" | sed 's/"/\\"/g')\"}" >> "$DEBUG_LOG"
    fi
}

# -------------------------
# Utilities
# -------------------------
urlencode() {
    local s="$*"; local out=""
    for ((i=0;i<${#s};i++)); do c=${s:i:1}; case $c in [a-zA-Z0-9.~_-]) out+="$c";; *) out+=$(printf '%%%02X' "'$c");; esac; done
    printf '%s' "$out"
}
safe_mktemp_dir() { mktemp -d --tmpdir "$SESSION_DIR/tmp.XXXXXX"; }

# -------------------------
# Config management (atomic + diagnostics)
# -------------------------
load_config() {
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck disable=SC1090
        . "$CONFIG_FILE"
    else
        TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
        TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
        ENABLE_AUTO_MAINTENANCE="${ENABLE_AUTO_MAINTENANCE:-1}"
    fi
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then TELEGRAM_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"; else TELEGRAM_API=""; fi
}

save_config_atomic() {
    local tfile
    tfile=$(mktemp -u "${CONFIG_FILE}.tmp.XXXXXX")
    cat > "$tfile" <<EOF
# Linux Secure Manager configuration (keep private)
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ENABLE_AUTO_MAINTENANCE="${ENABLE_AUTO_MAINTENANCE:-1}"
EOF
    chmod 600 "$tfile" || true
    chown root:root "$tfile" 2>/dev/null || true
    # test write ability
    if ! mv -f "$tfile" "$CONFIG_FILE"; then
        pro_log "ERROR" "Failed to move temp config into ${CONFIG_FILE}"
        # capture diagnostics
        {
            echo "mv failed moving $tfile to $CONFIG_FILE at $(date --iso-8601=seconds)"
            echo "--- lsattr and permissions ---"
            lsattr "$CONFIG_FILE" 2>/dev/null || true
            ls -la "$(dirname "$CONFIG_FILE")" || true
            echo "--- mount options ---"
            mount | grep "on $(dirname "$CONFIG_FILE") " 2>/dev/null || true
        } >> "$DEBUG_LOG"
        die "FAILED to write configuration (mv failed). See $DEBUG_LOG"
    fi
    chmod 600 "$CONFIG_FILE"
    pro_log "INFO" "Config saved to $CONFIG_FILE"
}

# -------------------------
# Telegram helpers (timeouts, safe calls)
# -------------------------
curl_post() {
    curl --fail --silent --show-error --max-time 20 --location "$@" || true
}

sendMessage() {
    local text="$1"
    [ -n "${TELEGRAM_API:-}" ] || { pro_log "WARN" "sendMessage: TELEGRAM not configured"; return 1; }
    local payload=$(printf '%s' "$text")
    curl_post -X POST "${TELEGRAM_API}/sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=${payload}" -d "parse_mode=Markdown" >/dev/null 2>&1 || {
        pro_log "WARN" "sendMessage failed (payload truncated)"; return 1; }
}

sendMessageWithKeyboard() {
    # $1 = text, $2 = JSON for reply_markup (already encoded as JSON string)
    local text="$1"; local reply_markup_json="$2"
    [ -n "${TELEGRAM_API:-}" ] || return 1
    curl_post -X POST "${TELEGRAM_API}/sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=${text}" -d "reply_markup=${reply_markup_json}" -d "parse_mode=Markdown" >/dev/null 2>&1 || pro_log "WARN" "sendMessageWithKeyboard failed"
}

answerCallback() {
    local cb_id="$1"; local text="$2"
    curl_post -X POST "${TELEGRAM_API}/answerCallbackQuery" -d "callback_query_id=${cb_id}" -d "text=${text}" >/dev/null 2>&1 || pro_log "WARN" "answerCallback failed"
}

sendDocument() {
    local filepath="$1"; local caption="${2:-File}"
    [ -n "${TELEGRAM_API:-}" ] || return 1
    if [ ! -f "$filepath" ] || [ ! -r "$filepath" ]; then pro_log "WARN" "sendDocument: not readable $filepath"; return 1; fi
    local size; size=$(stat -c%s "$filepath")
    if [ "$size" -gt 46900000 ]; then pro_log "WARN" "sendDocument: file too big $size"; sendMessage "❌ File too large: $(basename "$filepath")"; return 1; fi
    sendAction "upload_document"
    curl_post -X POST "${TELEGRAM_API}/sendDocument" -F "chat_id=${TELEGRAM_CHAT_ID}" -F "document=@${filepath}" -F "caption=${caption}" >/dev/null 2>&1 || pro_log "WARN" "sendDocument failed for $filepath"
}

sendAction() { local action="$1"; [ -n "${TELEGRAM_API:-}" ] || return 1; curl_post -X POST "${TELEGRAM_API}/sendChatAction" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "action=${action}" >/dev/null 2>&1 || true; }

sendOutputAsFile() {
    local command_name="$1"; local output="$2"
    local tmpdir; tmpdir=$(safe_mktemp_dir)
    local tmpfile="${tmpdir}/output.txt"
    {
        printf "Command: %s\nHost: %s\nDate: %s\n\n" "$command_name" "$(hostname)" "$(date --iso-8601=seconds)"
        printf "%s\n" "$output"
    } > "$tmpfile"
    chmod 600 "$tmpfile"
    sendDocument "$tmpfile" "Output for ${command_name}"
    rm -rf "$tmpdir"
}

# -------------------------
# Preflight checks
# -------------------------
preflight_checks() {
    # network connectivity to api.telegram.org
    if ! ping -c1 -W2 api.telegram.org >/dev/null 2>&1; then pro_log "WARN" "No network connectivity to api.telegram.org"; fi
    # disk space check (at least 50MB)
    local free_kb; free_kb=$(df --output=avail / | tail -1)
    if [ "$free_kb" -lt 51200 ]; then pro_log "WARN" "Low disk space on / (available KB: $free_kb)"; fi
    # required binaries
    for bin in curl jq systemd-cat logger; do
        if ! command -v "$bin" >/dev/null 2>&1; then pro_log "WARN" "Missing binary: $bin"; fi
    done
}

# -------------------------
# Confirmation token system (secure)
# -------------------------
create_confirmation() {
    local action="$1"
    local code; code=$(head -c 12 /dev/urandom | od -An -tx1 | tr -d ' \n')
    local file="${CONFIRM_DIR}/${code}"
    local payload; payload=$(printf '{"ts":"%s","cmd":"%s"}' "$(date +%s)" "$(printf '%s' "$action" | base64 -w0)")
    printf '%s' "$payload" > "$file"
    chmod 600 "$file"
    pro_log "INFO" "Confirmation created code=$code cmd=$(echo "$action" | sed 's/"/\\"/g')"
    sendMessage "⚠️ *Confirmation Required*\nTo proceed reply with:\n\`/confirm $code\`\nExpires in 15 minutes."
}

execute_confirmation() {
    local code="$1"
    local file="${CONFIRM_DIR}/${code}"
    if [ ! -f "$file" ]; then sendMessage "❌ Invalid or expired confirmation code."; pro_log "WARN" "Invalid confirmation attempt code=$code"; return; fi
    local payload; payload=$(cat "$file")
    local ts; ts=$(echo "$payload" | jq -r '.ts' 2>/dev/null || echo "")
    local b64; b64=$(echo "$payload" | jq -r '.cmd' 2>/dev/null || echo "")
    if [ -z "$ts" ] || [ -z "$b64" ]; then rm -f "$file"; sendMessage "❌ Invalid confirmation data."; return; fi
    local now; now=$(date +%s)
    if [ "$((now - ts))" -gt 900 ]; then rm -f "$file"; sendMessage "❌ Confirmation code expired."; pro_log "WARN" "Expired confirmation code $code"; return; fi
    rm -f "$file"
    local cmd; cmd=$(printf '%s' "$b64" | base64 -d -w0)
    # run command in background and notify
    pro_log "INFO" "Executing confirmed command: $cmd"
    ( bash -lc "$cmd" 2>&1 ) >"${SESSION_DIR}/confirm_${code}.out" || true
    sendDocument "${SESSION_DIR}/confirm_${code}.out" "Confirmed command output"
    rm -f "${SESSION_DIR}/confirm_${code}.out" || true
    sendMessage "✅ Action executed."
}

# -------------------------
# Feature functions
# -------------------------
get_system_status() {
    local UPTIME; UPTIME=$(uptime -p || true)
    local MEM; MEM=$(free -m | awk 'NR==2{printf "%.2f%%", $3*100/$2 }' || true)
    local DISK; DISK=$(df -h / | awk 'NR==2{printf "%s", $5}' || true)
    local LOAD; LOAD=$(awk '{print $1","$2","$3}' <(uptime | sed -n 's/.*load average: //p') 2>/dev/null || true)
    local USERS; USERS=$(who | wc -l || true)
    local STATUS_REPORT="*Server Status:* $(hostname)
\`\`\`
- Uptime:         ${UPTIME}
- Memory Usage:   ${MEM}
- Disk Usage (/): ${DISK}
- Logged in Users:${USERS}
- Load Average:   ${LOAD}
\`\`\`"
    sendMessage "$STATUS_REPORT"
    pro_log "INFO" "Status reported"
}

check_updates() {
    sendMessage "🔄 Checking for package updates..."
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y >/dev/null 2>&1 || pro_log "WARN" "apt-get update failed"
        local UPGRADABLE; UPGRADABLE=$(apt list --upgradable 2>/dev/null | sed '1d' || true)
        if [ -z "$UPGRADABLE" ]; then sendMessage "✅ No packages to upgrade."; else sendOutputAsFile "checkupdates" "$UPGRADABLE"; fi
    else
        sendMessage "❌ Unsupported package manager"
    fi
}

run_updates() {
    pro_log "INFO" "Initiating background system upgrade"
    sendMessage "🚀 Starting system upgrade in background. Will notify on completion."
    (
        set -e
        apt-get update -y >>"$LOG_FILE" 2>&1
        DEBIAN_FRONTEND=noninteractive apt-get upgrade -y >>"$LOG_FILE" 2>&1
        apt-get autoremove --purge -y >>"$LOG_FILE" 2>&1
        apt-get clean >>"$LOG_FILE" 2>&1
        pro_log "INFO" "System upgrade finished"
        sendMessage "✅ *System upgrade complete on $(hostname)!*"
    ) & disown
}

# process control, users, firewall same as previous version but with extra logs
manage_user() {
    local action="${1:-}"; local username="${2:-}"
    if ! [[ "$username" =~ ^[a-z_][a-z0-9_-]{1,31}$ ]]; then sendMessage "❌ Invalid username format."; pro_log "WARN" "Invalid username input: $username"; return; fi
    if [ "$action" = "add" ]; then
        local password; password=$(tr -dc 'A-Za-z0-9!@%_-+=' </dev/urandom | head -c 16)
        if useradd -m -s /bin/bash "$username"; then
            echo "${username}:${password}" | chpasswd
            sendMessage "✅ User \`${username}\` created.\n\n*Temporary Password:*\n\`\`\`${password}\`\`\`\nChange it immediately."
            pro_log "INFO" "User created: $username"
        else
            sendMessage "❌ Failed to create user \`${username}\`."
            pro_log "ERROR" "useradd failed for $username"
        fi
    elif [ "$action" = "del" ]; then
        create_confirmation "userdel -r ${username} && logger -t linux_secure_manager 'Deleted user ${username}'"
        sendMessage "⚠️ Deletion scheduled. Confirm with /confirm <code>."
    else
        sendMessage "Usage: /user add|del <username>"
    fi
}

manage_firewall() {
    local action="${1:-}"; local port="${2:-}"
    if ! command -v ufw >/dev/null 2>&1; then sendMessage "❌ UFW not installed"; pro_log "WARN" "ufw missing"; return; fi
    case "$action" in
        status) local s; s=$(ufw status verbose 2>&1); sendOutputAsFile "ufw status" "$s" ;;
        enable) ufw --force enable >/dev/null 2>&1 && sendMessage "✅ Firewall enabled." || sendMessage "❌ Failed." ;;
        disable) ufw --force disable >/dev/null 2>&1 && sendMessage "✅ Firewall disabled." || sendMessage "❌ Failed." ;;
        allow|deny) if [[ ! "$port" =~ ^[0-9]+(/[a-z]+)?$ ]]; then sendMessage "❌ Invalid port"; return; fi; ufw "$action" "$port" >/dev/null 2>&1 && sendMessage "✅ Rule applied." || sendMessage "❌ Failed." ;;
        *) sendMessage "Usage: /ufw status|enable|disable|allow|deny <port>" ;;
    esac
}

manage_processes() {
    local action="${1:-}"; local pid="${2:-}"
    if [ "$action" = "top" ]; then local topout; topout=$(top -b -n 1 | head -n 17); sendOutputAsFile "top" "$topout"
    elif [ "$action" = "kill" ]; then
        if [[ ! "$pid" =~ ^[0-9]+$ ]]; then sendMessage "❌ Invalid PID"; return; fi
        create_confirmation "kill -9 ${pid} && logger -t linux_secure_manager 'Killed PID ${pid}'"
        sendMessage "⚠️ Kill scheduled. Confirm with /confirm <code>."
    else sendMessage "Usage: /top or /kill <pid>"; fi
}

get_log_file() {
    local path="${1:-/var/log/syslog}"
    local real; real=$(realpath -m "$path")
    if [[ "$real" != /var/log/* ]]; then sendMessage "❌ Access denied. Only /var/log/*"; pro_log "WARN" "get_log_file attempted outside /var/log: $path"; return; fi
    if [ -r "$real" ]; then local out; out=$(tail -n 1000 "$real" 2>/dev/null || true); sendOutputAsFile "getlog $(basename "$real")" "$out"; else sendMessage "❌ Not readable"; fi
}

# -------------------------
# Pro Terminal Manager (buttons + callback)
# -------------------------
terminal_main_menu() {
    # build inline keyboard JSON (escaped)
    local keyboard='{"inline_keyboard":[[{"text":"Uptime","callback_data":"term:uptime"},{"text":"Top","callback_data":"term:top"}],[{"text":"Disk Usage","callback_data":"term:disk"},{"text":"Memory","callback_data":"term:mem"}],[{"text":"Run Custom Command","callback_data":"term:custom"}]]}'
    sendMessageWithKeyboard "*Terminal Manager*" "$keyboard"
}

handle_terminal_callback() {
    local query_id="$1"; local data="$2"
    case "$data" in
        term:uptime)
            answerCallback "$query_id" "Running uptime..."
            local out; out=$(uptime -p 2>&1 || true)
            sendMessage "*Uptime:* `$(echo "$out")`"
            ;;
        term:top)
            answerCallback "$query_id" "Fetching top..."
            local out; out=$(top -b -n1 | head -n 20)
            sendOutputAsFile "top" "$out"
            ;;
        term:disk)
            answerCallback "$query_id" "Checking disk..."
            local out; out=$(df -h)
            sendOutputAsFile "df" "$out"
            ;;
        term:mem)
            answerCallback "$query_id" "Checking memory..."
            local out; out=$(free -h)
            sendOutputAsFile "free" "$out"
            ;;
        term:custom)
            answerCallback "$query_id" "Reply to this message with /cmd <your command> to run it."
            ;;
        *)
            answerCallback "$query_id" "Unknown terminal action."
            pro_log "WARN" "Unknown terminal callback: $data"
            ;;
    esac
}

# run arbitrary command via /cmd
run_ad_hoc_command() {
    local cmd="$*"
    if [ -z "$cmd" ]; then sendMessage "Usage: /cmd <command>"; return; fi
    pro_log "INFO" "Ad-hoc command queued: $cmd"
    sendMessage "⏳ Executing command in background..."
    local outfile="${SESSION_DIR}/cmd_$(date +%s).out"
    ( bash -lc "$cmd" >"$outfile" 2>&1 || true; sendDocument "$outfile" "Command output: $(echo "$cmd" | cut -c1-80)"; rm -f "$outfile" ) & disown
}

# -------------------------
# Shell session (same as previous but uses keyboard on start)
# -------------------------
is_shell_active() { [ -f "${SESSION_DIR}/active.lock" ]; }
get_shell_prompt() { local cwd; cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "~"); cwd=${cwd/#$HOME/\~}; echo "*root@$(hostname):${cwd}#*"; }
start_shell_session() {
    stop_shell_session || true
    mkdir -p "$SESSION_DIR"
    touch "${SESSION_DIR}/active.lock"
    echo "$HOME" > "${SESSION_DIR}/cwd.txt"
    pro_log "INFO" "Remote shell started"
    # show terminal manager keyboard
    terminal_main_menu
    sendMessage "$(get_shell_prompt)"
}
stop_shell_session() {
    if [ -d "$SESSION_DIR" ]; then rm -rf "$SESSION_DIR"; pro_log "INFO" "Remote shell stopped"; fi
}
execute_shell_command() {
    local cmd="$*"
    if [ -z "$cmd" ]; then sendMessage "No command provided."; return; fi
    if [ -f "${SESSION_DIR}/pid.txt" ]; then sendMessage "⏳ Background task in progress. Wait."; return; fi
    pro_log "INFO" "Shell cmd exec queued: $cmd"
    local cwd; cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "$HOME")
    local out="${SESSION_DIR}/output.log"
    ( cd "$cwd" && bash -lc "$cmd" ) >"$out" 2>&1 &
    local pid=$!; echo "$pid" > "${SESSION_DIR}/pid.txt"
    sendMessage "⏳ Executing (PID: $pid)..."
}

check_background_process() {
    if [ -f "${SESSION_DIR}/pid.txt" ]; then
        local pid; pid=$(cat "${SESSION_DIR}/pid.txt" 2>/dev/null || "")
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            local out; out=$(cat "${SESSION_DIR}/output.log" 2>/dev/null || "")
            sendDocument <(printf "%s" "$out") "Shell command output" 2>/dev/null || sendOutputAsFile "shellcmd" "$out"
            sendMessage "$(get_shell_prompt)"
            rm -f "${SESSION_DIR}/pid.txt" "${SESSION_DIR}/output.log"
        fi
    fi
}

upload_file_to_telegram(){
    local file="$1"
    local real; real=$(realpath -m "$file" 2>/dev/null || echo "")
    if [ -f "$real" ] && [ -r "$real" ]; then sendDocument "$real" "File from $(hostname)"; else sendMessage "❌ File not found or unreadable"; fi
}
download_file_from_telegram(){
    local json="$1"
    local file_id; file_id=$(echo "$json" | jq -r '.document.file_id' 2>/dev/null || echo "")
    local file_name; file_name=$(echo "$json" | jq -r '.document.file_name' 2>/dev/null || echo "")
    local cwd; cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "/root")
    if [ -z "$file_id" ] || [ "$file_id" = "null" ]; then sendMessage "❌ No document"; return; fi
    local path; path=$(curl_post "${TELEGRAM_API}/getFile?file_id=${file_id}" | jq -r '.result.file_path' 2>/dev/null || echo "")
    if [ -n "$path" ] && [ "$path" != "null" ]; then curl_post -s -o "${cwd}/${file_name}" "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${path}" || true; sendMessage "✅ Downloaded ${file_name} to ${cwd}"; else sendMessage "❌ Download failed"; fi
}

self_update_script() {
    sendMessage "🔄 Checking for updates..."
    local tmpd; tmpd=$(mktemp -d)
    local tmpf="${tmpd}/lsm.new"
    if ! curl_post -sSL "$REPO_URL" -o "$tmpf"; then sendMessage "❌ Download failed."; rm -rf "$tmpd"; return; fi
    chmod +x "$tmpf"
    mv -f "$tmpf" "$SCRIPT_PATH"
    chmod +x "$SCRIPT_PATH"
    sendMessage "✅ Script updated. Restarting service may be required."
    pro_log "INFO" "Script self-updated"
    rm -rf "$tmpd"
}

# -------------------------
# Listener (handles messages + callback_query)
# -------------------------
listen_for_commands() {
    load_config
    if [ -z "${TELEGRAM_API:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then pro_log "ERROR" "Not configured"; exit 1; fi
    for cmd in curl jq; do if ! command -v "$cmd" >/dev/null 2>&1; then pro_log "ERROR" "Missing dependency $cmd"; exit 1; fi; done
    exec 9>"$LOCKFILE"
    if ! flock -n 9; then pro_log "WARN" "Listener already running"; exit 0; fi

    preflight_checks
    while true; do
        check_background_process
        find "$CONFIRM_DIR" -type f -mmin +20 -delete || true
        local OFFSET; OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
        local RESP; RESP=$(curl_post "${TELEGRAM_API}/getUpdates?offset=$((OFFSET + 1))&timeout=30" || "")
        if [ -z "$RESP" ]; then sleep 1; continue; fi
        # iterate results
        echo "$RESP" | jq -c '.result[]' 2>/dev/null | while read -r item; do
            # handle callback_query first
            local cbq; cbq=$(echo "$item" | jq -c '.callback_query' 2>/dev/null || echo "null")
            if [ "$cbq" != "null" ]; then
                local cb_id; cb_id=$(echo "$cbq" | jq -r '.id' 2>/dev/null || echo "")
                local cb_data; cb_data=$(echo "$cbq" | jq -r '.data' 2>/dev/null || echo "")
                local cb_user; cb_user=$(echo "$cbq" | jq -r '.from.id' 2>/dev/null || echo "")
                if [ "$cb_user" = "$TELEGRAM_CHAT_ID" ]; then
                    pro_log "INFO" "Callback query received: $cb_data"
                    # dispatch terminal callbacks starting with term:
                    case "$cb_data" in
                        term:*) handle_terminal_callback "$cb_id" "$cb_data" ;;
                        *) answerCallback "$cb_id" "Unknown action." ;;
                    esac
                else
                    pro_log "WARN" "Unauthorized callback from $cb_user"
                fi
                local update_id; update_id=$(echo "$item" | jq -r '.update_id' 2>/dev/null || echo "")
                echo "$update_id" > "$OFFSET_FILE"
                continue
            fi

            local msg; msg=$(echo "$item" | jq -c '.message' 2>/dev/null || echo "null")
            if [ "$msg" = "null" ]; then
                local u_id; u_id=$(echo "$item" | jq -r '.update_id' 2>/dev/null || echo "")
                echo "$u_id" > "$OFFSET_FILE"
                continue
            fi
            local chat_id; chat_id=$(echo "$msg" | jq -r '.chat.id' 2>/dev/null || echo "")
            local text; text=$(echo "$msg" | jq -r '.text' 2>/dev/null || echo "")
            local reply_to; reply_to=$(echo "$msg" | jq -c '.reply_to_message' 2>/dev/null || echo "")
            if [ "$chat_id" = "$TELEGRAM_CHAT_ID" ]; then
                pro_log "INFO" "Received: $text"
                read -r command arg1 arg2 <<<"$text" || true
                case "$command" in
                    "/start"|"/help") sendMessage "*Welcome.* Use /terminal to open terminal manager. Use /help for commands."; ;;
                    "/status") get_system_status ;;
                    "/checkupdates") check_updates ;;
                    "/runupdates") run_updates ;;
                    "/top") manage_processes "top" ;;
                    "/getlog") get_log_file "$arg1" ;;
                    "/ufw") manage_firewall "$arg1" "$arg2" ;;
                    "/user") manage_user "$arg1" "$arg2" ;;
                    "/kill") manage_processes "kill" "$arg1" ;;
                    "/upload") upload_file_to_telegram "$arg1" ;;
                    "/download") download_file_from_telegram "$reply_to" ;;
                    "/reboot") create_confirmation "/sbin/reboot" ;;
                    "/shutdown") create_confirmation "/sbin/shutdown -h now" ;;
                    "/confirm") execute_confirmation "$arg1" ;;
                    "/selfupdate") self_update_script ;;
                    "/terminal") terminal_main_menu ;;
                    "/shell") start_shell_session ;;
                    "/exit") if is_shell_active; then stop_shell_session; sendMessage "Shell terminated."; else sendMessage "No active shell."; fi ;;
                    "/cmd") shift; shift || true; run_ad_hoc_command "${text# /cmd }" ;; # run whole remainder
                    *)
                        if is_shell_active; then execute_shell_command "$text"; else sendMessage "❓ Unknown. Use /help or /terminal." ; fi
                        ;;
                esac
            else
                pro_log "WARN" "Unauthorized message from $chat_id"
            fi
            local u_id; u_id=$(echo "$item" | jq -r '.update_id' 2>/dev/null || echo "")
            echo "$u_id" > "$OFFSET_FILE"
        done
    done
}

# -------------------------
# Systemd installer
# -------------------------
install_systemd_service() {
    if [ "$(id -u)" -ne 0 ]; then die "Must be root to install service"; fi
    cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=Linux Secure Manager (Telegram listener)
After=network.target

[Service]
Type=simple
ExecStart=${SCRIPT_PATH} --listen
Restart=always
RestartSec=5
User=root
Environment=LSM_DEBUG=1

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable --now linux_secure_manager.service
    pro_log "INFO" "Systemd service installed and started"
    sendMessage "✅ Service installed and started (linux_secure_manager.service)."
}

# -------------------------
# Setup wizard
# -------------------------
run_setup_wizard() {
    if [ "$(id -u)" -ne 0 ]; then die "Setup must be run as root."; fi
    clear
    echo -e "${C_BOLD}--- Linux Secure Manager Setup Wizard ---${C_RESET}"
    preflight_checks
    local new_bot new_chat new_auto
    while [ -z "${new_bot:-}" ]; do read -rp "Enter your Telegram Bot Token: " new_bot || true; if ! [[ "$new_bot" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then echo "Token format invalid"; new_bot=""; fi; done
    echo "[INFO] To find Chat ID, send message to bot and visit: https://api.telegram.org/bot${new_bot}/getUpdates"
    while ! [[ "${new_chat:-}" =~ ^[0-9]+$ ]]; do read -rp "Enter your numerical Telegram Chat ID: " new_chat || true; done
    read -rp "Enable scheduled automatic updates? (y/n) [default: y]: " new_auto || true
    new_auto="${new_auto:-y}"
    if [[ "$new_auto" =~ ^[Yy]$ ]]; then new_auto=1; else new_auto=0; fi
    TELEGRAM_BOT_TOKEN="$new_bot"
    TELEGRAM_CHAT_ID="$new_chat"
    ENABLE_AUTO_MAINTENANCE="$new_auto"
    # verify write access before saving
    if ! touch "${CONFIG_FILE}.test" >/dev/null 2>&1; then
        pro_log "ERROR" "Cannot write to $(dirname "$CONFIG_FILE") - permission or immutable flag?"
        echo "[ERROR] Cannot write to $(dirname "$CONFIG_FILE"). Check filesystem and attributes. See $DEBUG_LOG"
        # collect some diagnostics
        lsattr "$(dirname "$CONFIG_FILE")" 2>/dev/null >> "$DEBUG_LOG" || true
        stat "$(dirname "$CONFIG_FILE")" >> "$DEBUG_LOG" 2>/dev/null || true
        rm -f "${CONFIG_FILE}.test" || true
        exit 1
    fi
    rm -f "${CONFIG_FILE}.test"
    # Save
    save_config_atomic
    load_config
    chmod +x "$SCRIPT_PATH"
    pro_log "INFO" "Setup saved: chat=$TELEGRAM_CHAT_ID"
    echo "[SUCCESS] Configuration saved to $CONFIG_FILE"
    # cron option fallback: recommend systemd
    read -rp "Install systemd service for auto-start? (y/n) [default: y]: " install_sv || true
    install_sv="${install_sv:-y}"
    if [[ "$install_sv" =~ ^[Yy]$ ]]; then install_systemd_service; fi
    sendMessage "✅ Linux Secure Manager configured on $(hostname)."
    exit 0
}

# -------------------------
# Main
# -------------------------
main() {
    if [ "$(id -u)" -ne 0 ]; then echo "This script must be run as root." >&2; exit 1; fi
    case "${1:-}" in
        --listen) listen_for_commands ;;
        --setup) run_setup_wizard ;;
        --install-service) install_systemd_service ;;
        --selfupdate) self_update_script ;;
        --check) get_system_status ;;
        --help|*) run_setup_wizard ;;
    esac
}

main "$@"
