#!/bin/bash
# Refresh Pro OAuth tokens for all sub-agents that have credentials files.
# Agents without credentials (still on Max) are silently skipped.
# Run via cron every 6 hours.

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPONENT_LOG="$HOME/derek/logs/token-refresh.log"
COMP="token-refresh"

# Source infra logging library
source "$SCRIPT_DIR/infra_lib.sh"

REFRESHED=0
SKIPPED=0
FAILED=0

refresh_agent() {
    local AGENT="$1" DAEMON="$2"
    local CREDS="$HOME/.claude-${AGENT}/.credentials.json"

    if [ ! -f "$CREDS" ]; then
        infra_info "$COMP" "$AGENT: No credentials file — on Max, skipping"
        SKIPPED=$((SKIPPED + 1))
        return
    fi

    infra_info "$COMP" "$AGENT: Refreshing Pro token..."
    local OUTPUT
    OUTPUT=$("$SCRIPT_DIR/refresh_agent_token.sh" "$AGENT" "$DAEMON" 2>&1)
    local EXIT_CODE=$?

    if [ $EXIT_CODE -eq 0 ]; then
        infra_info "$COMP" "$AGENT: Refreshed successfully"
        REFRESHED=$((REFRESHED + 1))
    else
        infra_error "$COMP" "$AGENT: Token refresh FAILED (exit $EXIT_CODE). Output: ${OUTPUT:0:200}"
        FAILED=$((FAILED + 1))
    fi
}

infra_info "$COMP" "Starting token refresh cycle"

refresh_agent vera  com.vera-agent.daemon
refresh_agent test  com.test-agent.daemon
refresh_agent nate  com.nate-agent.daemon
refresh_agent blake com.blake-agent.daemon
refresh_agent julie com.julie-agent.daemon

infra_info "$COMP" "Summary: $REFRESHED refreshed, $SKIPPED skipped (Max), $FAILED failed"

if [ "$FAILED" -gt 0 ]; then
    infra_warn "$COMP" "$FAILED agent(s) failed token refresh — will fall back to Max on next restart"
fi
