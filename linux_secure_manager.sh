#!/bin/bash

# [CONFIG_START]
TELEGRAM_BOT_TOKEN="YOUR_TELEGRAM_BOT_TOKEN"
TELEGRAM_CHAT_ID="YOUR_CHAT_ID"
ENABLE_AUTO_MAINTENANCE="1"
# [CONFIG_END]

# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---
# --- SCRIPT LOGIC - DO NOT EDIT BELOW THIS LINE ---
# --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- --- ---

set +e

# --- Core, UI & State Management ---
C_BOLD="\033[1m"; C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_BLUE="\033[34m"; C_RESET="\033[0m"
print_info() { echo -e "${C_BLUE}${C_BOLD}[INFO]${C_RESET} $1"; }
print_success() { echo -e "${C_GREEN}${C_BOLD}[SUCCESS]${C_RESET} $1"; }
print_warn() { echo -e "${C_YELLOW}${C_BOLD}[WARNING]${C_RESET} $1"; }
prompt_input() {
    local prompt="$1"; local default_value="$2"; local var_name="$3"; local user_input
    if [[ -n "$default_value" ]]; then
        read -p "$(echo -e "${C_YELLOW}${prompt}${C_RESET} [default: ${default_value}]: ")" user_input
        eval "$var_name='${user_input:-$default_value}'"
    else
        read -p "$(echo -e "${C_YELLOW}${prompt}${C_RESET}: ")" user_input
        eval "$var_name='${user_input}'"
    fi
}

LOG_FILE="/var/log/linux_secure_manager.log"
SESSION_DIR="/tmp/telegram_shell_session"
SESSION_ACTIVE_FILE="${SESSION_DIR}/active.lock"
SESSION_CWD_FILE="${SESSION_DIR}/cwd.txt"
SESSION_PID_FILE="${SESSION_DIR}/pid.txt"
SESSION_LOG_FILE="${SESSION_DIR}/output.log"
OFFSET_FILE="/tmp/telegram_offset.dat"

log_message() { echo "$(date +"%Y-%m-%d %H:%M:%S") - $1" | tee -a "$LOG_FILE"; }

# --- Setup Wizard ---
run_setup_wizard() {
    clear
    echo -e "${C_BOLD}====================================================${C_RESET}"
    echo -e "${C_BOLD} Welcome to the Linux Secure Manager Setup Wizard ${C_RESET}"
    echo -e "${C_BOLD}====================================================${C_RESET}"
    print_warn "This script provides a full root shell. The security of your"
    print_warn "Telegram account is critical. Enable Two-Step Verification."
    echo

    local new_bot_token; while [ -z "$new_bot_token" ]; do prompt_input "Enter your Telegram Bot Token" "" new_bot_token; done
    
    local new_chat_id
    print_info "To find your Chat ID, send a message to your bot, then visit:"
    print_info "https://api.telegram.org/bot${new_bot_token}/getUpdates"
    while [[ ! "$new_chat_id" =~ ^[0-9]+$ ]]; do prompt_input "Enter your numerical Telegram Chat ID" "" new_chat_id; done
    
    local new_auto_maintenance
    prompt_input "Enable scheduled automatic updates (Sun at 3am)? (y/n)" "y" new_auto_maintenance
    [[ "$new_auto_maintenance" =~ ^[Yy]$ ]] && new_auto_maintenance="1" || new_auto_maintenance="0"

    print_info "Saving configuration..."
    local SCRIPT_PATH; SCRIPT_PATH="$(realpath "$0")"
    sed -i "s#TELEGRAM_BOT_TOKEN=\".*\"#TELEGRAM_BOT_TOKEN=\"$new_bot_token\"#" "$SCRIPT_PATH"
    sed -i "s#TELEGRAM_CHAT_ID=\".*\"#TELEGRAM_CHAT_ID=\"$new_chat_id\"#" "$SCRIPT_PATH"
    sed -i "s#ENABLE_AUTO_MAINTENANCE=\".*\"#ENABLE_AUTO_MAINTENANCE=\"$new_auto_maintenance\"#" "$SCRIPT_PATH"
    print_success "Configuration saved!"
    echo

    local setup_cron
    prompt_input "Automatically set up cron jobs? (y/n)" "y" setup_cron
    if [[ "$setup_cron" =~ ^[Yy]$ ]]; then
        print_info "Setting up cron jobs..."
        local LISTEN_CRON="* * * * * $SCRIPT_PATH --listen >> $LOG_FILE 2>&1"
        (crontab -l 2>/dev/null | grep -Fv "$SCRIPT_PATH --listen" || true; echo "$LISTEN_CRON") | crontab -
        
        if [ "$new_auto_maintenance" -eq 1 ]; then
            local AUTO_CRON="0 3 * * 0 $SCRIPT_PATH --auto >> $LOG_FILE 2>&1"
            (crontab -l 2>/dev/null | grep -Fv "$SCRIPT_PATH --auto" || true; echo "$AUTO_CRON") | crontab -
        fi
        print_success "Cron jobs have been configured."
    fi
    echo
    print_success "Setup complete! Type /start in your Telegram bot for a command list."
    exit 0
}

# --- Telegram API & Messaging ---
TELEGRAM_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"
sendMessage() { local response; response=$(curl -s -X POST "${TELEGRAM_API}/sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=$1" -d "parse_mode=Markdown"); echo "$response" | jq -r '.result.message_id'; }
editMessage() { curl -s -X POST "${TELEGRAM_API}/editMessageText" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "message_id=$1" -d "text=$2" -d "parse_mode=Markdown" > /dev/null; }
sendDocument() { curl -s -X POST "${TELEGRAM_API}/sendDocument" -F "chat_id=${TELEGRAM_CHAT_ID}" -F "document=@$1" -F "caption=$2" > /dev/null; }
sendLongMessage() {
    local text="$1"
    echo "$text" | awk '{printf "%s%s", $0, RT}' | fold -s -w 4000 | while read -r chunk; do
        sendMessage "\`\`\`\n${chunk}\n\`\`\`" > /dev/null
    done
}

# --- Shortcut Command Functions ---
get_system_status() {
    local msg_id; msg_id=$(sendMessage "🔄 Fetching system status...")
    local UPTIME; UPTIME=$(uptime -p)
    local MEM; MEM=$(free -m | awk 'NR==2{printf "%.2f%%", $3*100/$2 }')
    local DISK; DISK=$(df -h / | awk 'NR==2{printf "%s", $5}')
    local IP; IP=$(hostname -I | awk '{print $1}')
    local KERNEL; KERNEL=$(uname -r)
    local STATUS_REPORT; STATUS_REPORT="*Server Status: $(hostname)*
\`\`\`
- IP Address:     ${IP}
- Kernel Version: ${KERNEL}
- Uptime:         ${UPTIME}
- Memory Usage:   ${MEM}
- Disk Usage (/): ${DISK}
\`\`\`"
    editMessage "$msg_id" "$STATUS_REPORT"
}

check_updates() {
    local msg_id; msg_id=$(sendMessage "🔄 Checking for package updates...")
    apt-get update -y >/dev/null 2>&1
    local UPGRADABLE; UPGRADABLE=$(apt list --upgradable 2>/dev/null | grep -v "Listing...")
    if [ -z "$UPGRADABLE" ]; then
        editMessage "$msg_id" "✅ Your system is up-to-date. No packages to upgrade."
    else
        local COUNT; COUNT=$(echo "$UPGRADABLE" | wc -l)
        editMessage "$msg_id" "⚠️ *${COUNT} packages can be upgraded.* Sending list..."
        local LIST_FILE="/tmp/update_list.txt"
        echo "$UPGRADABLE" > "$LIST_FILE"
        sendDocument "$LIST_FILE" "List of upgradable packages."
        rm "$LIST_FILE"
    fi
}

run_updates() {
    log_message "Starting full system upgrade..."
    sendMessage "🚀 Starting full system upgrade. This will run in the background. I will notify you upon completion." > /dev/null
    (
        set -e
        {
            echo "--- APT UPDATE ---"
            apt-get update -y
            echo -e "\n--- APT UPGRADE ---"
            apt-get upgrade -y
            echo -e "\n--- APT AUTOREMOVE ---"
            apt-get autoremove --purge -y
            echo -e "\n--- APT CLEAN ---"
            apt-get clean
        } >> "$LOG_FILE" 2>&1
        log_message "System upgrade completed."
        sendMessage "✅ *System upgrade complete on $(hostname)!*" > /dev/null
    ) &
}

manage_service() {
    local action="$1"; local service_name="$2"
    local msg_id; msg_id=$(sendMessage "🔄 Managing service \`$service_name\`...")
    if [[ ! "$service_name" =~ ^[a-zA-Z0-9._-]+$ ]]; then editMessage "$msg_id" "❌ *Invalid service name.*"; return; fi
    if ! systemctl list-units --type=service --all | grep -q "${service_name}.service"; then editMessage "$msg_id" "❌ Service \`$service_name\` not found."; return; fi
    local output;
    if [ "$action" == "restart" ]; then (systemctl restart "$service_name" && output=$(systemctl status "$service_name" --no-pager)) || output="Failed to restart service."; else output=$(systemctl status "$service_name" --no-pager); fi
    local truncated_output; truncated_output=$(echo -e "$output" | head -c 3000)
    editMessage "$msg_id" "*Service: \`${service_name}\` Action: \`${action}\`*\n\`\`\`\n${truncated_output}\n\`\`\`"
}

# --- Shell Session Management ---
is_shell_active() { [[ -f "$SESSION_ACTIVE_FILE" ]]; }
get_shell_prompt() { local cwd; cwd=$(cat "$SESSION_CWD_FILE" 2>/dev/null || echo "~"); cwd=${cwd/#$HOME/\~}; echo "*root@$(hostname):${cwd}#*"; }
start_shell_session() { log_message "Starting new shell session."; rm -rf "$SESSION_DIR"; mkdir -p "$SESSION_DIR"; touch "$SESSION_ACTIVE_FILE"; echo "$HOME" > "$SESSION_CWD_FILE"; local WELCOME; WELCOME="✅ *Remote Shell Session Started*\n- Type any command to execute.\n- Use \`/exit\` to terminate.\n\n$(get_shell_prompt)"; sendMessage "$WELCOME" > /dev/null; }
stop_shell_session() { log_message "Stopping shell session."; if [[ -f "$SESSION_PID_FILE" ]]; then local pid; pid=$(cat "$SESSION_PID_FILE"); if kill -0 "$pid" 2>/dev/null; then kill -9 "$pid"; fi; fi; rm -rf "$SESSION_DIR"; sendMessage "✅ *Remote Shell Session Terminated.*" > /dev/null; }
execute_shell_command() {
    local command_to_run="$1"
    if [[ -f "$SESSION_PID_FILE" ]]; then sendMessage "⏳ A background task is still running. Please wait." > /dev/null; return; fi
    log_message "Executing shell command: $command_to_run"
    local CWD; CWD=$(cat "$SESSION_CWD_FILE")
    if [[ "$command_to_run" =~ (apt|wget|curl|sleep|upgrade|dist-upgrade|install|remove|make|git) ]] || [[ "$command_to_run" == *\& ]]; then
        ( cd "$CWD" && eval "$command_to_run" ) > "$SESSION_LOG_FILE" 2>&1 &
        local pid=$!; echo "$pid" > "$SESSION_PID_FILE"
        sendMessage "⏳ Command started in background (PID: $pid). Output will be sent upon completion.\n\n$(get_shell_prompt)" > /dev/null
    else
        local raw_output; raw_output=$(cd "$CWD" && { eval "$command_to_run"; echo "---CWD---"; pwd; } 2>&1 || true)
        local output; output=$(echo "$raw_output" | sed '/---CWD---/,$d')
        local new_cwd; new_cwd=$(echo "$raw_output" | sed '1,/---CWD---/d')
        echo "$new_cwd" > "$SESSION_CWD_FILE"
        if [[ -z "$output" ]]; then sendMessage "$(get_shell_prompt)" > /dev/null; else sendLongMessage "$output"; sendMessage "$(get_shell_prompt)" > /dev/null; fi
    fi
}
check_background_process() {
    if [[ -f "$SESSION_PID_FILE" ]]; then
        local pid; pid=$(cat "$SESSION_PID_FILE")
        if ! kill -0 "$pid" 2>/dev/null; then
            log_message "Background process $pid has finished."; local output; output=$(cat "$SESSION_LOG_FILE")
            sendMessage "✅ *Background task (PID: $pid) finished.*" > /dev/null
            if [[ -n "$output" ]]; then sendLongMessage "$output"; else sendMessage "_No output was produced._" > /dev/null; fi
            sendMessage "$(get_shell_prompt)" > /dev/null; rm "$SESSION_PID_FILE" "$SESSION_LOG_FILE"
        fi
    fi
}

# --- Main Logic ---
main() {
    if [ "$(id -u)" -ne 0 ]; then echo "This script must be run as root." >&2; exit 1; fi
    if grep -q "YOUR_TELEGRAM_BOT_TOKEN" "$0"; then run_setup_wizard; fi
    for cmd in curl jq; do if ! command -v $cmd &> /dev/null; then log_message "ERROR: Dependency '$cmd' not found."; exit 1; fi; done
    case "$1" in
        --listen) listen_for_commands ;;
        --auto) if [ "$ENABLE_AUTO_MAINTENANCE" -eq 1 ]; then log_message "Running scheduled maintenance."; run_updates; fi ;;
        --setup) run_setup_wizard ;;
        *) echo "Usage: $0 {--listen|--auto|--setup}"; exit 1 ;;
    esac
}

listen_for_commands() {
    check_background_process
    local OFFSET; OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
    local UPDATES; UPDATES=$(curl -s "${TELEGRAM_API}/getUpdates?offset=$((OFFSET + 1))&timeout=10")
    echo "$UPDATES" | jq -c '.result[]' | while read -r MESSAGE; do
        local CHAT_ID; CHAT_ID=$(echo "$MESSAGE" | jq -r '.message.chat.id')
        local COMMAND; COMMAND=$(echo "$MESSAGE" | jq -r '.message.text')
        local UPDATE_ID; UPDATE_ID=$(echo "$MESSAGE" | jq -r '.update_id')
        if [ "$CHAT_ID" -eq "$TELEGRAM_CHAT_ID" ]; then
            log_message "Received input: $COMMAND"
            case "$COMMAND" in
                "/start"|"/help")
                    local HELP_TEXT; HELP_TEXT="*Welcome to the Linux Secure Manager!*
                    
*Shortcut Commands (Always Available):*
\`/status\` - Quick system overview.
\`/checkupdates\` - See available package updates.
\`/runupdates\` - Install all updates & clean system.
\`/service status <name>\` - Get service status.
\`/service restart <name>\` - Restart a service.
\`/reboot\` - ⚠️ Reboot the entire server.

*Interactive Shell:*
\`/shell\` - Start a persistent root shell.
\`/exit\` - Terminate the shell session.

_Once a shell is active, any text you send is treated as a command in the terminal._"
                    sendMessage "$HELP_TEXT" > /dev/null
                    ;;
                "/status") get_system_status ;;
                "/checkupdates") check_updates ;;
                "/runupdates") run_updates ;;
                "/reboot") sendMessage "⚠️ *WARNING:* Server will reboot in 10 seconds!" > /dev/null; sleep 10; log_message "Reboot command executed."; /sbin/reboot ;;
                /service*) read -r _ action service_name <<< "$COMMAND"; if [[ -n "$action" && -n "$service_name" ]]; then manage_service "$action" "$service_name"; else sendMessage "Usage: \`/service <status|restart> <name>\`" > /dev/null; fi ;;
                "/shell") start_shell_session ;;
                "/exit") if is_shell_active; then stop_shell_session; else sendMessage "No active shell session to exit." > /dev/null; fi ;;
                *)
                    if is_shell_active; then
                        execute_shell_command "$COMMAND"
                    else
                        sendMessage "❓ Unknown command. Use /help for a list of commands or start a session with /shell." > /dev/null
                    fi
                    ;;
            esac
        else
            log_message "WARNING: Received command from unauthorized chat ID: $CHAT_ID."
        fi
        echo "$UPDATE_ID" > "$OFFSET_FILE"
    done
}

# --- Script Entry Point ---
main "$@"
