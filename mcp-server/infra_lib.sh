#!/bin/bash
# Infrastructure Logging & Alerting Library
# Source this in any infra script:
#   COMPONENT_LOG="/path/to/component.log"
#   source "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp/infra_lib.sh"
#
# Provides:
#   infra_info     COMPONENT "message"  — log INFO, no alert
#   infra_warn     COMPONENT "message"  — log WARNING + Telegram alert
#   infra_error    COMPONENT "message"  — log ERROR + Telegram alert
#   infra_critical COMPONENT "message"  — log CRITICAL + Telegram alert
#
# All entries go to:
#   1. Central log: ~/derek/logs/infra.log (all systems)
#   2. Component log: $COMPONENT_LOG (set before sourcing)
#
# Trace IDs: Auto-generated per invocation. Override by setting TRACE_ID before sourcing.

INFRA_LOG_DIR="/Users/YOUR_MAC_USERNAME/derek/logs"
INFRA_LOG="$INFRA_LOG_DIR/infra.log"
INFRA_CHAT_ID="8676483103"

# Generate trace ID: epoch-PID
: "${TRACE_ID:=$(date +%s)-$$}"

mkdir -p "$INFRA_LOG_DIR"

# Lazy-load Derek's bot token for alerts
_INFRA_BOT_TOKEN=""
_infra_bot_token() {
    if [ -z "$_INFRA_BOT_TOKEN" ]; then
        # Read from Derek's Telegram channel .env file
        local envfile="/Users/YOUR_MAC_USERNAME/.claude/channels/telegram/.env"
        if [ -f "$envfile" ]; then
            _INFRA_BOT_TOKEN=$(grep '^TELEGRAM_BOT_TOKEN=' "$envfile" | cut -d= -f2-)
        fi
    fi
    echo "$_INFRA_BOT_TOKEN"
}

# Core structured log writer
# Format: TIMESTAMP | LEVEL | COMPONENT | trace=ID | MESSAGE
_infra_log() {
    local level="$1" component="$2" msg="$3"
    local ts
    ts=$(date '+%Y-%m-%d %H:%M:%S')
    local entry="$ts | $level | $component | trace=$TRACE_ID | $msg"

    echo "$entry" >> "$INFRA_LOG"

    if [ -n "$COMPONENT_LOG" ]; then
        echo "$entry" >> "$COMPONENT_LOG"
    fi
}

# Send Telegram alert
_infra_telegram() {
    local component="$1" msg="$2" severity="$3"
    local token
    token=$(_infra_bot_token)
    [ -z "$token" ] && return 1

    local icon
    case "$severity" in
        CRITICAL) icon="🔴" ;;
        ERROR)    icon="🟠" ;;
        WARNING)  icon="⚠️" ;;
        *)        icon="ℹ️" ;;
    esac

    curl -s -X POST "https://api.telegram.org/bot${token}/sendMessage" \
        -d chat_id="$INFRA_CHAT_ID" \
        -d text="${icon} ${severity} [${component}]
${msg}
trace: ${TRACE_ID}" >/dev/null 2>&1
}

# --- Public API ---

infra_info() {
    _infra_log "INFO" "$1" "$2"
}

infra_warn() {
    _infra_log "WARNING" "$1" "$2"
    _infra_telegram "$1" "$2" "WARNING"
}

infra_error() {
    _infra_log "ERROR" "$1" "$2"
    _infra_telegram "$1" "$2" "ERROR"
}

infra_critical() {
    _infra_log "CRITICAL" "$1" "$2"
    _infra_telegram "$1" "$2" "CRITICAL"
}

# --- State-aware variants (suppress Telegram for pre-onboarded agents) ---
# Usage: infra_warn_user_tier  AGENT COMPONENT "msg"
# If the agent has no liability_accepted.json, logs at INFO level with no alert.
# Once onboarded, behaves like infra_warn / infra_error.

infra_warn_user_tier() {
    local agent="$1" component="$2" msg="$3"
    if [ -f "/Users/YOUR_MAC_USERNAME/${agent}/liability_accepted.json" ]; then
        _infra_log "WARNING" "$component" "$msg"
        _infra_telegram "$component" "$msg" "WARNING"
    else
        _infra_log "INFO" "$component" "$msg [suppressed: $agent not onboarded]"
    fi
}

infra_error_user_tier() {
    local agent="$1" component="$2" msg="$3"
    if [ -f "/Users/YOUR_MAC_USERNAME/${agent}/liability_accepted.json" ]; then
        _infra_log "ERROR" "$component" "$msg"
        _infra_telegram "$component" "$msg" "ERROR"
    else
        _infra_log "INFO" "$component" "$msg [suppressed: $agent not onboarded]"
    fi
}

# --- Session pruning: prevent --continue from loading a bloated conversation ---
# Call before the --continue decision block in each launcher.
# Archives session JSONLs older than MAX_SESSION_AGE_DAYS (default 2).
# If remaining files still exceed MAX_SESSION_BYTES, archives everything.
# Usage: prune_session_if_bloated "$CLAUDE_CONFIG_DIR" "$COMP"
MAX_SESSION_AGE_DAYS="${MAX_SESSION_AGE_DAYS:-2}"
MAX_SESSION_BYTES="${MAX_SESSION_BYTES:-52428800}"  # 50 MB safety net
prune_session_if_bloated() {
    local config_dir="$1" comp="$2"
    local projects_dir="$config_dir/projects"
    [ -d "$projects_dir" ] || return 0

    # Phase 1: archive files older than MAX_SESSION_AGE_DAYS
    local old_files
    old_files=$(find "$projects_dir" -name "*.jsonl" -type f -mtime +"$MAX_SESSION_AGE_DAYS" 2>/dev/null)
    if [ -n "$old_files" ]; then
        local archive="$config_dir/projects-archive-$(date +%Y%m%d%H%M%S)"
        mkdir -p "$archive"
        local count=0
        echo "$old_files" | while IFS= read -r f; do mv "$f" "$archive/" 2>/dev/null; done
        count=$(find "$archive" -name "*.jsonl" -type f 2>/dev/null | wc -l | tr -d ' ')
        infra_info "$comp" "Session pruned: archived $count files older than ${MAX_SESSION_AGE_DAYS}d"
    fi

    # Phase 2: safety net — if remaining files still exceed size limit, archive all
    local remaining_bytes
    remaining_bytes=$(find "$projects_dir" -name "*.jsonl" -type f -exec stat -f %z {} + 2>/dev/null | awk '{s+=$1} END {print s+0}')
    if [ "$remaining_bytes" -gt "$MAX_SESSION_BYTES" ]; then
        local archive="$config_dir/projects-archive-$(date +%Y%m%d%H%M%S)-oversized"
        mkdir -p "$archive"
        find "$projects_dir" -name "*.jsonl" -type f -exec mv {} "$archive/" \; 2>/dev/null
        infra_warn "$comp" "Session pruned (oversized): ${remaining_bytes} bytes exceeded ${MAX_SESSION_BYTES} after age prune — archived all, next boot is fresh"
    fi
}
