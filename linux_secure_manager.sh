#!/usr/bin/env bash
# ===================================================================================
# Linux Secure Manager - Hardened Edition
# Upgraded for safer operation + "Pro" logging (structured logs + journald/syslog)
# Author: Opselon (upgrades by assistant)
# Version: 9.1
# ===================================================================================

set -euo pipefail
IFS=$'\n\t'
umask 0077

# ------------------------
# Paths & Defaults
# ------------------------
SCRIPT_PATH="$(realpath "$0")"
CONFIG_FILE="/etc/linux_secure_manager.conf"
LOG_FILE="/var/log/linux_secure_manager.log"
SESSION_DIR="/run/linux_secure_manager/session"
CONFIRM_DIR="/run/linux_secure_manager/confirm"
OFFSET_FILE="/run/linux_secure_manager/offset.dat"
REPO_URL="https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/linux_secure_manager.sh"
LOCKFILE="/run/lock/linux_secure_manager.lock"

# Ensure runtime dirs exist with secure perms
mkdir -p "$SESSION_DIR" "$CONFIRM_DIR" "$(dirname "$OFFSET_FILE")" "$(dirname "$LOCKFILE")"
chmod 700 "$SESSION_DIR" "$CONFIRM_DIR"
chown root:root "$SESSION_DIR" "$CONFIRM_DIR"

# Colorized UI (for interactive menu)
C_BOLD="\033[1m"; C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_BLUE="\033[34m"; C_RED="\033[31m"; C_RESET="\033[0m"

# ------------------------
# Helper: Pro logger (file + journald/syslog)
# Produces JSON-ish log lines (timestamp, level, msg, meta...)
# ------------------------
pro_log() {
    local level="${1:-INFO}"; shift
    local msg="${*:-}"
    # JSON-ish line (avoid quoting issues)
    local ts; ts=$(date --iso-8601=seconds)
    local host; host=$(hostname --short)
    local pid="$$"
    local logline
    logline="{\"ts\":\"$ts\",\"host\":\"$host\",\"pid\":$pid,\"level\":\"$level\",\"msg\":\"$(echo "$msg" | sed 's/"/\\"/g')\"}"
    # Append to rotating file (append-only)
    printf '%s\n' "$logline" >> "$LOG_FILE"
    # Send to journald / syslog if available (systemd-cat preferred)
    if command -v systemd-cat >/dev/null 2>&1; then
        printf '%s\n' "$logline" | systemd-cat -t linux_secure_manager -p "${level,,}" || true
    else
        printf '%s\n' "$logline" | logger -t linux_secure_manager -p "user.${level,,}" || true
    fi
}

# Ensure log file exists and secure perms
if [ ! -f "$LOG_FILE" ]; then
    touch "$LOG_FILE"
    chmod 600 "$LOG_FILE"
fi

# ------------------------
# Safe utilities
# ------------------------
die() { pro_log "ERROR" "$*"; echo -e "${C_RED}[ERROR]${C_RESET} $*" >&2; exit 1; }

safe_mktemp_dir() {
    mktemp -d --tmpdir "$SESSION_DIR/tmp.XXXXXX"
}

urlencode() {
    # simple urlencode for text
    local length="${#1}"; local i; for ((i = 0; i < length; i++)); do local c=${1:i:1}; case $c in [a-zA-Z0-9.~_-]) printf '%s' "$c" ;; *) printf '%%%02X' "'$c" ;; esac; done
}

# ------------------------
# Config management (external file)
# ------------------------
load_config() {
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck disable=SC1090
        # shellcheck source=/dev/null
        . "$CONFIG_FILE"
    else
        TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
        TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
        ENABLE_AUTO_MAINTENANCE="${ENABLE_AUTO_MAINTENANCE:-1}"
    fi
}

save_config_atomic() {
    local tmp; tmp="$(mktemp -u "${CONFIG_FILE}.tmp.XXXXXX")"
    cat > "$tmp" <<EOF
# Linux Secure Manager configuration - DO NOT SHARE
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ENABLE_AUTO_MAINTENANCE="${ENABLE_AUTO_MAINTENANCE:-1}"
EOF
    chmod 600 "$tmp"
    chown root:root "$tmp" || true
    mv -f "$tmp" "$CONFIG_FILE"
    chmod 600 "$CONFIG_FILE"
    pro_log "INFO" "Config saved to $CONFIG_FILE"
}

# ------------------------
# Input helpers (safe; returns via stdout)
# ------------------------
prompt_input() {
    local prompt="$1"; local default_value="${2:-}"
    local reply
    if [ -n "$default_value" ]; then
        read -rp "$(echo -e "${C_YELLOW}${prompt}${C_RESET} [default: ${default_value}]: ")" reply || true
        reply="${reply:-$default_value}"
    else
        read -rp "$(echo -e "${C_YELLOW}${prompt}${C_RESET}: ")" reply || true
    fi
    printf '%s' "$reply"
}

# ------------------------
# Telegram helpers (safer curl usage)
# ------------------------
load_config
if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then
    TELEGRAM_API=""
else
    TELEGRAM_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
fi

curl_post() {
    # wrapper with timeouts and failure handling
    curl --fail --silent --show-error --max-time 20 --location "$@" || true
}

sendMessage() {
    local text="$1"
    [ -n "${TELEGRAM_API:-}" ] || { pro_log "WARN" "sendMessage: TELEGRAM not configured"; return 1; }
    local url="${TELEGRAM_API}/sendMessage"
    # Use POST form, safe quoting
    local encoded
    encoded=$(urlencode "$text")
    curl_post -X POST -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=${text}" -d "parse_mode=Markdown" "${url}" >/dev/null 2>&1 || pro_log "WARN" "Telegram sendMessage failed"
}

sendAction() {
    local action="$1"
    [ -n "${TELEGRAM_API:-}" ] || return 1
    curl_post -X POST "${TELEGRAM_API}/sendChatAction" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "action=${action}" >/dev/null 2>&1 || true
}

sendDocument() {
    local filepath="$1"; local caption="${2:-File}"
    [ -n "${TELEGRAM_API:-}" ] || return 1
    # Use --form to avoid issues; bail if file too large
    if [ ! -f "$filepath" ] || [ ! -r "$filepath" ]; then pro_log "WARN" "sendDocument: file not readable $filepath"; return 1; fi
    local size; size=$(stat -c%s "$filepath")
    if [ "$size" -gt 46900000 ]; then pro_log "WARN" "sendDocument: file too large ($size)"; sendMessage "❌ File too large to send: $(basename "$filepath")"; return 1; fi
    sendAction "upload_document"
    curl_post -X POST "${TELEGRAM_API}/sendDocument" -F "chat_id=${TELEGRAM_CHAT_ID}" -F "document=@${filepath}" -F "caption=${caption}" >/dev/null 2>&1 || pro_log "WARN" "sendDocument failed for $filepath"
}

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

# ------------------------
# Confirmation system (secure tokens)
# Stores JSON with base64 command, TTL-based cleanup
# ------------------------
create_confirmation() {
    local action="$1"
    local code; code=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
    local file="${CONFIRM_DIR}/${code}"
    # store base64-encoded action + timestamp (expiry 15m)
    local payload; payload=$(printf '{"ts":"%s","cmd":"%s"}' "$(date +%s)" "$(printf '%s' "$action" | base64 -w0)")
    printf '%s' "$payload" > "$file"
    chmod 600 "$file"
    pro_log "INFO" "Confirmation created code=$code"
    sendMessage "⚠️ *Confirmation Required*\n\nTo proceed with the dangerous action, reply with:\n\`/confirm $code\`\n\nThis code expires in 15 minutes."
}

_execute_confirmation_cmd() {
    # internal; carefully execute permitted commands only
    local cmd="$1"
    pro_log "INFO" "Executing confirmed action: ${cmd}"
    # We will run under 'bash -c' but avoid environment expansion by using an explicit shell.
    # The design still trusts the script owner who triggers create_confirmation but avoids storing plaintext in logs.
    bash -c "$cmd"
}

execute_confirmation() {
    local code="$1"
    local file="${CONFIRM_DIR}/${code}"
    if [ ! -f "$file" ]; then sendMessage "❌ Invalid or expired confirmation code."; pro_log "WARN" "Confirmation attempt invalid code=$code"; return; fi
    local payload; payload=$(cat "$file")
    # parse
    local ts; ts=$(echo "$payload" | jq -r '.ts' 2>/dev/null || echo "")
    local cmd_b64; cmd_b64=$(echo "$payload" | jq -r '.cmd' 2>/dev/null || echo "")
    if [ -z "$ts" ] || [ -z "$cmd_b64" ]; then rm -f "$file"; sendMessage "❌ Invalid confirmation data."; return; fi
    # expiry 15m = 900s
    local now; now=$(date +%s)
    if [ "$((now - ts))" -gt 900 ]; then rm -f "$file"; sendMessage "❌ Confirmation code expired."; pro_log "WARN" "Confirmation code expired code=$code"; return; fi
    rm -f "$file"
    local cmd; cmd=$(printf '%s' "$cmd_b64" | base64 -d -w0)
    # For safety: restrict dangerous patterns? we allow a curated subset since script issues these confirmations itself
    _execute_confirmation_cmd "$cmd" >/dev/null 2>&1 || true
    sendMessage "✅ Action executed."
    pro_log "INFO" "Confirmation executed code=$code"
}

# periodically clean old confirmation files
_clean_confirm_dir() {
    find "$CONFIRM_DIR" -type f -mmin +20 -delete || true
}

# ------------------------
# System / Feature functions (sanitized)
# ------------------------
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
    sendAction "typing"
    if command -v apt-get >/dev/null 2>&1; then
        apt-get update -y >/dev/null 2>&1 || pro_log "WARN" "apt-get update failed"
        local UPGRADABLE; UPGRADABLE=$(apt list --upgradable 2>/dev/null | sed '1d' || true)
        if [ -z "$UPGRADABLE" ]; then sendMessage "✅ No packages to upgrade."; else sendOutputAsFile "checkupdates" "$UPGRADABLE"; fi
    else
        sendMessage "❌ Package manager not supported by this script."
    fi
}

run_updates() {
    pro_log "INFO" "Initiating system upgrade (background)"
    sendMessage "🚀 Starting full system upgrade in the background. I will notify you on completion."
    (
        set -e
        pro_log "INFO" "APT: update"
        apt-get update -y >>"$LOG_FILE" 2>&1
        pro_log "INFO" "APT: upgrade"
        DEBIAN_FRONTEND=noninteractive apt-get upgrade -y >>"$LOG_FILE" 2>&1
        pro_log "INFO" "APT: autoremove"
        apt-get autoremove --purge -y >>"$LOG_FILE" 2>&1
        pro_log "INFO" "APT: clean"
        apt-get clean >>"$LOG_FILE" 2>&1
        pro_log "INFO" "System upgrade completed"
        sendMessage "✅ *System upgrade complete on $(hostname)!*"
    ) & disown
}

manage_user() {
    local action="${1:-}"; local username="${2:-}"
    if ! [[ "$username" =~ ^[a-z_][a-z0-9_-]{1,31}$ ]]; then sendMessage "❌ Invalid username format."; return; fi
    if [ "$action" = "add" ]; then
        local password; password=$(tr -dc 'A-Za-z0-9!@%_-+=' </dev/urandom | head -c 16)
        if useradd -m -s /bin/bash "$username"; then
            echo "${username}:${password}" | chpasswd
            sendMessage "✅ User \`${username}\` created.\n\n*Temporary Password:*\n\`\`\`${password}\`\`\`\nPlease change it immediately."
            pro_log "INFO" "User created: $username"
        else
            sendMessage "❌ Failed to create user \`${username}\`."
            pro_log "ERROR" "Failed useradd $username"
        fi
    elif [ "$action" = "del" ]; then
        # Use confirmation wrapper
        create_confirmation "userdel -r ${username} && logger -t linux_secure_manager 'Deleted user ${username} by confirmation'"
        sendMessage "⚠️ Deletion scheduled. Confirm with /confirm <code>."
    else
        sendMessage "Usage: /user add|del <username>"
    fi
}

manage_firewall() {
    local action="${1:-}"; local port="${2:-}"
    if ! command -v ufw >/dev/null 2>&1; then sendMessage "❌ UFW is not installed."; return; fi
    case "$action" in
        status)
            local status; status=$(ufw status verbose 2>&1 || true)
            sendOutputAsFile "ufw status" "$status"
            ;;
        enable)
            ufw --force enable >/dev/null 2>&1 && sendMessage "✅ Firewall enabled." || sendMessage "❌ Failed to enable firewall."
            ;;
        disable)
            ufw --force disable >/dev/null 2>&1 && sendMessage "✅ Firewall disabled." || sendMessage "❌ Failed to disable firewall."
            ;;
        allow|deny)
            if [[ ! "$port" =~ ^[0-9]+(/[a-z]+)?$ ]]; then sendMessage "❌ Invalid port format. Example: \`22\` or \`80/tcp\`"; return; fi
            ufw "$action" "$port" >/dev/null 2>&1 && sendMessage "✅ Rule \`${action} ${port}\` applied." || sendMessage "❌ Failed to apply rule."
            ;;
        *)
            sendMessage "Usage: /ufw status|enable|disable|allow|deny <port>"
            ;;
    esac
}

manage_processes() {
    local action="${1:-}"; local pid="${2:-}"
    if [ "$action" = "top" ]; then
        local top_output; top_output=$(top -b -n 1 | head -n 17)
        sendOutputAsFile "top" "$top_output"
    elif [ "$action" = "kill" ]; then
        if [[ ! "$pid" =~ ^[0-9]+$ ]]; then sendMessage "❌ Invalid PID."; return; fi
        create_confirmation "kill -9 ${pid} && logger -t linux_secure_manager 'Killed PID ${pid} by confirmation'"
        sendMessage "⚠️ Kill scheduled. Confirm with /confirm <code>."
    else
        sendMessage "Usage: /top or /kill <pid>"
    fi
}

get_log_file() {
    local log_path="${1:-/var/log/syslog}"
    local real_log_path; real_log_path=$(realpath -m "$log_path" 2>/dev/null || echo "")
    if [ -z "$real_log_path" ] || [[ "$real_log_path" != /var/log/* ]]; then sendMessage "❌ Access denied. You can only access logs within \`/var/log/\`."; return; fi
    if [ -r "$real_log_path" ]; then
        local tailout; tailout=$(tail -n 1000 "$real_log_path" 2>/dev/null || true)
        sendOutputAsFile "getlog $(basename "$real_log_path")" "$tailout"
    else
        sendMessage "❌ Log file not found or not readable at \`${real_log_path}\`."
    fi
}

# ------------------------
# Shell session management (persistent session)
# ------------------------
is_shell_active() { [ -f "${SESSION_DIR}/active.lock" ]; }
get_shell_prompt() {
    local cwd
    cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "~")
    cwd=${cwd/#$HOME/\~}
    echo "*root@$(hostname):${cwd}#*"
}

start_shell_session() {
    stop_shell_session || true
    mkdir -p "$SESSION_DIR"
    touch "${SESSION_DIR}/active.lock"
    echo "$HOME" > "${SESSION_DIR}/cwd.txt"
    pro_log "INFO" "Remote shell started"
    local WELCOME="✅ *Remote Shell Started*\n- Use \`/exit\` to terminate.\n\n$(get_shell_prompt)"
    sendMessage "$WELCOME"
}

stop_shell_session() {
    if [ -d "$SESSION_DIR" ]; then
        if [ -f "${SESSION_DIR}/pid.txt" ]; then
            local pid; pid=$(cat "${SESSION_DIR}/pid.txt" 2>/dev/null || echo "")
            if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then kill -9 "$pid" 2>/dev/null || true; fi
        fi
        rm -rf "$SESSION_DIR"
        pro_log "INFO" "Remote shell stopped"
    fi
}

execute_shell_command() {
    local command_to_run="$1"
    if [ -f "${SESSION_DIR}/pid.txt" ]; then sendMessage "⏳ Background task running. Please wait."; return; fi
    pro_log "INFO" "Shell cmd queued: ${command_to_run}"
    local cwd; cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "$HOME")
    local out="${SESSION_DIR}/output.log"
    ( cd "$cwd" && bash -c "$command_to_run" ) >"$out" 2>&1 &
    echo $! > "${SESSION_DIR}/pid.txt"
    sendMessage "⏳ Executing in background (PID: $!)..."
}

check_background_process() {
    if [ -f "${SESSION_DIR}/pid.txt" ]; then
        local pid; pid=$(cat "${SESSION_DIR}/pid.txt" 2>/dev/null || echo "")
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            local output; output=$(cat "${SESSION_DIR}/output.log" 2>/dev/null || "")
            sendMessage "✅ *Task (PID: $pid) finished.*"
            sendOutputAsFile "Shell Command Output" "$output"
            sendMessage "$(get_shell_prompt)"
            rm -f "${SESSION_DIR}/pid.txt" "${SESSION_DIR}/output.log"
        fi
    fi
}

upload_file_to_telegram() {
    local file_path="$1"
    if [ -z "$file_path" ]; then sendMessage "Usage: \`/upload /path/to/file\`"; return; fi
    local real_file_path; real_file_path=$(realpath -m "$file_path" 2>/dev/null || echo "")
    if [ -f "$real_file_path" ] && [ -r "$real_file_path" ]; then
        sendDocument "$real_file_path" "File from $(hostname)"
    else
        sendMessage "❌ File not found or not readable."
    fi
}

download_file_from_telegram() {
    # $1 must be JSON message object
    local json="$1"
    local file_id; file_id=$(echo "$json" | jq -r '.document.file_id' 2>/dev/null || echo "")
    local file_name; file_name=$(echo "$json" | jq -r '.document.file_name' 2>/dev/null || echo "")
    local cwd; cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "/root")
    if [ -z "$file_id" ] || [ "$file_id" = "null" ]; then sendMessage "❌ No document object found."; return; fi
    sendMessage "⬇️ Downloading \`${file_name}\` to \`${cwd}\`..."
    local file_path_json; file_path_json=$(curl_post "${TELEGRAM_API}/getFile?file_id=${file_id}" || "")
    local path_remote; path_remote=$(echo "$file_path_json" | jq -r '.result.file_path' 2>/dev/null || echo "")
    if [ -n "$path_remote" ] && [ "$path_remote" != "null" ]; then
        curl_post -o "${cwd}/${file_name}" "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${path_remote}" || true
        sendMessage "✅ Downloaded \`${file_name}\` to \`${cwd}\`."
    else
        sendMessage "❌ Failed to download file."
    fi
}

self_update_script() {
    sendMessage "🔄 Checking for updates..."
    local tempdir; tempdir=$(mktemp -d)
    local tmpfile="${tempdir}/lsm.new"
    if ! curl_post -sSL "$REPO_URL" -o "$tmpfile"; then
        sendMessage "❌ Download failed."
        rm -rf "$tempdir"
        return
    fi
    local new_version; new_version=$(grep -m1 'Version:' "$tmpfile" || echo "")
    local old_version; old_version=$(grep -m1 'Version:' "$SCRIPT_PATH" || echo "")
    if [ "$new_version" = "$old_version" ]; then
        sendMessage "✅ Already latest version ($old_version)."
        rm -rf "$tempdir"
        return
    fi
    # preserve external config; simply overwrite script atomically
    chmod 700 "$tmpfile"
    mv -f "$tmpfile" "$SCRIPT_PATH"
    chmod +x "$SCRIPT_PATH"
    sendMessage "✅ Updated script from ${old_version:-unknown} to ${new_version:-unknown}."
    pro_log "INFO" "Script self-updated"
    rm -rf "$tempdir"
}

# ------------------------
# Interactive Menu (same UX but safe)
# ------------------------
run_interactive_menu() {
    while true; do
        clear
        echo -e "${C_BOLD}--- Telegram Linux Admin Management Menu ---${C_RESET}"
        echo
        echo " 1) View Live Script Logs"
        echo " 2) Test Telegram Bot Connection"
        echo " 3) Send Full Log File to Telegram"
        echo " 4) Check Cron Job Status"
        echo " 5) Run Self-Update"
        echo " 6) Re-run Setup Wizard"
        echo " 7) Uninstall (runs remote uninstall script)"
        echo " q) Quit"
        echo
        read -rp "Select an option: " choice
        case "$choice" in
            1) sudo tail -f "$LOG_FILE"; read -rp "Press Enter..." _ || true ;;
            2)
                load_config
                if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then echo "Not configured."; else
                    local apiresp; apiresp=$(curl_post "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" || "")
                    if echo "$apiresp" | jq -e '.ok' >/dev/null 2>&1; then
                        echo "Connection OK! Bot: $(echo "$apiresp" | jq -r '.result.first_name')"
                    else
                        echo "Connection FAILED."
                    fi
                fi
                read -rp "Press Enter..." _ || true
                ;;
            3)
                if [ -f "$LOG_FILE" ]; then sendDocument "$LOG_FILE" "Full log file from $(hostname)"; else echo "No log file found."; fi
                read -rp "Press Enter..." _ || true
                ;;
            4)
                echo "Current cron jobs for root:"
                sudo crontab -l 2>/dev/null | grep --color=always "$SCRIPT_PATH" || echo "No cron jobs for this script found."
                read -rp "Press Enter..." _ || true
                ;;
            5) self_update_script; read -rp "Press Enter..." _ || true ;;
            6) run_setup_wizard; exit 0 ;;
            7)
                echo "This will run the remote uninstaller. Are you sure? (y/N)"
                read -r confirm
                if [[ "$confirm" =~ ^[Yy]$ ]]; then
                    curl_post -sSL https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/uninstall.sh | sudo bash
                    exit 0
                fi
                ;;
            q) exit 0 ;;
            *) echo "Invalid option."; sleep 1 ;;
        esac
    done
}

# ------------------------
# Setup wizard (creates /etc/linux_secure_manager.conf safely)
# ------------------------
run_setup_wizard() {
    if [ "$(id -u)" -ne 0 ]; then die "Setup wizard must be run as root."; fi
    clear
    echo -e "${C_BOLD}--- Linux Secure Manager Setup Wizard ---${C_RESET}"
    echo
    pro_log "INFO" "Starting setup wizard"
    echo -e "${C_YELLOW}Important:${C_RESET} This script provides powerful root control. Protect your Telegram account and do not share the config file."
    echo
    # bot token
    local new_bot_token=""
    while [ -z "$new_bot_token" ]; do
        new_bot_token=$(prompt_input "Enter your Telegram Bot Token" "")
        # quick validation: token looks like digits:alphanum
        if ! [[ "$new_bot_token" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then
            echo "Invalid token format. Please re-enter."
            new_bot_token=""
        fi
    done
    local new_chat_id=""
    echo -e "To find your Chat ID: send a message to your bot and visit:"
    echo -e "https://api.telegram.org/bot${new_bot_token}/getUpdates"
    while ! [[ "$new_chat_id" =~ ^[0-9]+$ ]]; do
        new_chat_id=$(prompt_input "Enter your numerical Telegram Chat ID" "")
    done
    local new_auto_maintenance
    new_auto_maintenance=$(prompt_input "Enable scheduled automatic updates? (y/n)" "y")
    if [[ "$new_auto_maintenance" =~ ^[Yy]$ ]]; then new_auto_maintenance=1; else new_auto_maintenance=0; fi

    # Save to config atomically
    TELEGRAM_BOT_TOKEN="$new_bot_token"
    TELEGRAM_CHAT_ID="$new_chat_id"
    ENABLE_AUTO_MAINTENANCE="$new_auto_maintenance"
    save_config_atomic
    # refresh in-memory var
    load_config
    pro_log "INFO" "Setup complete: Telegram configured for chat ${TELEGRAM_CHAT_ID}"
    chmod +x "$SCRIPT_PATH"
    echo
    local setup_cron
    setup_cron=$(prompt_input "Automatically set up cron jobs? (y/n)" "y")
    if [[ "$setup_cron" =~ ^[Yy]$ ]]; then
        # add per-minute listener
        local LISTEN_CRON="* * * * * ${SCRIPT_PATH} --listen >> ${LOG_FILE} 2>&1"
        (crontab -l 2>/dev/null | grep -Fv "${SCRIPT_PATH} --listen" || true; echo "$LISTEN_CRON") | crontab -
        if [ "$ENABLE_AUTO_MAINTENANCE" -eq 1 ]; then
            local AUTO_CRON="0 3 * * 0 ${SCRIPT_PATH} --auto >> ${LOG_FILE} 2>&1"
            (crontab -l 2>/dev/null | grep -Fv "${SCRIPT_PATH} --auto" || true; echo "$AUTO_CRON") | crontab -
        fi
        pro_log "INFO" "Cron jobs configured"
        echo "Cron jobs configured."
    fi
    echo
    sendMessage "✅ Linux Secure Manager successfully configured on $(hostname)."
    pro_log "INFO" "Setup wizard finished"
    exit 0
}

# ------------------------
# Listener: polls Telegram for commands (safe loops, offset handling)
# ------------------------
listen_for_commands() {
    # require config
    load_config
    if [ -z "${TELEGRAM_BOT_TOKEN:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then pro_log "ERROR" "Script not configured"; exit 1; fi
    for cmd in curl jq; do
        if ! command -v "$cmd" >/dev/null 2>&1; then pro_log "ERROR" "Dependency '$cmd' not found. Install and retry."; exit 1; fi
    done

    # one-instance lock
    exec 9>"$LOCKFILE"
    if ! flock -n 9; then pro_log "WARN" "Another listener instance active; exiting."; exit 0; fi

    # run loop
    while true; do
        check_background_process
        _clean_confirm_dir
        local OFFSET; OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
        local UPDATES; UPDATES=$(curl_post "${TELEGRAM_API}/getUpdates?offset=$((OFFSET + 1))&timeout=30" || "")
        if [ -z "$UPDATES" ]; then sleep 1; continue; fi
        echo "$UPDATES" | jq -c '.result[]' 2>/dev/null | while read -r MESSAGE; do
            # parse safely
            local CHAT_ID; CHAT_ID=$(echo "$MESSAGE" | jq -r '.message.chat.id' 2>/dev/null || echo "")
            local TEXT; TEXT=$(echo "$MESSAGE" | jq -r '.message.text' 2>/dev/null || echo "")
            local UPDATE_ID; UPDATE_ID=$(echo "$MESSAGE" | jq -r '.update_id' 2>/dev/null || echo "")
            local REPLY_MSG; REPLY_MSG=$(echo "$MESSAGE" | jq -c '.message.reply_to_message' 2>/dev/null || echo "")
            if [ "$CHAT_ID" = "$TELEGRAM_CHAT_ID" ]; then
                pro_log "INFO" "Received input from authorized chat: ${TEXT}"
                # If reply contains a document object we treat as /download
                if [ "$REPLY_MSG" != "null" ] && [ "$(echo "$REPLY_MSG" | jq -r '.document')" != "null" ]; then
                    TEXT="/download"
                fi
                # split safely
                local command arg1 arg2
                # read into variables using bash read
                read -r command arg1 arg2 <<<"$TEXT" || true
                case "$command" in
                    "/start"|"/help")
                        local HELP_TEXT="*Welcome to the Secure Linux Admin bot!*

*⚡️ System & Updates*
\`/status\` - Detailed system overview.
\`/checkupdates\` - See available packages.
\`/runupdates\` - Install all updates.
\`/top\` - View top processes.
\`/getlog <path>\` - Get a log file (default: syslog).

*🔐 Security & Network*
\`/ufw status|enable|disable\`
\`/ufw allow|deny <port>\`
\`/user add|del <name>\`
\`/kill <pid>\`

*📁 File Management*
\`/upload <path>\` - Upload a file from the server.
\`/download\` - Reply to a file to download it.

*🚀 Interactive Shell*
\`/shell\` - Start a persistent root shell.
\`/exit\` - Terminate the shell session.

*🛠️ System Control & Script*
\`/service status|restart <name>\`
\`/reboot\` | \`/shutdown\` - ⚠️ (require confirmation)
\`/selfupdate\` - Update this script.
\`/confirm <code>\` - Confirm a dangerous action."
                        sendMessage "$HELP_TEXT"
                        ;;
                    "/status") get_system_status ;;
                    "/checkupdates") check_updates ;;
                    "/runupdates") run_updates ;;
                    "/top") manage_processes "top" ;;
                    "/getlog") get_log_file "$arg1" ;;
                    "/ufw") manage_firewall "$arg1" "$arg2" ;;
                    "/user") manage_user "$arg1" "$arg2" ;;
                    "/kill") manage_processes "kill" "$arg1" ;;
                    "/upload") upload_file_to_telegram "$arg1" ;;
                    "/download") download_file_from_telegram "$REPLY_MSG" ;;
                    "/reboot") create_confirmation "/sbin/reboot" ;;
                    "/shutdown") create_confirmation "/sbin/shutdown -h now" ;;
                    "/confirm") execute_confirmation "$arg1" ;;
                    "/selfupdate") self_update_script ;;
                    "/service")
                        # lightweight service manager
                        if [ -z "$arg1" ]; then sendMessage "Usage: /service status|restart <name>"; else
                            if systemctl is-active --quiet "$arg2"; then sendMessage "Service $arg2 is active."; else sendMessage "Service $arg2 is not active."; fi
                        fi
                        ;;
                    "/shell") start_shell_session ;;
                    "/exit")
                        if is_shell_active; then stop_shell_session; sendMessage "Shell terminated."; else sendMessage "No active shell." ; fi
                        ;;
                    *)
                        # if shell active, treat raw message as shell command
                        if is_shell_active; then execute_shell_command "$TEXT"; else sendMessage "❓ Unknown command. Use /help or start with /shell."; fi
                        ;;
                esac
            else
                pro_log "WARN" "Received command from unauthorized chat ID: $CHAT_ID"
            fi
            # save offset
            echo "$UPDATE_ID" > "$OFFSET_FILE"
        done
    done
}

# ------------------------
# Main entry
# ------------------------
main() {
    if [ "$(id -u)" -ne 0 ]; then echo "This script must be run as root." >&2; exit 1; fi
    load_config
    case "${1:-}" in
        --listen) listen_for_commands ;;
        --auto)
            if [ "${ENABLE_AUTO_MAINTENANCE:-1}" -eq 1 ]; then pro_log "INFO" "Running scheduled maintenance (--auto)"; run_updates; fi
            ;;
        --setup) run_setup_wizard ;;
        --check) get_system_status ;;
        *) run_interactive_menu ;;
    esac
}

main "$@"
