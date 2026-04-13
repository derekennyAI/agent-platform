#!/bin/bash
# Refresh an agent's Claude OAuth token via Supabase edge function
# Usage: refresh_agent_token.sh <agent_name> <daemon_label>
# Example: refresh_agent_token.sh vera com.vera.daemon
#
# Reads refresh_token from ~/.claude-<agent>/.credentials.json
# Calls edge function to get new access_token
# Updates credentials file
# Restarts the daemon

set -euo pipefail

AGENT_NAME="${1:?Usage: refresh_agent_token.sh <agent_name> <daemon_label>}"
DAEMON_LABEL="${2:?Usage: refresh_agent_token.sh <agent_name> <daemon_label>}"
EDGE_URL="${SUPABASE_URL}/functions/v1/oauth-exchange"
CREDS_FILE="$HOME/.claude-${AGENT_NAME}/.credentials.json"
PLIST="$HOME/Library/LaunchAgents/${DAEMON_LABEL}.plist"
ADMIN_CHAT="${ADMIN_TELEGRAM_CHAT_ID:-}"

# Alert admin on Telegram (uses admin agent's bot token)
alert_admin() {
    local msg="$1"
    [ -z "$ADMIN_CHAT" ] && return 0
    local BOT_TOKEN
    BOT_TOKEN=$(python3 -c "import json; print(json.load(open('$HOME/.claude/channels/telegram/.env')))" 2>/dev/null || true)
    if [ -n "$BOT_TOKEN" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d chat_id="$ADMIN_CHAT" -d text="$msg" >/dev/null 2>&1 || true
    fi
}

if [ ! -f "$CREDS_FILE" ]; then
    echo "[$(date)] No credentials file for $AGENT_NAME — not on Pro yet, skipping" >&2
    exit 0
fi

if [ ! -f "$PLIST" ]; then
    echo "[$(date)] ERROR: Plist not found: $PLIST" >&2
    exit 1
fi

# Extract refresh token
REFRESH_TOKEN=$(python3 -c "import json; d=json.load(open('$CREDS_FILE')); print(d['claudeAiOauth']['refreshToken'])" 2>/dev/null || true)

if [ -z "$REFRESH_TOKEN" ]; then
    echo "[$(date)] ERROR: No refresh token found in $CREDS_FILE" >&2
    alert_admin "⚠️ $AGENT_NAME: No refresh token in credentials file. Agent will fall back to Max on next restart."
    exit 1
fi

echo "[$(date)] Refreshing token for agent: $AGENT_NAME"

# Call edge function
RESPONSE=$(curl -s -X POST "$EDGE_URL" \
    -H "Content-Type: application/json" \
    -d "{\"grant_type\": \"refresh_token\", \"refresh_token\": \"$REFRESH_TOKEN\"}")

# Check response
STATUS=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('status',''))")

if [ "$STATUS" != "200" ]; then
    echo "[$(date)] ERROR: Token refresh failed (status $STATUS)" >&2
    echo "$RESPONSE" >&2
    alert_admin "⚠️ $AGENT_NAME: Pro token refresh failed (status $STATUS). Agent will fall back to Max on next restart."
    exit 1
fi

# Extract new tokens and update credentials file
python3 << PYEOF
import json, time

with open("$CREDS_FILE") as f:
    creds = json.load(f)

response = json.loads('''$RESPONSE''')
resp = response["response"]

creds["claudeAiOauth"]["accessToken"] = resp["access_token"]
if "refresh_token" in resp:
    creds["claudeAiOauth"]["refreshToken"] = resp["refresh_token"]
creds["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000) + (resp.get("expires_in", 28800) * 1000)

with open("$CREDS_FILE", "w") as f:
    json.dump(creds, f, indent=2)

print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Token refreshed. Expires in {resp.get('expires_in', 28800)}s")
PYEOF

# Restart daemon
echo "[$(date)] Restarting daemon: $DAEMON_LABEL"
launchctl unload "$PLIST" 2>/dev/null || true
sleep 2
launchctl load "$PLIST"
echo "[$(date)] Done. Agent $AGENT_NAME restarted with fresh token."
