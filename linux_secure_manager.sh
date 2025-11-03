#!/bin/bash

# ===================================================================================
# Linux Secure Manager - The Ultimate Edition
#
# A comprehensive, self-updating script that provides a hybrid interface for
# total server management via a secure Telegram bot.
#
# Author: Opselon
# Version: 9.0
#
# ===================================================================================

# [CONFIG_START]
TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID="YOUR_CHAT_ID"
ENABLE_AUTO_MAINTENANCE="1"
# [CONFIG_END]

set +e
# --- Core, UI & State Management ---
C_BOLD="\033[1m"; C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_BLUE="\033[34m"; C_RED="\033[31m"; C_RESET="\033[0m"
print_info() { echo -e "${C_BLUE}${C_BOLD}[INFO]${C_RESET} $1"; }
print_success() { echo -e "${C_GREEN}${C_BOLD}[SUCCESS]${C_RESET} $1"; }
print_warn() { echo -e "${C_YELLOW}${C_BOLD}[WARNING]${C_RESET} $1"; }
print_error() { echo -e "${C_RED}${C_BOLD}[ERROR]${C_RESET} $1"; }
prompt_input() {
    local prompt="$1"; local default_value="$2"; local var_name="$3"; local user_input
    if [[ -n "$default_value" ]]; then read -p "$(echo -e "${C_YELLOW}${prompt}${C_RESET} [default: ${default_value}]: ")" user_input; eval "$var_name='${user_input:-$default_value}'";
    else read -p "$(echo -e "${C_YELLOW}${prompt}${C_RESET}: ")" user_input; eval "$var_name='${user_input}'"; fi
}

LOG_FILE="/var/log/linux_secure_manager.log"; SESSION_DIR="/tmp/telegram_shell_session"; CONFIRM_DIR="/tmp/telegram_confirm"; OFFSET_FILE="/tmp/telegram_offset.dat"
SCRIPT_PATH="$(realpath "$0")"; REPO_URL="https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/linux_secure_manager.sh"
trap 'rm -rf "$SESSION_DIR" "$CONFIRM_DIR"' EXIT

log_message() { echo "$(date +"%Y-%m-%d %H:%M:%S") - $1" | tee -a "$LOG_FILE"; }

# --- Setup Wizard ---
run_setup_wizard() {
    clear; echo -e "${C_BOLD}--- Linux Secure Manager Setup Wizard ---${C_RESET}"; print_warn "This script provides a full root shell. The security of your"; print_warn "Telegram account is critical. Enable Two-Step Verification."; echo
    local new_bot_token; while [ -z "$new_bot_token" ]; do prompt_input "Enter your Telegram Bot Token" "" new_bot_token; done
    local new_chat_id; print_info "To find your Chat ID, send a message to your bot, then visit:"; print_info "https://api.telegram.org/bot${new_bot_token}/getUpdates"; while [[ ! "$new_chat_id" =~ ^[0-9]+$ ]]; do prompt_input "Enter your numerical Telegram Chat ID" "" new_chat_id; done
    local new_auto_maintenance; prompt_input "Enable scheduled automatic updates? (y/n)" "y" new_auto_maintenance; [[ "$new_auto_maintenance" =~ ^[Yy]$ ]] && new_auto_maintenance="1" || new_auto_maintenance="0"
    print_info "Attempting to save configuration..."; local start_line=$(grep -n "# \[CONFIG_START\]" "$SCRIPT_PATH" | head -1 | cut -d: -f1); local end_line=$(grep -n "# \[CONFIG_END\]" "$SCRIPT_PATH" | head -1 | cut -d: -f1)
    if [ -z "$start_line" ] || [ -z "$end_line" ]; then print_error "FATAL: Config markers are missing from the script. Please re-download it. Aborting."; exit 1; fi
    local new_config_block; new_config_block="# [CONFIG_START]\nTELEGRAM_BOT_TOKEN=\"$new_bot_token\"\nTELEGRAM_CHAT_ID=\"$new_chat_id\"\nENABLE_AUTO_MAINTENANCE=\"$new_auto_maintenance\"\n# [CONFIG_END]"
    local temp_script_file; temp_script_file=$(mktemp); head -n $((start_line - 1)) "$SCRIPT_PATH" > "$temp_script_file"; echo -e "$new_config_block" >> "$temp_script_file"; tail -n +$((end_line + 1)) "$SCRIPT_PATH" >> "$temp_script_file"
    if [ ! -s "$temp_script_file" ]; then print_error "Failed to create temp config file. Aborting."; rm -f "$temp_script_file"; exit 1; fi
    local error_output; error_output=$(mv -f "$temp_script_file" "$SCRIPT_PATH" 2>&1); if [ $? -ne 0 ]; then print_error "Config save FAILED! The system prevented the file from being overwritten."; echo; echo -e "${C_YELLOW}--- LIVE SYSTEM ERROR LOG ---${C_RESET}"; echo -e "${C_RED}$error_output${C_RESET}"; echo -e "${C_YELLOW}---------------------------${C_RESET}"; print_info "This can be caused by an 'immutable' flag. Check with: lsattr $SCRIPT_PATH"; print_error "Aborting setup."; rm -f "$temp_script_file"; exit 1; fi
    if grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$SCRIPT_PATH"; then print_error "Verification FAILED. Config was not written correctly."; print_error "Aborting setup."; exit 1; fi
    print_success "Configuration saved successfully!"; chmod +x "$SCRIPT_PATH"
    local setup_cron; prompt_input "Automatically set up cron jobs? (y/n)" "y" setup_cron
    if [[ "$setup_cron" =~ ^[Yy]$ ]]; then print_info "Setting up cron jobs..."; local LISTEN_CRON="* * * * * $SCRIPT_PATH --listen >> $LOG_FILE 2>&1"; (crontab -l 2>/dev/null | grep -Fv "$SCRIPT_PATH --listen" || true; echo "$LISTEN_CRON") | crontab -; if [ "$new_auto_maintenance" -eq 1 ]; then local AUTO_CRON="0 3 * * 0 $SCRIPT_PATH --auto >> $LOG_FILE 2>&1"; (crontab -l 2>/dev/null | grep -Fv "$SCRIPT_PATH --auto" || true; echo "$AUTO_CRON") | crontab -; fi; print_success "Cron jobs have been configured."; fi
    echo; print_success "Setup complete! Type /start in your Telegram bot."; print_info "You can run 'sudo $SCRIPT_PATH' again for a management menu."; exit 0
}

# --- Telegram API & Messaging ---
TELEGRAM_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
sendMessage() { local response; response=$(curl -s -X POST "${TELEGRAM_API}/sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=$1" -d "parse_mode=Markdown"); echo "$response" | jq -r '.result.message_id'; }
editMessage() { curl -s -X POST "${TELEGRAM_API}/editMessageText" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "message_id=$1" -d "text=$2" -d "parse_mode=Markdown" > /dev/null; }
sendAction() { curl -s -X POST "${TELEGRAM_API}/sendChatAction" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "action=$1" > /dev/null; }
sendOutputAsFile() {
    local command_name="$1"; local output="$2"
    local temp_file; temp_file=$(mktemp --suffix=.txt)
    echo -e "Output for command: $command_name\nHostname: $(hostname)\nDate: $(date)\n\n" > "$temp_file"
    echo "$output" >> "$temp_file"
    sendAction "upload_document"
    curl -s -X POST "${TELEGRAM_API}/sendDocument" -F "chat_id=${TELEGRAM_CHAT_ID}" -F "document=@${temp_file}" -F "caption=Output for \`$command_name\`" -F "parse_mode=Markdown" > /dev/null
    rm -f "$temp_file"
}

# --- Confirmation System ---
create_confirmation() {
    local action="$1"; mkdir -p "$CONFIRM_DIR"
    local code; code=$((RANDOM % 9000 + 1000)); echo "$action" > "${CONFIRM_DIR}/${code}"
    sendMessage "⚠️ *Confirmation Required*\n\nThis is a dangerous action. To proceed, please reply with:\n\`/confirm $code\`" > /dev/null
}
execute_confirmation() {
    local code="$1"; local action_file="${CONFIRM_DIR}/${code}"; if [ -f "$action_file" ]; then local action=$(cat "$action_file"); rm -f "$action_file"; eval "$action"; else sendMessage "❌ Invalid or expired confirmation code." > /dev/null; fi
}

# --- Feature Functions ---
get_system_status() { local msg_id=$(sendMessage "🔄 Fetching system status..."); local UPTIME=$(uptime -p); local MEM=$(free -m | awk 'NR==2{printf "%.2f%%", $3*100/$2 }'); local DISK=$(df -h / | awk 'NR==2{printf "%s", $5}'); local LOAD=$(uptime | awk -F'load average:' '{print $2}' | sed 's/ //g'); local USERS=$(who | wc -l); local STATUS_REPORT="*Server Status: $(hostname)*\n\`\`\`\n- Uptime:         ${UPTIME}\n- Memory Usage:   ${MEM}\n- Disk Usage (/): ${DISK}\n- Logged in Users:${USERS}\n- Load Average:   ${LOAD}\n\`\`\`"; editMessage "$msg_id" "$STATUS_REPORT"; }
check_updates() { local msg_id=$(sendMessage "🔄 Checking for package updates..."); sendAction "typing"; apt-get update -y >/dev/null 2>&1; local UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -v "Listing..."); editMessage "$msg_id" "✅ Update check complete."; if [ -z "$UPGRADABLE" ]; then sendMessage "No packages to upgrade." >/dev/null; else sendOutputAsFile "checkupdates" "$UPGRADABLE"; fi; }
run_updates() { log_message "Starting full system upgrade..."; sendMessage "🚀 Starting full system upgrade in the background. I will notify you on completion." > /dev/null; ( set -e; { echo "--- APT UPDATE ---"; apt-get update -y; echo -e "\n--- APT UPGRADE ---"; apt-get upgrade -y; echo -e "\n--- APT AUTOREMOVE ---"; apt-get autoremove --purge -y; echo -e "\n--- APT CLEAN ---"; apt-get clean; } >> "$LOG_FILE" 2>&1; log_message "System upgrade completed."; sendMessage "✅ *System upgrade complete on $(hostname)!*" > /dev/null ) & }
manage_user() {
    local action="$1"; local username="$2"
    if [[ ! "$username" =~ ^[a-z_][a-z0-9_-]*$ ]]; then sendMessage "❌ Invalid username format." > /dev/null; return; fi
    if [ "$action" == "add" ]; then
        local password; password=$(< /dev/urandom tr -dc _A-Z-a-z-0-9 | head -c16)
        if useradd -m -s /bin/bash "$username" && echo "$username:$password" | chpasswd; then
            sendMessage "✅ User \`$username\` created.\n\n*Temporary Password:*\n\`\`\`$password\`\`\`\n\nPlease instruct the user to change it immediately with the \`passwd\` command." > /dev/null
        else sendMessage "❌ Failed to create user \`$username\`." > /dev/null; fi
    elif [ "$action" == "del" ]; then create_confirmation "userdel -r $username && sendMessage '✅ User \`$username\` and their home directory have been deleted.' > /dev/null"; fi
}
manage_firewall() {
    local action="$1"; local port="$2"; sendAction "typing"
    if ! command -v ufw &> /dev/null; then sendMessage "❌ UFW (firewall) is not installed." > /dev/null; return; fi
    case "$action" in
        status) local status; status=$(ufw status verbose); sendOutputAsFile "ufw status" "$status" ;;
        enable) ufw enable && sendMessage "✅ Firewall enabled." > /dev/null ;;
        disable) ufw disable && sendMessage "✅ Firewall disabled." > /dev/null ;;
        allow|deny)
            if [[ ! "$port" =~ ^[0-9]+(/[a-z]+)?$ ]]; then sendMessage "❌ Invalid port format. Example: \`22\` or \`80/tcp\`" > /dev/null; return; fi
            ufw "$action" "$port" && sendMessage "✅ Rule \`$action $port\` applied." > /dev/null ;;
    esac
}
manage_processes() {
    local action="$1"; local pid="$2"
    if [ "$action" == "top" ]; then
        local msg_id=$(sendMessage "🔄 Getting top processes..."); sendAction "typing"; local top_output; top_output=$(top -b -n 1 | head -n 17); editMessage "$msg_id" "✅ Top processes fetched."; sendOutputAsFile "top" "$top_output"
    elif [ "$action" == "kill" ]; then
        if [[ ! "$pid" =~ ^[0-9]+$ ]]; then sendMessage "❌ Invalid PID. It must be a number." > /dev/null; return; fi
        create_confirmation "kill -9 $pid && sendMessage '✅ Sent KILL signal to PID \`$pid\`.' > /dev/null"
    fi
}
get_log_file() {
    local log_path="$1"; if [ -z "$log_path" ]; then log_path="/var/log/syslog"; fi
    # Security: Prevent directory traversal and restrict to /var/log
    local real_log_path; real_log_path=$(realpath -m "$log_path")
    if [[ "$real_log_path" != /var/log/* ]] || [[ "$real_log_path" == */../* ]]; then sendMessage "❌ Access denied. You can only access logs within \`/var/log/\`." > /dev/null; return; fi
    if [ -r "$real_log_path" ]; then sendOutputAsFile "getlog $log_path" "$(tail -n 1000 "$real_log_path")"; else sendMessage "❌ Log file not found or not readable at \`$real_log_path\`." > /dev/null; fi
}

# --- Shell Session, File Management & Self-Update (Largely unchanged but included for completeness) ---
is_shell_active() { [ -f "${SESSION_DIR}/active.lock" ]; }
get_shell_prompt() { local cwd; cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "~"); cwd=${cwd/#$HOME/\~}; echo "*root@$(hostname):${cwd}#*"; }
start_shell_session() { stop_shell_session; mkdir -p "$SESSION_DIR"; touch "${SESSION_DIR}/active.lock"; echo "$HOME" > "${SESSION_DIR}/cwd.txt"; local WELCOME="✅ *Remote Shell Started*\n- Use \`/exit\` to terminate.\n\n$(get_shell_prompt)"; sendMessage "$WELCOME" > /dev/null; }
stop_shell_session() { if [ -d "$SESSION_DIR" ]; then if [ -f "${SESSION_DIR}/pid.txt" ]; then local pid=$(cat "${SESSION_DIR}/pid.txt"); if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid"; fi; fi; rm -rf "$SESSION_DIR"; fi; }
execute_shell_command() {
    local command_to_run="$1"; if [ -f "${SESSION_DIR}/pid.txt" ]; then sendMessage "⏳ Background task is running. Please wait." > /dev/null; return; fi; log_message "Shell cmd: $command_to_run"; local CWD=$(cat "${SESSION_DIR}/cwd.txt")
    ( cd "$CWD" && eval "$command_to_run" ) > "${SESSION_DIR}/output.log" 2>&1 &
    local pid=$!; echo "$pid" > "${SESSION_DIR}/pid.txt"; sendMessage "⏳ Executing in background (PID: $pid)..." > /dev/null
}
check_background_process() { if [ -f "${SESSION_DIR}/pid.txt" ]; then local pid=$(cat "${SESSION_DIR}/pid.txt"); if ! kill -0 "$pid" 2>/dev/null; then local output=$(cat "${SESSION_DIR}/output.log"); sendMessage "✅ *Task (PID: $pid) finished.*" > /dev/null; sendOutputAsFile "Shell Command" "$output"; sendMessage "$(get_shell_prompt)" > /dev/null; rm -f "${SESSION_DIR}/pid.txt" "${SESSION_DIR}/output.log"; fi; fi; }
upload_file_to_telegram() { local file_path="$1"; if [ -z "$file_path" ]; then sendMessage "Usage: \`/upload /path/to/file\`" >/dev/null; return; fi; local real_file_path=$(realpath -m "$file_path"); if [ -f "$real_file_path" ] && [ -r "$real_file_path" ]; then if [ $(stat -c%s "$real_file_path") -gt 50000000 ]; then sendMessage "❌ File > 50MB." >/dev/null; return; fi; local msg_id=$(sendMessage "⬆️ Uploading \`${real_file_path}\`..."); sendAction "upload_document"; curl -s -X POST "${TELEGRAM_API}/sendDocument" -F "chat_id=${TELEGRAM_CHAT_ID}" -F "document=@${real_file_path}" -F "caption=File from $(hostname)" > /dev/null; editMessage "$msg_id" "✅ Uploaded \`${real_file_path}\`."; else sendMessage "❌ File not found or not readable." >/dev/null; fi; }
download_file_from_telegram() {
    local file_id=$(echo "$1" | jq -r .document.file_id); local file_name=$(echo "$1" | jq -r .document.file_name); local CWD=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "/root")
    local msg_id=$(sendMessage "⬇️ Downloading \`${file_name}\` to \`${CWD}\`..."); sendAction "typing"; local file_path_json=$(curl -s "${TELEGRAM_API}/getFile?file_id=${file_id}"); local file_path=$(echo "$file_path_json" | jq -r .result.file_path)
    if [ "$file_path" != "null" ]; then curl -s "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file_path}" -o "${CWD}/${file_name}"; editMessage "$msg_id" "✅ Downloaded \`${file_name}\` to \`${CWD}\`."; else editMessage "$msg_id" "❌ Failed to download file."; fi
}
self_update_script() { local msg_id=$(sendMessage "🔄 Checking for updates..."); local temp_script="/tmp/lsm.new"; if ! curl -sSL "$REPO_URL" -o "$temp_script"; then editMessage "$msg_id" "❌ Download failed."; rm -f "$temp_script"; return; fi; local new_version=$(grep 'Version:' "$temp_script" | head -1); local old_version=$(grep 'Version:' "$SCRIPT_PATH" | head -1); if [ "$new_version" == "$old_version" ]; then editMessage "$msg_id" "✅ Already latest version ($old_version)."; rm -f "$temp_script"; return; fi; local config_block=$(sed -n '/# \[CONFIG_START\]/,/# \[CONFIG_END\]/p' "$SCRIPT_PATH"); sed -i '/# \[CONFIG_START\]/,/# \[CONFIG_END\]/c\'"$config_block" "$temp_script"; mv "$temp_script" "$SCRIPT_PATH" && chmod +x "$SCRIPT_PATH"; editMessage "$msg_id" "✅ Updated from $old_version to $new_version."; }

# --- INTERACTIVE MANAGEMENT MENU ---
run_interactive_menu() {
    # implementation unchanged from previous versions, included for completeness
    if ! grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$SCRIPT_PATH"; then local B_TOKEN=$(grep 'TELEGRAM_BOT_TOKEN=' "$SCRIPT_PATH" | cut -d'"' -f2); local C_ID=$(grep 'TELEGRAM_CHAT_ID=' "$SCRIPT_PATH" | cut -d'"' -f2); fi
    while true; do
        clear; echo -e "${C_BOLD}--- Telegram Linux Admin Management Menu ---${C_RESET}"; echo
        echo -e " 1) View Live Script Logs"; echo -e " 2) Test Telegram Bot Connection"; echo -e " 3) Send Full Log File to Telegram"; echo -e " 4) Check Cron Job Status"; echo -e " 5) Run Self-Update"; echo -e " 6) Re-run Setup Wizard"; echo -e " 7) Uninstall"; echo -e " q) Quit"; echo
        read -p "Select an option: " choice
        case $choice in
            1) sudo tail -f "$LOG_FILE"; read -p "Press Enter...";;
            2) print_info "Pinging API..."; if [ -z "$B_TOKEN" ]; then print_error "Not configured."; else API_RESPONSE=$(curl -s "https://api.telegram.org/bot${B_TOKEN}/getMe"); if echo "$API_RESPONSE" | grep -q '"ok":true'; then print_success "Connection OK! Bot: $(echo $API_RESPONSE | jq -r .result.first_name)"; else print_error "Connection FAILED."; fi; fi; read -p "Press Enter...";;
            3) print_info "Sending log file..."; if [ -z "$B_TOKEN" ]; then print_error "Not configured."; else sendAction "upload_document"; curl -s -X POST "https://api.telegram.org/bot${B_TOKEN}/sendDocument" -F "chat_id=${C_ID}" -F "document=@${LOG_FILE}" > /dev/null; print_success "Log file sent!"; fi; read -p "Press Enter...";;
            4) print_info "Current cron jobs for root:"; sudo crontab -l | grep --color=always "$SCRIPT_PATH" || print_warn "No cron jobs for this script found."; read -p "Press Enter...";;
            5) print_info "Checking for updates..."; self_update_script; read -p "Press Enter...";;
            6) run_setup_wizard; exit 0;;
            7) print_warn "This will run the uninstaller. Are you sure? (y/N)"; read confirm; if [[ "$confirm" =~ ^[Yy]$ ]]; then curl -sSL https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/uninstall.sh | sudo bash; exit 0; fi;;
            q) exit 0;;
            *) print_error "Invalid option."; sleep 1;;
        esac
    done
}

# --- MAIN & Listener ---
main() {
    if [ "$(id -u)" -ne 0 ]; then echo "This script must be run as root." >&2; exit 1; fi
    case "$1" in
        --listen) if grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$SCRIPT_PATH"; then log_message "ERROR: Script not configured."; exit 1; fi; for cmd in curl jq; do if ! command -v $cmd &> /dev/null; then log_message "ERROR: Dependency '$cmd' not found."; exit 1; fi; done; listen_for_commands ;;
        --auto) if [ "$ENABLE_AUTO_MAINTENANCE" -eq 1 ]; then log_message "Running scheduled maintenance."; run_updates; fi ;;
        --setup) run_setup_wizard ;;
        *) run_interactive_menu ;;
    esac
}
listen_for_commands() {
    check_background_process; local OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0); local UPDATES=$(curl -s "${TELEGRAM_API}/getUpdates?offset=$((OFFSET + 1))&timeout=10")
    echo "$UPDATES" | jq -c '.result[]' | while read -r MESSAGE; do
        local CHAT_ID=$(echo "$MESSAGE" | jq -r '.message.chat.id'); local TEXT=$(echo "$MESSAGE" | jq -r '.message.text'); local UPDATE_ID=$(echo "$MESSAGE" | jq -r '.update_id'); local REPLY_MSG=$(echo "$MESSAGE" | jq -r '.message.reply_to_message')
        if [ "$CHAT_ID" -eq "$TELEGRAM_CHAT_ID" ]; then
            log_message "Received input: $TEXT"
            if [ "$REPLY_MSG" != "null" ] && [ "$(echo "$REPLY_MSG" | jq -r '.document')" != "null" ]; then TEXT="/download"; fi
            read -r command arg1 arg2 <<< "$TEXT"
            case "$command" in
                "/start"|"/help") local HELP_TEXT="*Welcome to the Ultimate Linux Admin bot!*
                
*⚡️ System & Updates*
\`/status\` - Detailed system overview.
\`/checkupdates\` - See available packages.
\`/runupdates\` - Install all updates.
\`/top\` - View top processes.
\`/getlog <path>\` - Get a log file (default: syslog).

*🔐 Security & Network*
\`/ufw status | enable | disable\`
\`/ufw allow | deny <port>\`
\`/user add | del <name>\`
\`/kill <pid>\`

*📁 File Management*
\`/upload <path>\` - Upload a file from the server.
\`/download\` - Reply to a file to download it.

*🚀 Interactive Shell*
\`/shell\` - Start a persistent root shell.
\`/exit\` - Terminate the shell session.

*🛠️ System Control & Script*
\`/service status | restart <name>\`
\`/reboot\` | \`/shutdown\` - ⚠️
\`/selfupdate\` - Update this script.
\`/confirm <code>\` - Confirm a dangerous action."; sendMessage "$HELP_TEXT" > /dev/null ;;
                "/status") get_system_status ;; "/checkupdates") check_updates ;; "/runupdates") run_updates ;; "/top") manage_processes "top" ;;
                "/getlog") get_log_file "$arg1" ;;
                "/ufw") manage_firewall "$arg1" "$arg2" ;;
                "/user") manage_user "$arg1" "$arg2" ;;
                "/kill") manage_processes "kill" "$arg1" ;;
                "/upload") upload_file_to_telegram "$arg1" ;; "/download") download_file_from_telegram "$REPLY_MSG" ;;
                "/reboot") create_confirmation "/sbin/reboot" ;; "/shutdown") create_confirmation "/sbin/shutdown -h now" ;;
                "/confirm") execute_confirmation "$arg1" ;; "/selfupdate") self_update_script ;;
                "/service") manage_service "$arg1" "$arg2" ;;
                "/shell") start_shell_session ;; "/exit") if is_shell_active; then stop_shell_session; sendMessage "Shell terminated." >/dev/null; else sendMessage "No active shell." >/dev/null; fi ;;
                *) if is_shell_active; then execute_shell_command "$TEXT"; else sendMessage "❓ Unknown command. Use /help or start with /shell." > /dev/null; fi ;;
            esac
        else log_message "WARNING: Received command from unauthorized chat ID: $CHAT_ID."; fi
        echo "$UPDATE_ID" > "$OFFSET_FILE"
    done
}
main "$@"
