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
EDGE_URL="https://YOUR_SUPABASE_PROJECT_ID.supabase.co/functions/v1/oauth-exchange"
CREDS_FILE="$HOME/.claude-${AGENT_NAME}/.credentials.json"
PLIST="$HOME/Library/LaunchAgents/${DAEMON_LABEL}.plist"
FARLEN_CHAT="YOUR_TELEGRAM_CHAT_ID"

# Alert Farlen on Telegram (uses Derek's bot token)
alert_farlen() {
    local msg="$1"
    local BOT_TOKEN
    BOT_TOKEN=$(python3 -c "import json; print(json.load(open('$HOME/.claude/channels/telegram_derek/config.json'))['botToken'])" 2>/dev/null || true)
    if [ -n "$BOT_TOKEN" ]; then
        curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
            -d chat_id="$FARLEN_CHAT" -d text="$msg" >/dev/null 2>&1 || true
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
    alert_farlen "⚠️ $AGENT_NAME: No refresh token in credentials file. Agent will fall back to API key (Sonnet) on next restart."
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
    alert_farlen "⚠️ $AGENT_NAME: Pro token refresh failed (status $STATUS). Agent will fall back to API key (Sonnet) on next restart."
    exit 1
fi

# Extract new tokens, update credentials file, and sync to Supabase agent_tokens
python3 << PYEOF
import json, time, urllib.request, urllib.error, plistlib, os
from datetime import datetime, timezone, timedelta

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

# ── Sync to public.agent_tokens so the usage-check Edge Function has fresh data.
# Load Supabase creds from vera's plist (same pattern as other admin-mcp scripts).
try:
    svc_url = os.environ.get("SUPABASE_URL")
    svc_key = os.environ.get("SUPABASE_SERVICE_KEY")
    if not (svc_url and svc_key):
        plist_path = "/Users/YOUR_MAC_USERNAME/Library/LaunchAgents/com.vera-agent.daemon.plist"
        with open(plist_path, "rb") as f:
            env = plistlib.load(f).get("EnvironmentVariables", {})
        svc_url = svc_url or env["SUPABASE_URL"]
        svc_key = svc_key or env["SUPABASE_SERVICE_KEY"]

    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=resp.get("expires_in", 28800))).isoformat()
    row = {
        "agent_name":        "$AGENT_NAME",
        "access_token":      resp["access_token"],
        "refresh_token":     resp.get("refresh_token", creds["claudeAiOauth"]["refreshToken"]),
        "expires_at":        expires_at,
        "account_email":     resp.get("account_email") or resp.get("email"),
        "subscription_type": resp.get("subscription_type", "pro"),
        "updated_at":        datetime.now(timezone.utc).isoformat(),
    }
    req = urllib.request.Request(
        f"{svc_url}/rest/v1/agent_tokens?on_conflict=agent_name",
        data=json.dumps(row).encode(),
        method="POST",
        headers={
            "apikey": svc_key,
            "Authorization": f"Bearer {svc_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal,resolution=merge-duplicates",
        },
    )
    urllib.request.urlopen(req, timeout=10)
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] agent_tokens upsert ok — expires {expires_at}")
except Exception as e:
    # Don't fail the refresh — agent_tokens is for usage-check, not critical path
    print(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] WARN: agent_tokens sync failed: {e}", flush=True)
PYEOF

# Restart daemon
echo "[$(date)] Restarting daemon: $DAEMON_LABEL"
launchctl unload "$PLIST" 2>/dev/null || true
sleep 2
launchctl load "$PLIST"
echo "[$(date)] Done. Agent $AGENT_NAME restarted with fresh token."
