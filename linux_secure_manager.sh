#!/bin/bash

# ===================================================================================
# Linux Secure Manager - The Enterprise Edition
#
# A comprehensive, self-updating script that provides a hybrid interface for
# total server management via a secure Telegram bot.
#
# Author: Opselon
# Version: 8.0
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

LOG_FILE="/var/log/linux_secure_manager.log"; SESSION_DIR="/tmp/telegram_shell_session"; SESSION_ACTIVE_FILE="${SESSION_DIR}/active.lock"; SESSION_CWD_FILE="${SESSION_DIR}/cwd.txt"; SESSION_PID_FILE="${SESSION_DIR}/pid.txt"; SESSION_LOG_FILE="${SESSION_DIR}/output.log"; OFFSET_FILE="/tmp/telegram_offset.dat"
SCRIPT_PATH="$(realpath "$0")"
REPO_URL="https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/linux_secure_manager.sh"

log_message() { echo "$(date +"%Y-%m-%d %H:%M:%S") - $1" | tee -a "$LOG_FILE"; }

# --- Setup Wizard ---
run_setup_wizard() {
    clear; echo -e "${C_BOLD}====================================================${C_RESET}"; echo -e "${C_BOLD} Welcome to the Linux Secure Manager Setup Wizard ${C_RESET}"; echo -e "${C_BOLD}====================================================${C_RESET}"; print_warn "This script provides a full root shell. The security of your"; print_warn "Telegram account is critical. Enable Two-Step Verification."; echo
    local new_bot_token; while [ -z "$new_bot_token" ]; do prompt_input "Enter your Telegram Bot Token" "" new_bot_token; done
    local new_chat_id; print_info "To find your Chat ID, send a message to your bot, then visit:"; print_info "https://api.telegram.org/bot${new_bot_token}/getUpdates"; while [[ ! "$new_chat_id" =~ ^[0-9]+$ ]]; do prompt_input "Enter your numerical Telegram Chat ID" "" new_chat_id; done
    local new_auto_maintenance; prompt_input "Enable scheduled automatic updates (Sun at 3am)? (y/n)" "y" new_auto_maintenance; [[ "$new_auto_maintenance" =~ ^[Yy]$ ]] && new_auto_maintenance="1" || new_auto_maintenance="0"
    print_info "Saving configuration..."
    sed -i "s#TELEGRAM_BOT_TOKEN=\".*\"#TELEGRAM_BOT_TOKEN=\"$new_bot_token\"#" "$SCRIPT_PATH"
    sed -i "s#TELEGRAM_CHAT_ID=\".*\"#TELEGRAM_CHAT_ID=\"$new_chat_id\"#" "$SCRIPT_PATH"
    sed -i "s#ENABLE_AUTO_MAINTENANCE=\".*\"#ENABLE_AUTO_MAINTENANCE=\"$new_auto_maintenance\"#" "$SCRIPT_PATH"
    if grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$SCRIPT_PATH"; then print_error "Configuration save FAILED! This is usually a permissions issue."; print_error "Please ensure you are running the installer with 'sudo'. Aborting."; exit 1; fi
    print_success "Configuration saved!"
    local setup_cron; prompt_input "Automatically set up cron jobs? (y/n)" "y" setup_cron
    if [[ "$setup_cron" =~ ^[Yy]$ ]]; then
        print_info "Setting up cron jobs..."; local LISTEN_CRON="* * * * * $SCRIPT_PATH --listen >> $LOG_FILE 2>&1"; (crontab -l 2>/dev/null | grep -Fv "$SCRIPT_PATH --listen" || true; echo "$LISTEN_CRON") | crontab -
        if [ "$new_auto_maintenance" -eq 1 ]; then local AUTO_CRON="0 3 * * 0 $SCRIPT_PATH --auto >> $LOG_FILE 2>&1"; (crontab -l 2>/dev/null | grep -Fv "$SCRIPT_PATH --auto" || true; echo "$AUTO_CRON") | crontab -; fi
        print_success "Cron jobs have been configured."
    fi
    echo; print_success "Setup complete! Type /start in your Telegram bot."; print_info "You can run 'sudo $SCRIPT_PATH' again for a management menu."; exit 0
}

# --- Telegram API & Messaging ---
TELEGRAM_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
sendMessage() { local response; response=$(curl -s -X POST "${TELEGRAM_API}/sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=$1" -d "parse_mode=Markdown"); echo "$response" | jq -r '.result.message_id'; }
editMessage() { curl -s -X POST "${TELEGRAM_API}/editMessageText" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "message_id=$1" -d "text=$2" -d "parse_mode=Markdown" > /dev/null; }
sendDocument() { curl -s -X POST "${TELEGRAM_API}/sendDocument" -F "chat_id=${TELEGRAM_CHAT_ID}" -F "document=@$1" -F "caption=$2" > /dev/null; }
sendLongMessage() { echo "$1" | awk '{printf "%s%s", $0, RT}' | fold -s -w 4000 | while read -r chunk; do sendMessage "\`\`\`\n${chunk}\n\`\`\`" > /dev/null; done; }

# --- Feature Functions ---
get_system_status() { local msg_id=$(sendMessage "🔄 Fetching system status..."); local UPTIME=$(uptime -p); local MEM=$(free -m | awk 'NR==2{printf "%.2f%%", $3*100/$2 }'); local DISK=$(df -h / | awk 'NR==2{printf "%s", $5}'); local LOAD=$(uptime | awk -F'load average:' '{print $2}' | sed 's/ //g'); local USERS=$(who | wc -l); local PROCS=$(ps aux --sort=-%mem | head -n 6); local STATUS_REPORT="*Server Status: $(hostname)*\n\`\`\`\n- Uptime:         ${UPTIME}\n- Memory Usage:   ${MEM}\n- Disk Usage (/): ${DISK}\n- Logged in Users:${USERS}\n- Load Average:   ${LOAD}\n\nTop 5 Processes by Memory:\n${PROCS}\n\`\`\`"; editMessage "$msg_id" "$STATUS_REPORT"; }
check_updates() { local msg_id=$(sendMessage "🔄 Checking for package updates..."); apt-get update -y >/dev/null 2>&1; local UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -v "Listing..."); if [ -z "$UPGRADABLE" ]; then editMessage "$msg_id" "✅ System is up-to-date."; else local COUNT=$(echo "$UPGRADABLE" | wc -l); editMessage "$msg_id" "⚠️ *${COUNT} packages can be upgraded.* Sending list..."; local LIST_FILE="/tmp/update_list.txt"; echo "$UPGRADABLE" > "$LIST_FILE"; sendDocument "$LIST_FILE" "List of upgradable packages."; rm "$LIST_FILE"; fi; }
run_updates() { log_message "Starting full system upgrade..."; sendMessage "🚀 Starting full system upgrade in the background. I will notify you on completion." > /dev/null; ( set -e; { echo "--- APT UPDATE ---"; apt-get update -y; echo -e "\n--- APT UPGRADE ---"; apt-get upgrade -y; echo -e "\n--- APT AUTOREMOVE ---"; apt-get autoremove --purge -y; echo -e "\n--- APT CLEAN ---"; apt-get clean; } >> "$LOG_FILE" 2>&1; log_message "System upgrade completed."; sendMessage "✅ *System upgrade complete on $(hostname)!*" > /dev/null ) & }
get_network_info() { local msg_id=$(sendMessage "🔄 Fetching network info..."); local IP=$(hostname -I); local PORTS=$(ss -tuln); local NET_INFO="*Network Info: $(hostname)*\n\n*IP Addresses:*\n\`\`\`${IP}\`\`\`\n*Active Listening Ports (TCP/UDP):*\n\`\`\`${PORTS}\`\`\`"; editMessage "$msg_id" "$NET_INFO"; }
upload_file_to_telegram() { local file_path="$1"; if [ -z "$file_path" ]; then sendMessage "Usage: \`/upload /path/to/your/file\`" > /dev/null; return; fi; if [ -f "$file_path" ]; then if [ $(stat -c%s "$file_path") -gt 50000000 ]; then sendMessage "❌ File is larger than 50MB (Telegram API limit)." > /dev/null; return; fi; local msg_id=$(sendMessage "⬆️ Uploading \`${file_path}\`..."); sendDocument "$file_path" "File from $(hostname)"; editMessage "$msg_id" "✅ Uploaded \`${file_path}\`."; else sendMessage "❌ File not found at \`${file_path}\`." > /dev/null; fi; }
download_file_from_telegram() {
    local file_id=$(echo "$1" | jq -r .document.file_id); local file_name=$(echo "$1" | jq -r .document.file_name)
    local CWD=$(cat "$SESSION_CWD_FILE" 2>/dev/null || echo "/root")
    local msg_id=$(sendMessage "⬇️ Downloading \`${file_name}\` to \`${CWD}\`...")
    local file_path_json=$(curl -s "${TELEGRAM_API}/getFile?file_id=${file_id}"); local file_path=$(echo "$file_path_json" | jq -r .result.file_path)
    if [ "$file_path" != "null" ]; then
        curl -s "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${file_path}" -o "${CWD}/${file_name}"
        editMessage "$msg_id" "✅ Downloaded \`${file_name}\` to \`${CWD}\`."
    else
        editMessage "$msg_id" "❌ Failed to get file path from Telegram."
    fi
}
self_update_script() {
    local msg_id=$(sendMessage "🔄 Checking for updates to the script...")
    local temp_script="/tmp/linux_secure_manager.sh.new"
    if ! curl -sSL "$REPO_URL" -o "$temp_script"; then editMessage "$msg_id" "❌ Failed to download new version."; rm -f "$temp_script"; return; fi
    local new_version=$(grep 'Version:' "$temp_script" | head -1); local old_version=$(grep 'Version:' "$SCRIPT_PATH" | head -1)
    if [ "$new_version" == "$old_version" ]; then editMessage "$msg_id" "✅ You are already running the latest version. ($old_version)"; rm -f "$temp_script"; return; fi
    # Preserve configuration
    local config_block=$(sed -n '/# \[CONFIG_START\]/,/# \[CONFIG_END\]/p' "$SCRIPT_PATH")
    # Replace the placeholder in the new script with the user's actual config
    sed -i '/# \[CONFIG_START\]/,/# \[CONFIG_END\]/c\'"$config_block" "$temp_script"
    # Replace the running script file
    mv "$temp_script" "$SCRIPT_PATH" && chmod +x "$SCRIPT_PATH"
    editMessage "$msg_id" "✅ Script updated from $old_version to $new_version. Restarting services is recommended."
}

# --- Shell Session ---
is_shell_active() { [[ -f "$SESSION_ACTIVE_FILE" ]]; }
get_shell_prompt() { local cwd; cwd=$(cat "$SESSION_CWD_FILE" 2>/dev/null || echo "~"); cwd=${cwd/#$HOME/\~}; echo "*root@$(hostname):${cwd}#*"; }
start_shell_session() { log_message "Starting shell session."; rm -rf "$SESSION_DIR"; mkdir -p "$SESSION_DIR"; touch "$SESSION_ACTIVE_FILE"; echo "$HOME" > "$SESSION_CWD_FILE"; local WELCOME="✅ *Remote Shell Started*\n- Use \`/exit\` to terminate.\n\n$(get_shell_prompt)"; sendMessage "$WELCOME" > /dev/null; }
stop_shell_session() { log_message "Stopping shell session."; if [[ -f "$SESSION_PID_FILE" ]]; then local pid=$(cat "$SESSION_PID_FILE"); if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid"; fi; fi; rm -rf "$SESSION_DIR"; sendMessage "✅ *Remote Shell Terminated.*" > /dev/null; }
execute_shell_command() {
    local command_to_run="$1"; if [[ -f "$SESSION_PID_FILE" ]]; then sendMessage "⏳ Background task is running. Please wait." > /dev/null; return; fi; log_message "Executing shell cmd: $command_to_run"; local CWD=$(cat "$SESSION_CWD_FILE")
    if [[ "$command_to_run" =~ (apt|wget|curl|sleep|upgrade|dist-upgrade|install|remove|make|git) ]] || [[ "$command_to_run" == *\& ]]; then
        ( cd "$CWD" && eval "$command_to_run" ) > "$SESSION_LOG_FILE" 2>&1 &
        local pid=$!; echo "$pid" > "$SESSION_PID_FILE"; sendMessage "⏳ Command started in background (PID: $pid). Output will follow.\n\n$(get_shell_prompt)" > /dev/null
    else
        local raw_output=$(cd "$CWD" && { eval "$command_to_run"; echo "---CWD---"; pwd; } 2>&1 || true); local output=$(echo "$raw_output" | sed '/---CWD---/,$d'); local new_cwd=$(echo "$raw_output" | sed '1,/---CWD---/d'); echo "$new_cwd" > "$SESSION_CWD_FILE"
        if [[ -z "$output" ]]; then sendMessage "$(get_shell_prompt)" > /dev/null; else sendLongMessage "$output"; sendMessage "$(get_shell_prompt)" > /dev/null; fi
    fi
}
check_background_process() { if [[ -f "$SESSION_PID_FILE" ]]; then local pid=$(cat "$SESSION_PID_FILE"); if ! kill -0 "$pid" 2>/dev/null; then log_message "Background process $pid finished."; local output=$(cat "$SESSION_LOG_FILE"); sendMessage "✅ *Background task (PID: $pid) finished.*" > /dev/null; if [[ -n "$output" ]]; then sendLongMessage "$output"; else sendMessage "_No output produced._" > /dev/null; fi; sendMessage "$(get_shell_prompt)" > /dev/null; rm "$SESSION_PID_FILE" "$SESSION_LOG_FILE"; fi; fi; }

# --- INTERACTIVE MANAGEMENT MENU ---
run_interactive_menu() {
    if ! grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$SCRIPT_PATH"; then local B_TOKEN=$(grep 'TELEGRAM_BOT_TOKEN=' "$SCRIPT_PATH" | cut -d'"' -f2); local C_ID=$(grep 'TELEGRAM_CHAT_ID=' "$SCRIPT_PATH" | cut -d'"' -f2); fi
    while true; do
        clear; echo -e "${C_BOLD}--- Telegram Linux Admin Management Menu ---${C_RESET}"; echo
        echo -e " 1) View Live Script Logs"; echo -e " 2) Test Telegram Bot Connection"; echo -e " 3) Send Full Log File to Telegram"; echo -e " 4) Check Cron Job Status"; echo -e " 5) Run Self-Update"; echo -e " 6) Re-run Setup Wizard"; echo -e " 7) Uninstall"; echo -e " q) Quit"; echo
        read -p "Select an option: " choice
        case $choice in
            1) sudo tail -f "$LOG_FILE"; read -p "Press Enter to return...";;
            2) print_info "Pinging Telegram API..."; if [ -z "$B_TOKEN" ]; then print_error "Not configured. Run setup first."; else API_RESPONSE=$(curl -s "https://api.telegram.org/bot${B_TOKEN}/getMe"); if echo "$API_RESPONSE" | grep -q '"ok":true'; then print_success "Connection OK! Bot name: $(echo $API_RESPONSE | jq -r .result.first_name)"; else print_error "Connection FAILED. Check your Bot Token."; fi; fi; read -p "Press Enter...";;
            3) print_info "Sending log file..."; if [ -z "$B_TOKEN" ]; then print_error "Not configured."; else curl -s -X POST "https://api.telegram.org/bot${B_TOKEN}/sendDocument" -F "chat_id=${C_ID}" -F "document=@${LOG_FILE}" -F "caption=Log file from $(hostname)" > /dev/null; print_success "Log file sent!"; fi; read -p "Press Enter...";;
            4) print_info "Current cron jobs for root:"; sudo crontab -l | grep --color=always "$SCRIPT_PATH" || print_warn "No cron jobs for this script found."; read -p "Press Enter...";;
            5) print_info "Checking for updates..."; self_update_script; read -p "Press Enter...";;
            6) run_setup_wizard; exit 0;;
            7) print_warn "This will run the uninstaller. Are you sure? (y/N)"; read confirm; if [[ "$confirm" =~ ^[Yy]$ ]]; then curl -sSL https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/uninstall.sh | sudo bash; exit 0; fi;;
            q) exit 0;;
            *) print_error "Invalid option."; sleep 1;;
        esac
    done
}

# --- MAIN ---
main() {
    if [ "$(id -u)" -ne 0 ]; then echo "This script must be run as root." >&2; exit 1; fi
    case "$1" in
        --listen) if grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$SCRIPT_PATH"; then log_message "ERROR: Script is not configured. Run setup wizard first."; exit 1; fi; for cmd in curl jq; do if ! command -v $cmd &> /dev/null; then log_message "ERROR: Dependency '$cmd' not found."; exit 1; fi; done; listen_for_commands ;;
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
            case "$TEXT" in
                "/start"|"/help") local HELP_TEXT="*Welcome to your Linux Admin bot!*
                
*⚡️ System & Updates*
\`/status\` - Detailed system overview.
\`/checkupdates\` - See available package updates.
\`/runupdates\` - Install all updates.
\`/netinfo\` - View listening ports & IP addresses.

*📁 File Management*
\`/upload <path>\` - Upload a file from the server.
\`/download\` - Reply to a file to download it here.

*🚀 Interactive Shell*
\`/shell\` - Start a persistent root shell.
\`/exit\` - Terminate the shell session.

*🛠️ System Control & Script*
\`/service status <name>\`
\`/service restart <name>\`
\`/reboot\` - ⚠️ Reboot server.
\`/shutdown\` - ⚠️ Shutdown server.
\`/selfupdate\` - Update this script to the latest version."; sendMessage "$HELP_TEXT" > /dev/null ;;
                "/status") get_system_status ;; "/checkupdates") check_updates ;; "/runupdates") run_updates ;; "/netinfo") get_network_info ;; "/selfupdate") self_update_script ;;
                "/reboot") sendMessage "⚠️ *Server REBOOTING in 10 seconds!* To cancel, shutdown the server manually." > /dev/null; sleep 10; log_message "Reboot command executed."; /sbin/reboot ;;
                "/shutdown") sendMessage "⚠️ *Server SHUTTING DOWN in 10 seconds!*" > /dev/null; sleep 10; log_message "Shutdown command executed."; /sbin/shutdown -h now ;;
                /upload*) local file_to_upload="${TEXT#*/upload }"; upload_file_to_telegram "$file_to_upload" ;;
                /download) download_file_from_telegram "$REPLY_MSG" ;;
                /service*) read -r _ action service_name <<< "$TEXT"; if [[ -n "$action" && -n "$service_name" ]]; then manage_service "$action" "$service_name"; else sendMessage "Usage: \`/service <status|restart> <name>\`" > /dev/null; fi ;;
                "/shell") start_shell_session ;; "/exit") if is_shell_active; then stop_shell_session; else sendMessage "No active shell session." > /dev/null; fi ;;
                *) if is_shell_active; then execute_shell_command "$TEXT"; else sendMessage "❓ Unknown command. Use /help or start with /shell." > /dev/null; fi ;;
            esac
        else
            log_message "WARNING: Received command from unauthorized chat ID: $CHAT_ID."
        fi
        echo "$UPDATE_ID" > "$OFFSET_FILE"
    done
}

main "$@"
