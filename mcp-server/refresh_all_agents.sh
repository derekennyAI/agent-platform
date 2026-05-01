#!/bin/bash
# Refresh Pro OAuth tokens for all sub-agents that have credentials files.
# Agents without credentials (still on Max) are silently skipped.
#
# Scheduling: canonical via launchd at com.token-refresh (every 6h at :07).
# The old crontab entry at :17 is deprecated; the guard below makes it a no-op
# until the crontab line can be removed (macOS TCC blocks `crontab` edits from
# most shells — grant Terminal Full Disk Access in System Settings, then remove
# the stale crontab line and this guard together).

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
COMPONENT_LOG="$HOME/derek/logs/token-refresh.log"
COMP="token-refresh"

# Guard — neutralize the stale `:17` cron invocation while launchd takes over at `:07`.
if [ "$(date +%M)" = "17" ]; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] skipping :17 cron fire (superseded by launchd :07 schedule)"
    exit 0
fi

# Source infra logging library + registry
source "$SCRIPT_DIR/infra_lib.sh"
source "/Users/YOUR_MAC_USERNAME/derek/agent-infra/shared/registry.sh"

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

# Iterate Pro-token agents from the registry (has_pro_token=true)
while IFS= read -r agent; do
    [ -z "$agent" ] && continue
    daemon=$(registry_get "$agent" daemon_label)
    refresh_agent "$agent" "$daemon"
done < <(registry_pro_agents)

infra_info "$COMP" "Summary: $REFRESHED refreshed, $SKIPPED skipped (Max), $FAILED failed"

if [ "$FAILED" -gt 0 ]; then
    infra_warn "$COMP" "$FAILED agent(s) failed token refresh — will fall back to Max on next restart"
fi

# ==========================================
# GOOGLE TOKEN REFRESH — all agents' Gmail/Calendar tokens
# ==========================================
# Google access_tokens expire after 1 hour. The refresh_token is long-lived
# but can be revoked (testing mode: 7 days, password change, token limit).
# This preemptively refreshes all Google tokens so agents don't hit 401s.

GOOGLE_COUNT_FILE=$(mktemp /tmp/google_refresh_counts.XXXXXX)
echo "0 0" > "$GOOGLE_COUNT_FILE"

refresh_google_tokens() {
    local AGENT="$1"
    local AGENT_DIR="$HOME/$AGENT"
    local CONFIG_DIR="$AGENT_DIR/.config/$AGENT"

    # Find all Google token files for this agent
    local TOKEN_FILES=$(find "$CONFIG_DIR" -name "google-token.json" 2>/dev/null)
    [ -z "$TOKEN_FILES" ] && return

    # Find credentials file (client_id/secret) — check agent-specific first, then Derek's
    local CRED_FILE=""
    for candidate in \
        "$CONFIG_DIR/google-credentials.json" \
        "$HOME/derek/.config/derek/accounts/${AGENT}_at_*/google-credentials.json" \
        "$HOME/derek/.config/derek/accounts/farlen_at_enny_ai/google-credentials.json"; do
        local expanded=$(ls $candidate 2>/dev/null | head -1)
        if [ -n "$expanded" ] && [ -f "$expanded" ]; then
            CRED_FILE="$expanded"
            break
        fi
    done

    if [ -z "$CRED_FILE" ]; then
        return  # No credentials to refresh with
    fi

    while IFS= read -r TOKEN_FILE; do
        [ -z "$TOKEN_FILE" ] && continue

        python3 << PYEOF
import json, urllib.request, urllib.parse, sys

cred_file = "$CRED_FILE"
token_file = "$TOKEN_FILE"

try:
    with open(cred_file) as f:
        creds = json.load(f)
    creds = creds.get("installed", creds.get("web", creds))

    with open(token_file) as f:
        token_data = json.load(f)

    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print(f"SKIP: no refresh_token in {token_file}")
        sys.exit(0)

    data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token"
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req) as resp:
        new_tokens = json.loads(resp.read().decode())

    token_data["access_token"] = new_tokens["access_token"]
    if "refresh_token" in new_tokens:
        token_data["refresh_token"] = new_tokens["refresh_token"]

    with open(token_file, "w") as f:
        json.dump(token_data, f, indent=2)

    print(f"OK: {token_file}")
except Exception as e:
    print(f"FAIL: {token_file} — {e}", file=sys.stderr)
    sys.exit(1)
PYEOF
        local counts
        read -r gr gf < "$GOOGLE_COUNT_FILE"
        if [ $? -eq 0 ]; then
            echo "$((gr + 1)) $gf" > "$GOOGLE_COUNT_FILE"
        else
            echo "$gr $((gf + 1))" > "$GOOGLE_COUNT_FILE"
        fi
    done <<< "$TOKEN_FILES"
}

infra_info "$COMP" "Starting Google token refresh"

# All agents from the registry may have Google tokens
while IFS= read -r agent; do
    [ -z "$agent" ] && continue
    refresh_google_tokens "$agent"
done < <(registry_agents)

read -r GOOGLE_REFRESHED GOOGLE_FAILED < "$GOOGLE_COUNT_FILE"
rm -f "$GOOGLE_COUNT_FILE"

infra_info "$COMP" "Google tokens: $GOOGLE_REFRESHED refreshed, $GOOGLE_FAILED failed"

if [ "$GOOGLE_FAILED" -gt 0 ]; then
    infra_warn "$COMP" "$GOOGLE_FAILED Google token(s) failed refresh — agents may need to re-authenticate"
fi
