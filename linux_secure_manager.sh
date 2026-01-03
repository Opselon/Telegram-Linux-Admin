#!/usr/bin/env bash
# Linux Secure Manager - Pro Full Edition
# Massive single-file manager with deploy, docker, healthchecks, metrics, async tasks, pro logging and fail shipping.
# Version: 9.3-pro-full
# Author: assistant (based on Opselon original + user requested features)
# IMPORTANT: Run as root. Keep /etc/linux_secure_manager.conf private.
set -euo pipefail
IFS=$'\n\t'
umask 0077
shopt -s inherit_errexit

# ----------------
# Basic Paths and Constants
# ----------------
SCRIPT_PATH="$(realpath "$0")"
CONFIG_FILE="/etc/linux_secure_manager.conf"
LOG_FILE="/var/log/linux_secure_manager.log"
DEBUG_LOG="/var/log/linux_secure_manager.debug.log"
SESSION_DIR="/run/linux_secure_manager/session"
CONFIRM_DIR="/run/linux_secure_manager/confirm"
TASKS_DIR="/run/linux_secure_manager/tasks"
OFFSET_FILE="/run/linux_secure_manager/offset.dat"
REPO_URL="https://raw.githubusercontent.com/Opselon/Telegram-Linux-Admin/main/linux_secure_manager.sh"
LOCKFILE="/run/lock/linux_secure_manager.lock"
SYSTEMD_UNIT="/etc/systemd/system/linux_secure_manager.service"
LOGROTATE_FILE="/etc/logrotate.d/linux_secure_manager"
RELEASES_ROOT="/var/www/releases"         # base for git-based deployments
CURRENT_SYMLINK="/var/www/current"        # symlink pointing to active release
RELEASE_RETENTION=5                       # keep last N releases
FAIL_NOTIFY_COOLDOWN=600                  # seconds between automatic fail log sends
MAX_TELEGRAM_MESSAGE=3800                 # Telegram safe chunk size (approx)
TASK_WORKERS=4                            # max concurrent background tasks

# ensure runtime dirs exist
for d in "$SESSION_DIR" "$CONFIRM_DIR" "$TASKS_DIR" "$(dirname "$OFFSET_FILE")" "$(dirname "$LOCKFILE")"; do
    mkdir -p "$d"
    chmod 700 "$d" || true
done

# create logs if not present
touch "$LOG_FILE" "$DEBUG_LOG"
chmod 600 "$LOG_FILE" "$DEBUG_LOG" || true

# color codes for interactive usage
C_BOLD="\033[1m"; C_GREEN="\033[32m"; C_YELLOW="\033[33m"; C_BLUE="\033[34m"; C_RED="\033[31m"; C_RESET="\033[0m"

# ----------------
# Safety & basic helpers
# ----------------
_log_json() {
    # simple JSON-ish logging (avoid jq dependency for logging)
    local level="${1:-INFO}"; shift
    local msg="${*:-}"
    local ts; ts=$(date --iso-8601=seconds)
    local host; host=$(hostname --short)
    printf '%s\n' "{\"ts\":\"$ts\",\"host\":\"$host\",\"level\":\"$level\",\"pid\":$$,\"msg\":\"$(echo "$msg" | sed 's/"/\\"/g')\"}" >> "$LOG_FILE"
}
pro_log() { _log_json "$@"; if [ "$1" = "ERROR" ] || [ "$1" = "WARN" ]; then printf '%s\n' "{\"ts\":\"$(date --iso-8601=seconds)\",\"level\":\"$1\",\"msg\":\"$2\"}" >> "$DEBUG_LOG"; fi; }

die() { pro_log "ERROR" "$*"; echo -e "${C_RED}[ERROR]${C_RESET} $*" >&2; exit 1; }

safe_mktemp_dir() { mktemp -d --tmpdir "$SESSION_DIR/tmp.XXXXXX"; }

# urlencode helper
urlencode() {
    local s="$*"; local out=""; local i c hex
    for ((i=0;i<${#s};i++)); do c=${s:i:1}; case $c in [a-zA-Z0-9.~_-]) out+="$c";; *) printf -v hex '%%%02X' "'$c"; out+="$hex";; esac; done
    printf '%s' "$out"
}

# safe-run wrapper capturing output and exit code
safe_run() {
    # usage: safe_run <label> -- command args...
    local label="$1"; shift
    local out; out=$( { "$@" ; } 2>&1 ) || { pro_log "ERROR" "Command failed ($label): $* -> rc=$?"; printf '%s\n' "$out" >> "$DEBUG_LOG"; return 1; }
    pro_log "INFO" "Command succeeded ($label): $*"
    printf '%s' "$out"
}

# curl with retries/backoff + jitter, returns response body
curl_retry() {
    local url="$1"; shift
    local tries="${RETRY_TRIES:-3}"; local delay="${RETRY_DELAY:-2}"
    local attempt=0
    local resp=""
    while [ $attempt -lt $tries ]; do
        attempt=$((attempt+1))
        resp=$(curl --silent --show-error --fail --max-time 20 "$@" "$url" 2>&1) && break
        pro_log "WARN" "curl attempt $attempt failed for $url: $(echo "$resp" | sed 's/"/\\"/g')"
        sleep $((delay * attempt + (RANDOM % 3)))
    done
    if [ -z "$resp" ]; then pro_log "ERROR" "curl all attempts failed for $url"; return 1; fi
    printf '%s' "$resp"
}

# push debug package to telegram (rate-limited)
_last_fail_notify_ts_file="/run/linux_secure_manager/last_fail_notify.ts"
send_debug_log_if_allowed() {
    load_config
    # require telegram configured
    if [ -z "${TELEGRAM_API:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then pro_log "WARN" "Not sending debug log: Telegram not configured"; return; fi
    local now; now=$(date +%s)
    local last=0
    if [ -f "$_last_fail_notify_ts_file" ]; then last=$(cat "$_last_fail_notify_ts_file" 2>/dev/null || echo 0); fi
    if [ $((now - last)) -lt $FAIL_NOTIFY_COOLDOWN ]; then pro_log "WARN" "Skipping debug push; cooldown not expired"; return; fi
    # create a tar.gz of debug artifacts
    local tmpdir; tmpdir=$(mktemp -d)
    ( cp -a "$LOG_FILE" "$DEBUG_LOG" "$CONFIG_FILE" "$SCRIPT_PATH" "$tmpdir" 2>/dev/null || true )
    tar -C "$tmpdir" -czf "${tmpdir}/debug_bundle.tgz" . || true
    # send via sendDocument
    sendDocument "${tmpdir}/debug_bundle.tgz" "LSM debug bundle from $(hostname) at $(date --iso-8601=seconds)"
    # update timestamp
    date +%s > "$_last_fail_notify_ts_file"
    rm -rf "$tmpdir" || true
    pro_log "INFO" "Debug bundle sent to Telegram"
}

# ----------------
# CONFIG handling (external file)
# ----------------
load_config() {
    if [ -f "$CONFIG_FILE" ]; then
        # shellcheck disable=SC1090
        # shellcheck disable=SC1091
        . "$CONFIG_FILE"
    else
        TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
        TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
        ENABLE_AUTO_MAINTENANCE="${ENABLE_AUTO_MAINTENANCE:-1}"
        DEPLOY_PROJECTS="${DEPLOY_PROJECTS:-}" # JSON-like or colon separated mapping (see comments)
        ALLOWLIST_CMDS="${ALLOWLIST_CMDS:-}"   # space separated allowed commands for /cmd (empty means allow all - use with caution)
    fi
    if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then TELEGRAM_API="https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}"; else TELEGRAM_API=""; fi
}

save_config_atomic() {
    local tmp; tmp=$(mktemp -u "${CONFIG_FILE}.tmp.XXXXXX")
    cat > "$tmp" <<EOF
# Linux Secure Manager confidential config (permissions 600)
TELEGRAM_BOT_TOKEN="${TELEGRAM_BOT_TOKEN:-}"
TELEGRAM_CHAT_ID="${TELEGRAM_CHAT_ID:-}"
ENABLE_AUTO_MAINTENANCE="${ENABLE_AUTO_MAINTENANCE:-1}"
DEPLOY_PROJECTS="${DEPLOY_PROJECTS:-}"
ALLOWLIST_CMDS="${ALLOWLIST_CMDS:-}"
EOF
    chmod 600 "$tmp" || true
    chown root:root "$tmp" 2>/dev/null || true
    if ! mv -f "$tmp" "$CONFIG_FILE"; then
        pro_log "ERROR" "Failed to write config via mv (permission or immutable?)"
        echo "mv failed when writing config; dumping diagnostics" >> "$DEBUG_LOG"
        lsattr "$CONFIG_FILE" 2>/dev/null >> "$DEBUG_LOG" || true
        ls -la "$(dirname "$CONFIG_FILE")" >> "$DEBUG_LOG" 2>/dev/null || true
        die "Failed to save config. See $DEBUG_LOG"
    fi
    chmod 600 "$CONFIG_FILE"
    pro_log "INFO" "Config saved to $CONFIG_FILE"
}

# ----------------
# Telegram helpers (robust)
# ----------------
curl_post() { curl --silent --show-error --fail --max-time 20 "$@" || true; }

telegram_send_raw() {
    local method="$1"; shift
    if [ -z "${TELEGRAM_API:-}" ]; then pro_log "WARN" "telegram_send_raw: not configured"; return 1; fi
    local resp
    resp=$(curl_retry "${TELEGRAM_API}/${method}" "$@") || return 1
    printf '%s' "$resp"
}

telegram_send_message() {
    # send long messages chunked to avoid Telegram size issues
    local txt="$1"
    load_config
    if [ -z "${TELEGRAM_API:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then pro_log "WARN" "telegram_send_message: not configured"; return 1; fi
    # chunk smartly on newlines
    local full="$txt"; local chunk
    while [ -n "$full" ]; do
        if [ ${#full} -le $MAX_TELEGRAM_MESSAGE ]; then
            chunk="$full"; full=""
        else
            # try split at last newline before limit
            local prefix=${full:0:$MAX_TELEGRAM_MESSAGE}
            local cutpos; cutpos=$(echo "$prefix" | awk 'BEGIN{pos=0} {for(i=1;i<=length($0);i++) if(substr($0,i,1)=="\n") pos=i} END{print pos}')
            if [ -n "$cutpos" ] && [ "$cutpos" -gt 0 ]; then
                chunk=${full:0:$cutpos}
                full=${full:$cutpos}
            else
                chunk=${full:0:$MAX_TELEGRAM_MESSAGE}
                full=${full:$MAX_TELEGRAM_MESSAGE}
            fi
        fi
        # send chunk
        telegram_send_raw "sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=${chunk}" -d "parse_mode=Markdown" >/dev/null 2>&1 || pro_log "WARN" "telegram send chunk failed"
        sleep 0.25
    done
    return 0
}

telegram_send_file() {
    local file="$1"; local caption="${2:-File}"
    load_config
    if [ -z "${TELEGRAM_API:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then pro_log "WARN" "telegram_send_file: not configured"; return 1; fi
    if [ ! -f "$file" ]; then pro_log "ERROR" "telegram_send_file: file not found $file"; return 1; fi
    local size; size=$(stat -c%s "$file" 2>/dev/null || echo 0)
    if [ "$size" -gt 46900000 ]; then pro_log "WARN" "file too big to send: $file"; return 1; fi
    curl --silent --show-error --fail --max-time 120 -X POST "${TELEGRAM_API}/sendDocument" -F "chat_id=${TELEGRAM_CHAT_ID}" -F "document=@${file}" -F "caption=${caption}" >/dev/null 2>&1 || pro_log "WARN" "telegram_send_file failed for $file"
    return 0
}

sendDocument() { telegram_send_file "$@"; }
sendMessage() { telegram_send_message "$*"; }

sendAction() {
    local action="$1"
    [ -n "${TELEGRAM_API:-}" ] || return 1
    curl_post -X POST "${TELEGRAM_API}/sendChatAction" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "action=${action}" >/dev/null 2>&1 || true
}

answerCallback() {
    local cb_id="$1"; local text="$2"
    curl_post -X POST "${TELEGRAM_API}/answerCallbackQuery" -d "callback_query_id=${cb_id}" -d "text=${text}" >/dev/null 2>&1 || pro_log "WARN" "answerCallback failed"
}

# ----------------
# Confirmation tokens
# ----------------
create_confirmation() {
    local action="$1"
    local code; code=$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')
    local file="${CONFIRM_DIR}/${code}"
    local payload; payload=$(printf '{"ts":%d,"cmd":"%s"}' "$(date +%s)" "$(printf '%s' "$action" | base64 -w0)")
    printf '%s' "$payload" > "$file"
    chmod 600 "$file"
    pro_log "INFO" "Created confirmation code $code for action"
    sendMessage "⚠️ *Confirmation Required*\nReply with:\n\`/confirm $code\`\nThis expires in 15 minutes."
}

execute_confirmation() {
    local code="$1"
    local file="${CONFIRM_DIR}/${code}"
    if [ ! -f "$file" ]; then sendMessage "❌ Invalid or expired confirmation code."; pro_log "WARN" "Invalid confirmation attempt $code"; return; fi
    local payload; payload=$(cat "$file")
    local ts; ts=$(echo "$payload" | jq -r '.ts' 2>/dev/null || echo 0)
    local b64; b64=$(echo "$payload" | jq -r '.cmd' 2>/dev/null || echo "")
    if [ -z "$ts" ] || [ -z "$b64" ]; then rm -f "$file"; sendMessage "❌ Invalid confirmation data"; return; fi
    local now; now=$(date +%s)
    if [ $((now - ts)) -gt 900 ]; then rm -f "$file"; sendMessage "❌ Confirmation expired."; pro_log "WARN" "Expired confirmation $code"; return; fi
    rm -f "$file"
    local cmd; cmd=$(printf '%s' "$b64" | base64 -d -w0)
    pro_log "INFO" "Executing confirmed command (background): $cmd"
    local out="${TASKS_DIR}/confirm_${code}.out"
    ( bash -lc "$cmd" >"$out" 2>&1 || true; sendDocument "$out" "Confirmed action output"; rm -f "$out" 2>/dev/null || true ) & disown
    sendMessage "✅ Action executed (output sent when ready)."
    return 0
}

# ----------------
# Async Task Queue (simple)
# ----------------
task_enqueue() {
    # usage: task_enqueue <label> -- <bash command>
    local label="$1"; shift
    shift || true
    local cmd="$*"
    local id; id=$(date +%s%N)
    local workdir="${TASKS_DIR}/${id}"
    mkdir -p "$workdir"
    printf '{"id":"%s","label":"%s","cmd":"%s","ts":%d,"status":"queued"}' "$id" "$label" "$(printf '%s' "$cmd" | sed 's/"/\\"/g')" "$(date +%s)" > "${workdir}/meta.json"
    # worker will pick it; we spawn a background runner here up to concurrency
    ( cd "$workdir"; bash -lc "$cmd" >"${workdir}/out.txt" 2>&1 || true; printf '{"id":"%s","status":"done","finished":%d}' "$id" "$(date +%s)" > "${workdir}/done.json"; sendDocument "${workdir}/out.txt" "Task ${label} output"; rm -rf "$workdir" ) & disown
    pro_log "INFO" "Enqueued task $id label=$label"
    echo "$id"
}

# ----------------
# deployments
# ----------------
# DEPLOY PROJECTS structure (string): "name|git_url|branch|build_cmd|restart_cmd:..."
# Example: DEPLOY_PROJECTS="app1|git@github.com:org/app.git|main|npm ci && npm run build|systemctl restart app1.service:app2|https://..|main|./build.sh|docker-compose -f /opt/app/docker-compose.yml up -d"
# The script will parse this string; for production you can use a JSON file or per-project config files.
parse_deploy_projects() {
    # returns list of project names
    load_config
    IFS=':' read -ra projs <<< "${DEPLOY_PROJECTS:-}"
    local names=()
    for p in "${projs[@]}"; do
        local name; name=$(echo "$p" | cut -d'|' -f1)
        [ -n "$name" ] && names+=("$name")
    done
    printf '%s\n' "${names[@]}"
}

deploy_info_for() {
    # returns raw line for project name
    local want="$1"
    IFS=':' read -ra projs <<< "${DEPLOY_PROJECTS:-}"
    for p in "${projs[@]}"; do
        local name; name=$(echo "$p" | cut -d'|' -f1)
        if [ "$name" = "$want" ]; then printf '%s' "$p"; return 0; fi
    done
    return 1
}

_safe_git_clone_release() {
    local git_url="$1"; local branch="$2"; local release_dir="$3"
    safe_run "git_clone_${release_dir}" git clone --depth 1 --branch "$branch" "$git_url" "$release_dir" || return 1
}

_release_cleanup_keep() {
    local root="$1"; local keep="${2:-5}"
    mkdir -p "$root"
    ls -1dt "$root"/* 2>/dev/null | tail -n +$((keep+1)) | xargs -r rm -rf || true
}

deploy_project() {
    # deploy_project <name>
    local name="$1"
    local line; line=$(deploy_info_for "$name") || { sendMessage "❌ Unknown project: $name"; return 1; }
    IFS='|' read -r proj_name git_url branch build_cmd restart_cmd <<< "$line"
    branch="${branch:-main}"
    pro_log "INFO" "Starting deploy for $proj_name ($git_url#$branch)"
    sendMessage "🚀 Deploy starting: ${proj_name}"
    # create release dir
    local ts; ts=$(date +%Y%m%d%H%M%S)
    local release_dir="${RELEASES_ROOT}/${proj_name}/${ts}"
    mkdir -p "$release_dir"
    # clone shallow
    if ! _safe_git_clone_release "$git_url" "$branch" "$release_dir"; then
        pro_log "ERROR" "git clone failed for $proj_name"
        sendMessage "❌ Deploy failed: git clone error for ${proj_name}"
        send_debug_log_if_allowed
        return 1
    fi
    # run build if present
    if [ -n "${build_cmd:-}" ]; then
        pro_log "INFO" "Running build for $proj_name: $build_cmd"
        if ! ( cd "$release_dir" && bash -lc "$build_cmd" ) >"${release_dir}/build.out" 2>&1; then
            pro_log "ERROR" "Build failed for $proj_name"
            sendDocument "${release_dir}/build.out" "Build output for ${proj_name} (failed)"
            sendMessage "❌ Build failed for ${proj_name}; uploaded logs."
            # cleanup failing release
            rm -rf "$release_dir"
            send_debug_log_if_allowed
            return 1
        fi
    fi
    # atomic symlink switch: create new symlink then swap
    mkdir -p "$(dirname "$CURRENT_SYMLINK")" "$(dirname "$RELEASES_ROOT")"
    local project_current_symlink="${RELEASES_ROOT}/${proj_name}/current"
    ln -sfn "$release_dir" "$project_current_symlink"
    # update global current symlink if desired
    ln -sfn "$project_current_symlink" "${CURRENT_SYMLINK}/${proj_name}" 2>/dev/null || true
    # run restart command
    if [ -n "${restart_cmd:-}" ]; then
        pro_log "INFO" "Running restart command for $proj_name: $restart_cmd"
        if ! bash -lc "$restart_cmd" >"${release_dir}/restart.out" 2>&1; then
            pro_log "ERROR" "Restart command failed for $proj_name"
            sendDocument "${release_dir}/restart.out" "Restart output for ${proj_name} (failed)"
            sendMessage "❌ Restart failed for ${proj_name}; uploaded logs."
            send_debug_log_if_allowed
            return 1
        fi
    fi
    # cleanup older releases
    _release_cleanup_keep "${RELEASES_ROOT}/${proj_name}" "$RELEASE_RETENTION"
    pro_log "INFO" "Deploy succeeded for $proj_name"
    sendMessage "✅ Deploy completed for ${proj_name}"
    # record last deploy
    mkdir -p /var/lib/linux_secure_manager
    echo "{\"project\":\"${proj_name}\",\"release\":\"${ts}\",\"ts\":$(date +%s)}" > "/var/lib/linux_secure_manager/last_deploy_${proj_name}.json"
    return 0
}

rollback_project() {
    # rollback_project <name>
    local name="$1"
    local releases_dir="${RELEASES_ROOT}/${name}"
    if [ ! -d "$releases_dir" ]; then sendMessage "❌ No releases for project $name"; return 1; fi
    local releases; releases=($(ls -1dt "$releases_dir"/* 2>/dev/null))
    if [ ${#releases[@]} -lt 2 ]; then sendMessage "❌ Not enough releases to rollback"; return 1; fi
    local current="${releases[0]}"
    local previous="${releases[1]}"
    pro_log "INFO" "Preparing rollback for $name: $current -> $previous"
    # require confirmation
    create_confirmation "ln -sfn ${previous} ${RELEASES_ROOT}/${name}/current && echo rollback && logger -t linux_secure_manager 'Rolled back ${name} to ${previous}'"
    sendMessage "⚠️ Rollback prepared. Confirm with /confirm <code> to perform the rollback."
    return 0
}

# ----------------
# Docker helpers
# ----------------
docker_pull_restart() {
    local image="$1"
    if ! command -v docker >/dev/null 2>&1; then sendMessage "❌ Docker not installed"; return 1; fi
    pro_log "INFO" "Pulling Docker image $image"
    if ! docker pull "$image" >>"$LOG_FILE" 2>&1; then pro_log "ERROR" "docker pull failed for $image"; sendMessage "❌ Docker pull failed for $image"; send_debug_log_if_allowed; return 1; fi
    # restart containers using the image (best-effort)
    local containers; containers=$(docker ps -q --filter ancestor="$image")
    if [ -n "$containers" ]; then
        for cid in $containers; do
            docker restart "$cid" >>"$LOG_FILE" 2>&1 || pro_log "WARN" "Failed restarting $cid"
        done
    fi
    sendMessage "✅ Docker image $image pulled and containers restarted."
    return 0
}

docker_container_logs() {
    local cid="$1"; local lines="${2:-200}"
    if ! command -v docker >/dev/null 2>&1; then sendMessage "❌ Docker not installed"; return 1; fi
    if ! docker ps -q --no-trunc | grep -q "$cid"; then sendMessage "❌ Container $cid not running"; return 1; fi
    local tmp; tmp=$(mktemp)
    docker logs --tail "$lines" "$cid" > "$tmp" 2>&1 || true
    sendDocument "$tmp" "Logs for container $cid"
    rm -f "$tmp"
}

# ----------------
# Healthchecks & metrics
# ----------------
write_health_file() {
    local hf="/var/run/linux_secure_manager/health.json"
    mkdir -p "$(dirname "$hf")"
    local ts; ts=$(date +%s)
    local uptime; uptime=$(uptime -p || echo "unknown")
    local mem; mem=$(free -m | awk 'NR==2{printf "%.2f%%", $3*100/$2 }' || echo "na")
    local disk; disk=$(df -h / | awk 'NR==2{print $5}' || echo "na")
    cat > "$hf" <<EOF
{"ts":$ts,"uptime":"$uptime","mem":"$mem","disk":"$disk"}
EOF
    chmod 644 "$hf"
}

get_metrics_summary() {
    local summary
    summary="$(hostname) - $(date --iso-8601=seconds)
$(uptime -p) | mem: $(free -m | awk 'NR==2{printf "%.2f%%", $3*100/$2 }') | disk: $(df -h / | awk 'NR==2{print $5}')
"
    printf '%s' "$summary"
}

# ----------------
# Interactive UI & Terminal Manager
# ----------------
terminal_main_menu() {
    # builds inline keyboard; use JSON text for reply_markup
    local keyboard='{"inline_keyboard":[[{"text":"Uptime","callback_data":"term:uptime"},{"text":"Top","callback_data":"term:top"}],[{"text":"Disk","callback_data":"term:disk"},{"text":"Memory","callback_data":"term:mem"}],[{"text":"Deploy Menu","callback_data":"term:deploy"}]]}'
    # telegram sendMessage with keyboard
    telegram_send_raw "sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=*Terminal Manager*" -d "reply_markup=${keyboard}" -d "parse_mode=Markdown" >/dev/null 2>&1 || pro_log "WARN" "Failed to send terminal keyboard"
}

handle_terminal_callback() {
    local cb_id="$1"; local cb_data="$2"
    case "$cb_data" in
        term:uptime)
            answerCallback "$cb_id" "Fetching uptime..."
            local out; out=$(uptime -p)
            sendMessage "*Uptime:* \`$out\`"
            ;;
        term:top)
            answerCallback "$cb_id" "Fetching top..."
            local out; out=$(top -b -n1 | head -n 20)
            sendOutputAsFile "top" "$out"
            ;;
        term:disk)
            answerCallback "$cb_id" "Disk..."
            local out; out=$(df -h)
            sendOutputAsFile "df" "$out"
            ;;
        term:mem)
            answerCallback "$cb_id" "Memory..."
            local out; out=$(free -h)
            sendOutputAsFile "free" "$out"
            ;;
        term:deploy)
            answerCallback "$cb_id" "Open deploy menu..."
            deploy_menu_keyboard
            ;;
        *)
            answerCallback "$cb_id" "Unknown action."
            pro_log "WARN" "Unknown callback: $cb_data"
            ;;
    esac
}

deploy_menu_keyboard() {
    # construct keyboard based on configured projects
    local names; names=($(parse_deploy_projects))
    local json='{"inline_keyboard":['
    for n in "${names[@]}"; do
        json+='[{"text":"Deploy '"$n"'","callback_data":"deploy:run:'"$n"'"},{"text":"Status '"$n"'","callback_data":"deploy:status:'"$n"'"}],'
    done
    json+='[{"text":"List Projects","callback_data":"deploy:list"}]'
    json+=']}'
    telegram_send_raw "sendMessage" -d "chat_id=${TELEGRAM_CHAT_ID}" -d "text=*Deploy Manager*" -d "reply_markup=${json}" -d "parse_mode=Markdown" >/dev/null 2>&1 || pro_log "WARN" "Failed to send deploy menu"
}

handle_deploy_callback() {
    local cb="$1"; local data="$2"
    # cb_data formats: deploy:run:<name>, deploy:status:<name>, deploy:list
    case "$data" in
        deploy:list)
            answerCallback "$cb" "Fetching project list..."
            local list; list=$(parse_deploy_projects | xargs echo)
            sendMessage "*Projects:* $list"
            ;;
        deploy:status:*)
            local name="${data#deploy:status:}"
            answerCallback "$cb" "Checking status for $name..."
            if [ -f "/var/lib/linux_secure_manager/last_deploy_${name}.json" ]; then
                sendDocument "/var/lib/linux_secure_manager/last_deploy_${name}.json" "Last deploy info for ${name}"
            else
                sendMessage "No deploy record for ${name}"
            fi
            ;;
        deploy:run:*)
            local name="${data#deploy:run:}"
            answerCallback "$cb" "Preparing deploy for $name..."
            # confirmation required
            create_confirmation "bash -lc '${SCRIPT_PATH} --deploy-exec ${name}'"
            sendMessage "⚠️ Deploy prepared for ${name}. Confirm with /confirm <code> to start."
            ;;
        *)
            answerCallback "$cb" "Unknown deploy action."
            pro_log "WARN" "Unknown deploy callback: $data"
            ;;
    esac
}

# ----------------
# Deploy-exec (internal)
# ----------------
deploy_exec() {
    local name="$1"
    deploy_project "$name"
}

# ----------------
# Self update (with signature placeholder)
# ----------------
self_update_script() {
    sendMessage "🔄 Checking for updates..."
    local tmpd; tmpd=$(mktemp -d)
    local tmpf="${tmpd}/lsm.new"
    if ! curl_retry "$REPO_URL" -o "$tmpf"; then
        sendMessage "❌ Download failed"
        rm -rf "$tmpd"
        return 1
    fi
    # TODO: signature verification (if you publish signed releases)
    chmod +x "$tmpf"
    mv -f "$tmpf" "$SCRIPT_PATH"
    chmod +x "$SCRIPT_PATH"
    sendMessage "✅ Script updated. Restart service if necessary."
    rm -rf "$tmpd"
    pro_log "INFO" "Self-update completed"
}

# ----------------
# Systemd service installer/uninstaller
# ----------------
install_systemd_service() {
    if [ "$(id -u)" -ne 0 ]; then die "install_systemd_service: must be root"; fi
    cat > "$SYSTEMD_UNIT" <<EOF
[Unit]
Description=Linux Secure Manager (Telegram Listener)
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
    pro_log "INFO" "Installed systemd unit and started service"
    sendMessage "✅ Service installed and started (linux_secure_manager.service)."
}

remove_systemd_service() {
    systemctl stop linux_secure_manager.service 2>/dev/null || true
    systemctl disable linux_secure_manager.service 2>/dev/null || true
    rm -f "$SYSTEMD_UNIT"
    systemctl daemon-reload
    pro_log "INFO" "Removed systemd service"
    sendMessage "✅ Service removed"
}

# ----------------
# logrotate generator
# ----------------
install_logrotate() {
    cat > "$LOGROTATE_FILE" <<EOF
${LOG_FILE} ${DEBUG_LOG} {
    daily
    rotate 14
    compress
    missingok
    notifempty
    create 0600 root root
    postrotate
        /bin/systemctl kill -s USR1 linux_secure_manager.service >/dev/null 2>&1 || true
    endscript
}
EOF
    pro_log "INFO" "Installed logrotate config"
    sendMessage "✅ logrotate configuration installed for LSM."
}

# ----------------
# Uninstall helper (generates safe uninstall script)
# ----------------
generate_uninstall_script() {
    local out="/tmp/lsm_uninstall.sh"
    cat > "$out" <<'UNINST'
#!/usr/bin/env bash
set -euo pipefail
systemctl stop linux_secure_manager.service 2>/dev/null || true
systemctl disable linux_secure_manager.service 2>/dev/null || true
rm -f /etc/systemd/system/linux_secure_manager.service
systemctl daemon-reload
rm -f /etc/linux_secure_manager.conf
rm -rf /var/log/linux_secure_manager* /run/linux_secure_manager /var/lib/linux_secure_manager
echo "Linux Secure Manager uninstalled (files removed)."
UNINST
    chmod +x "$out"
    echo "$out"
}

# ----------------
# Input validators & safe exec for /cmd
# ----------------
is_command_allowed() {
    load_config
    # if ALLOWLIST_CMDS is empty -> allow all (dangerous). Otherwise check words in allowlist
    if [ -z "${ALLOWLIST_CMDS:-}" ]; then return 0; fi
    local cmd="$1"
    for allow in $ALLOWLIST_CMDS; do
        if echo "$cmd" | grep -qE "^\s*${allow}(\s|$)"; then return 0; fi
    done
    return 1
}

run_ad_hoc_command() {
    local cmd="$*"
    if [ -z "$cmd" ]; then sendMessage "Usage: /cmd <command>"; return 1; fi
    if ! is_command_allowed "$cmd"; then sendMessage "❌ Command not allowed by policy"; pro_log "WARN" "Blocked command by policy: $cmd"; return 1; fi
    sendMessage "⏳ Running command..."
    local out="${TASKS_DIR}/cmd_$(date +%s).out"
    ( bash -lc "$cmd" >"$out" 2>&1 || true; sendDocument "$out" "Command output: $(echo "$cmd" | cut -c1-80)"; rm -f "$out" ) & disown
    return 0
}

# ----------------
# Listener (main long-running loop)
# ----------------
listen_for_commands() {
    load_config
    if [ -z "${TELEGRAM_API:-}" ] || [ -z "${TELEGRAM_CHAT_ID:-}" ]; then pro_log "ERROR" "Listener: not configured"; echo "Not configured. Run --setup" >&2; exit 1; fi
    for b in curl jq git; do
        if ! command -v $b >/dev/null 2>&1; then pro_log "WARN" "Missing dependency: $b"; fi
    done
    # avoid multiple listeners
    exec 9>"$LOCKFILE"
    if ! flock -n 9; then pro_log "WARN" "Listener already running"; exit 0; fi
    preflight_checks
    pro_log "INFO" "Entering listen loop"
    while true; do
        check_background_process
        find "$CONFIRM_DIR" -type f -mmin +20 -delete || true
        local OFFSET; OFFSET=$(cat "$OFFSET_FILE" 2>/dev/null || echo 0)
        local resp; resp=$(curl_retry "${TELEGRAM_API}/getUpdates?offset=$((OFFSET+1))&timeout=30") || resp=""
        if [ -z "$resp" ]; then sleep 1; continue; fi
        # iterate results
        echo "$resp" | jq -c '.result[]' 2>/dev/null | while read -r item; do
            # handle callback_query if present
            local cbq; cbq=$(echo "$item" | jq -c '.callback_query' 2>/dev/null || echo null)
            if [ "$cbq" != "null" ]; then
                local cb_id; cb_id=$(echo "$cbq" | jq -r '.id' 2>/dev/null || echo "")
                local cb_data; cb_data=$(echo "$cbq" | jq -r '.data' 2>/dev/null || echo "")
                local cb_user; cb_user=$(echo "$cbq" | jq -r '.from.id' 2>/dev/null || echo "")
                if [ "$cb_user" = "$TELEGRAM_CHAT_ID" ]; then
                    pro_log "INFO" "Callback received: $cb_data"
                    case "$cb_data" in
                        term:*) handle_terminal_callback "$cb_id" "$cb_data" ;;
                        deploy:*) handle_deploy_callback "$cb_id" "$cb_data" ;;
                        *) answerCallback "$cb_id" "Unknown action" ;;
                    esac
                else
                    pro_log "WARN" "Unauthorized callback by $cb_user"
                fi
                local upid; upid=$(echo "$item" | jq -r '.update_id' 2>/dev/null || echo "")
                echo "$upid" > "$OFFSET_FILE"
                continue
            fi
            local msg; msg=$(echo "$item" | jq -c '.message' 2>/dev/null || echo null)
            if [ "$msg" = "null" ]; then local uid; uid=$(echo "$item" | jq -r '.update_id' 2>/dev/null || echo "0"); echo "$uid" > "$OFFSET_FILE"; continue; fi
            local chat_id; chat_id=$(echo "$msg" | jq -r '.chat.id' 2>/dev/null || echo "")
            local text; text=$(echo "$msg" | jq -r '.text' 2>/dev/null || echo "")
            local reply_msg; reply_msg=$(echo "$msg" | jq -c '.reply_to_message' 2>/dev/null || echo null)
            if [ "$chat_id" = "$TELEGRAM_CHAT_ID" ]; then
                pro_log "INFO" "Received: $text"
                read -r command arg1 arg2 <<<"$text" || true
                case "$command" in
                    "/start"|"/help")
                        sendMessage "*Welcome.* Use /terminal for UI, /deploy for deploy, /cmd for commands. Use /help for commands."
                        ;;
                    "/status")
                        sendMessage "$(get_metrics_summary)"
                        ;;
                    "/checkupdates") check_updates ;;
                    "/runupdates") run_updates ;;
                    "/top") manage_processes "top" ;;
                    "/getlog") get_log_file "$arg1" ;;
                    "/ufw") manage_firewall "$arg1" "$arg2" ;;
                    "/user") manage_user "$arg1" "$arg2" ;;
                    "/kill") manage_processes "kill" "$arg1" ;;
                    "/upload") upload_file_to_telegram "$arg1" ;;
                    "/download") download_file_from_telegram "$reply_msg" ;;
                    "/reboot") create_confirmation "/sbin/reboot" ;;
                    "/shutdown") create_confirmation "/sbin/shutdown -h now" ;;
                    "/confirm") execute_confirmation "$arg1" ;;
                    "/selfupdate") self_update_script ;;
                    "/terminal") terminal_main_menu ;;
                    "/deploy") deploy_menu_keyboard ;;
                    "/shell") start_shell_session ;;
                    "/exit") if is_shell_active; then stop_shell_session; sendMessage "Shell terminated."; else sendMessage "No active shell."; fi ;;
                    "/cmd")
                        # everything after /cmd is the command; preserve whitespace
                        local payload="${text#*/cmd }"
                        run_ad_hoc_command "$payload"
                        ;;
                    *)
                        if is_shell_active; then execute_shell_command "$text"; else sendMessage "❓ Unknown. Use /help or /terminal."; fi
                        ;;
                esac
            else
                pro_log "WARN" "Unauthorized message from $chat_id"
            fi
            local uid; uid=$(echo "$item" | jq -r '.update_id' 2>/dev/null || echo "")
            echo "$uid" > "$OFFSET_FILE"
        done
    done
}

# ----------------
# utilities referenced earlier but not yet defined (get_log_file, manage_firewall, manage_user, manage_processes, upload/download, check_background_process)
# ----------------
check_background_process() {
    if [ -f "${SESSION_DIR}/pid.txt" ]; then
        local pid; pid=$(cat "${SESSION_DIR}/pid.txt" 2>/dev/null || echo "")
        if [ -n "$pid" ] && ! kill -0 "$pid" 2>/dev/null; then
            local out; out=$(cat "${SESSION_DIR}/output.log" 2>/dev/null || "")
            sendDocument <(printf "%s" "$out") "Shell output" 2>/dev/null || sendOutputAsFile "shellcmd" "$out"
            sendMessage "$(get_shell_prompt)"
            rm -f "${SESSION_DIR}/pid.txt" "${SESSION_DIR}/output.log"
        fi
    fi
}

get_log_file() {
    local path="${1:-/var/log/syslog}"
    local real; real=$(realpath -m "$path")
    if [[ "$real" != /var/log/* ]]; then sendMessage "❌ Access denied. Only /var/log/* allowed"; pro_log "WARN" "get_log_file out of bounds: $path"; return 1; fi
    if [ -r "$real" ]; then
        local out; out=$(tail -n 1000 "$real" 2>/dev/null || true)
        sendOutputAsFile "log:${real}" "$out"
    else sendMessage "❌ File not readable"; fi
}

manage_firewall() {
    local action="$1"; local port="$2"
    if ! command -v ufw >/dev/null 2>&1; then sendMessage "❌ UFW not installed"; return 1; fi
    case "$action" in
        status) local s; s=$(ufw status verbose 2>&1 || true); sendOutputAsFile "ufw status" "$s" ;;
        enable) ufw --force enable >/dev/null 2>&1 && sendMessage "✅ UFW enabled" || sendMessage "❌ Failed to enable" ;;
        disable) ufw --force disable >/dev/null 2>&1 && sendMessage "✅ UFW disabled" || sendMessage "❌ Failed to disable" ;;
        allow|deny)
            if [[ ! "$port" =~ ^[0-9]+(/[a-z]+)?$ ]]; then sendMessage "❌ Invalid port format"; return 1; fi
            ufw "$action" "$port" >/dev/null 2>&1 && sendMessage "✅ Rule applied: $action $port" || sendMessage "❌ Failed to apply rule"
            ;;
        *) sendMessage "Usage: /ufw status|enable|disable|allow|deny <port>" ;;
    esac
}

manage_user() {
    local action="$1"; local username="$2"
    if ! [[ "$username" =~ ^[a-z_][a-z0-9_-]{1,31}$ ]]; then sendMessage "❌ Invalid username"; pro_log "WARN" "Invalid username: $username"; return 1; fi
    if [ "$action" = "add" ]; then
        local password; password=$(tr -dc 'A-Za-z0-9!@%_-+=' </dev/urandom | head -c16)
        if useradd -m -s /bin/bash "$username"; then
            echo "${username}:${password}" | chpasswd
            sendMessage "✅ User \`${username}\` created.\n\nPassword:\n\`\`\`${password}\`\`\`"
            pro_log "INFO" "Created user $username"
        else
            sendMessage "❌ Failed to create user"
            pro_log "ERROR" "useradd failed $username"
        fi
    elif [ "$action" = "del" ]; then
        create_confirmation "userdel -r ${username} && logger -t linux_secure_manager 'Deleted ${username}'"
        sendMessage "⚠️ Deletion scheduled. Confirm with /confirm <code>."
    else
        sendMessage "Usage: /user add|del <username>"
    fi
}

manage_processes() {
    local action="$1"; local pid="$2"
    if [ "$action" = "top" ]; then local out; out=$(top -b -n1 | head -n 20); sendOutputAsFile "top" "$out"; fi
    if [ "$action" = "kill" ]; then
        if [[ ! "$pid" =~ ^[0-9]+$ ]]; then sendMessage "❌ Invalid PID"; return 1; fi
        create_confirmation "kill -9 ${pid} && logger -t linux_secure_manager 'Killed PID ${pid}'"
        sendMessage "⚠️ Kill scheduled. Confirm with /confirm <code>."
    fi
}

upload_file_to_telegram() {
    local file="$1"
    if [ -z "$file" ]; then sendMessage "Usage: /upload /path/to/file"; return 1; fi
    local real; real=$(realpath -m "$file" 2>/dev/null || echo "")
    if [ -f "$real" ] && [ -r "$real" ]; then sendDocument "$real" "File from $(hostname)"; else sendMessage "❌ File not found or unreadable"; fi
}

download_file_from_telegram() {
    local json="$1"
    local file_id; file_id=$(echo "$json" | jq -r '.document.file_id' 2>/dev/null || echo "")
    local file_name; file_name=$(echo "$json" | jq -r '.document.file_name' 2>/dev/null || echo "")
    local cwd; cwd=$(cat "${SESSION_DIR}/cwd.txt" 2>/dev/null || echo "/root")
    if [ -z "$file_id" ] || [ "$file_id" = "null" ]; then sendMessage "❌ No document"; return 1; fi
    local fjson; fjson=$(curl_post "${TELEGRAM_API}/getFile?file_id=${file_id}" || "")
    local path; path=$(echo "$fjson" | jq -r '.result.file_path' 2>/dev/null || echo "")
    if [ -n "$path" ] && [ "$path" != "null" ]; then curl_post -o "${cwd}/${file_name}" "https://api.telegram.org/file/bot${TELEGRAM_BOT_TOKEN}/${path}" || true; sendMessage "✅ Downloaded ${file_name} to ${cwd}"; else sendMessage "❌ Download failed"; fi
}

# ----------------
# Preflight checks
# ----------------
preflight_checks() {
    # disk space check
    local avail; avail=$(df --output=avail / | tail -1)
    if [ "$avail" -lt 51200 ]; then pro_log "WARN" "Low disk space ($avail KB)"; fi
    # ping telegram
    if ! ping -c1 -W2 api.telegram.org >/dev/null 2>&1; then pro_log "WARN" "Cannot reach api.telegram.org" ; fi
    # dependencies
    for b in curl jq git; do if ! command -v $b >/dev/null 2>&1; then pro_log "WARN" "Missing dependency: $b"; fi; done
}

# ----------------
# Setup wizard
# ----------------
run_setup_wizard() {
    if [ "$(id -u)" -ne 0 ]; then die "Setup must be run as root"; fi
    clear
    echo -e "${C_BOLD}--- Linux Secure Manager Setup Wizard ---${C_RESET}"
    echo
    preflight_checks
    local new_bot new_chat new_auto
    while [ -z "${new_bot:-}" ]; do read -rp "Enter your Telegram Bot Token: " new_bot || true; if ! [[ "$new_bot" =~ ^[0-9]+:[A-Za-z0-9_-]{20,}$ ]]; then echo "Invalid token format"; new_bot=""; fi; done
    echo "[INFO] To find your chat ID: send a message to your bot and visit: https://api.telegram.org/bot${new_bot}/getUpdates"
    while ! [[ "${new_chat:-}" =~ ^[0-9]+$ ]]; do read -rp "Enter your numerical Telegram Chat ID: " new_chat || true; done
    read -rp "Enable scheduled automatic updates? (y/n) [default: y]: " new_auto || true
    new_auto="${new_auto:-y}"
    if [[ "$new_auto" =~ ^[Yy]$ ]]; then new_auto=1; else new_auto=0; fi
    TELEGRAM_BOT_TOKEN="$new_bot"
    TELEGRAM_CHAT_ID="$new_chat"
    ENABLE_AUTO_MAINTENANCE="$new_auto"
    # optional projects config prompt (quick)
    read -rp "Do you want to add a deploy project now? (y/N): " addproj || true
    if [[ "$addproj" =~ ^[Yy]$ ]]; then
        read -rp "Project name: " pname || true
        read -rp "Git URL (ssh/https): " pgit || true
        read -rp "Branch (default main): " pbranch || true
        read -rp "Build command (optional): " pbuild || true
        read -rp "Restart command (optional): " prestart || true
        local line="${pname}|${pgit}|${pbranch:-main}|${pbuild}|${prestart}"
        if [ -z "${DEPLOY_PROJECTS:-}" ]; then DEPLOY_PROJECTS="$line"; else DEPLOY_PROJECTS="${DEPLOY_PROJECTS}:$line"; fi
    fi
    save_config_atomic
    load_config
    chmod +x "$SCRIPT_PATH"
    pro_log "INFO" "Setup saved (chat $TELEGRAM_CHAT_ID)"
    # systemd installer prompt
    read -rp "Install systemd service for auto-start? (y/n) [default: y]: " isv || true
    isv="${isv:-y}"
    if [[ "$isv" =~ ^[Yy]$ ]]; then install_systemd_service; fi
    sendMessage "✅ Linux Secure Manager configured on $(hostname)."
    exit 0
}

# ----------------
# Command-line entrypoints
# ----------------
usage() {
    cat <<EOF
Linux Secure Manager - Pro Full
Usage:
  $0 --setup                 Run setup wizard
  $0 --listen                Start listener (daemon mode)
  $0 --install-service       Install systemd service and start
  $0 --remove-service        Remove systemd service
  $0 --install-logrotate     Install logrotate config
  $0 --selfupdate            Self-update script
  $0 --deploy <project>      Deploy project name (internal)
  $0 --deploy-exec <name>    Internal: run deploy exec (used by confirmation)
  $0 --uninstall-generate    Generate uninstall script (path printed)
  $0 --health                Write health file
  $0 --help
EOF
}

# ----------------
# Top-level dispatch
# ----------------
main() {
    if [ "$(id -u)" -ne 0 ]; then echo "This script must be run as root." >&2; exit 1; fi
    load_config
    case "${1:-}" in
        --setup) run_setup_wizard ;;
        --listen) listen_for_commands ;;
        --install-service) install_systemd_service ;;
        --remove-service) remove_systemd_service ;;
        --install-logrotate) install_logrotate ;;
        --selfupdate) self_update_script ;;
        --deploy) shift; deploy_project "$1" ;;
        --deploy-exec) shift; deploy_exec "$1" ;;
        --uninstall-generate) echo "Uninstall script at: $(generate_uninstall_script)"; ;;
        --health) write_health_file ;;
        --help|*) usage ;;
    esac
}

main "$@"
