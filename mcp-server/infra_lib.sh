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
INFRA_CHAT_ID="YOUR_TELEGRAM_CHAT_ID"

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
