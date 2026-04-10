#!/bin/bash
# Claude Code persistent daemon launcher
# Managed by launchd (com.claude-code.daemon)
# Starts Claude Code inside tmux so it gets a PTY and stays interactive

# --- CUSTOMIZE THESE ---
AGENT_NAME="${AGENT_NAME:-myagent}"
TMUX_SESSION="${AGENT_NAME}-agent"
WORKSPACE="$HOME/${AGENT_NAME}"
CHANNELS="plugin:telegram@claude-plugins-official"

export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="${HOME}"
export CLAUDE_CODE_OAUTH_TOKEN="${CLAUDE_CODE_OAUTH_TOKEN}"
export USER="$(whoami)"
export LOGNAME="$(whoami)"
export SHELL="/bin/zsh"
export TERM="xterm-256color"
# Ensure telemetry is NOT disabled (blocks channel notifications if set)
unset DISABLE_TELEMETRY

TMUX_BIN="/opt/homebrew/bin/tmux"
CLAUDE_BIN="$HOME/.local/bin/claude"
COMPONENT_LOG="$HOME/.claude/daemon.log"
COMP="launcher:${AGENT_NAME}"

# Source infra logging library (if available)
INFRA_LIB="${WORKSPACE}/skills/admin-mcp/infra_lib.sh"
if [ -f "$INFRA_LIB" ]; then
    source "$INFRA_LIB"
else
    infra_info()  { echo "[INFO]  $1: $2" >> "$COMPONENT_LOG"; }
    infra_error() { echo "[ERROR] $1: $2" >> "$COMPONENT_LOG"; }
    infra_critical() { echo "[CRIT]  $1: $2" >> "$COMPONENT_LOG"; }
fi

# Kill stale session if any
$TMUX_BIN kill-session -t "$TMUX_SESSION" 2>/dev/null
sleep 2

infra_info "$COMP" "Starting Claude Code in tmux session '$TMUX_SESSION'"

# Start Claude Code in a detached tmux session
# --channels flag ensures the plugin is registered for inbound notifications
$TMUX_BIN new-session -d -s "$TMUX_SESSION" "$CLAUDE_BIN --channels $CHANNELS"

if ! $TMUX_BIN has-session -t "$TMUX_SESSION" 2>/dev/null; then
    infra_critical "$COMP" "Failed to create tmux session — agent DOWN"
    exit 1
fi

infra_info "$COMP" "Session started successfully"

# After Claude boots, send startup instructions
sleep 15
if $TMUX_BIN has-session -t "$TMUX_SESSION" 2>/dev/null; then
    $TMUX_BIN send-keys -t "$TMUX_SESSION" "Read ~/.claude/startup-instructions.md and follow the instructions." Enter
    infra_info "$COMP" "Sent startup instructions"
fi

# Monitor the session — exit when it dies so launchd can restart us
while $TMUX_BIN has-session -t "$TMUX_SESSION" 2>/dev/null; do
    sleep 30
done

infra_error "$COMP" "Session died — launchd will restart"
exit 0
