#!/bin/bash
# Scheduler Executor — runs every minute via system crontab
# Reads scheduled_tasks.json, fires any due tasks into the right agent's tmux session
# Part of admin-control MCP Module 4 (Cerebellum)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS_FILE="$SCRIPT_DIR/scheduled_tasks.json"
COMPONENT_LOG="$SCRIPT_DIR/scheduler.log"
TMUX_BIN="/opt/homebrew/bin/tmux"
COMP="scheduler"

# Load Supabase credentials from launchd plist if not already in environment
# (crontab jobs don't inherit launchd env vars). Source: admin's daemon plist —
# admin is the control plane, semantically the right env source. Same pattern
# as project_memories_to_files.py.
if [ -z "$SUPABASE_SERVICE_KEY" ]; then
    _PLIST="$HOME/Library/LaunchAgents/com.admin-agent.daemon.plist"
    if [ -f "$_PLIST" ]; then
        _ENV_EXPORTS=$(_PLIST="$_PLIST" python3 << 'PYEOF'
import os, plistlib, shlex
with open(os.environ["_PLIST"], "rb") as f:
    env = plistlib.load(f).get("EnvironmentVariables", {})
for k in ("SUPABASE_URL", "SUPABASE_SERVICE_KEY"):
    if k in env:
        print(f"export {k}={shlex.quote(env[k])}")
PYEOF
)
        eval "$_ENV_EXPORTS"
    fi
fi

# Supabase config
SUPABASE_URL="${SUPABASE_URL:-https://YOUR_SUPABASE_PROJECT_ID.supabase.co}"
SUPABASE_KEY="${SUPABASE_SERVICE_KEY:-}"

# Source infra logging library
source "$SCRIPT_DIR/infra_lib.sh"

# Agent lookups — registry-driven so renames/moves don't require code edits
REGISTRY_FILE="${REGISTRY_FILE:-/Users/YOUR_MAC_USERNAME/derek/agent-infra/agents.json}"

get_session() {
    /usr/bin/jq -r --arg a "$1" '.agents[$a].tmux_session // empty' "$REGISTRY_FILE" 2>/dev/null
}

get_workspace() {
    /usr/bin/jq -r --arg a "$1" '.agents[$a].workspace // empty' "$REGISTRY_FILE" 2>/dev/null
}

# Channels-mode agents deliver scheduled tasks via the list_due_tasks MCP tool
# (pull), not via tmux send-keys (push). This is because --channels mode turns
# Claude Code's stdin over to the plugin; send-keys just fills the input buffer
# without submitting a turn. Pull-based delivery works around that.
# Non-channels agents (blake, julie) continue to use send-keys.
agent_uses_channels() {
    case "$1" in
        admin|derek|dereklm|macgyver|nate|vera|blake|julie) return 0 ;;
        *) return 1 ;;
    esac
}

# Phase 6 — one-shot claude -p for proactive scheduled task delivery.
# Uses THIS AGENT'S OAuth token (per-agent quota tracking preserved).
# Runs headless (no tmux, no --channels) in background so the scheduler continues.
CLAUDE_BIN="/Users/YOUR_MAC_USERNAME/.local/bin/claude"
spawn_push_oneshot() {
    local agent="$1" tid="$2" desc="$3"
    local workspace="$(get_workspace "$agent")"
    local creds_file="$HOME/.claude-${agent}/.credentials.json"

    # Phase 1.2 defensive OAuth gate — push tasks require OAuth. Fail legibly
    # for any agent without has_pro_token (e.g., a future API-key-only agent).
    local has_pro
    has_pro=$(/usr/bin/jq -r --arg a "$agent" '.agents[$a].has_pro_token // false' "$HOME/derek/agent-infra/agents.json" 2>/dev/null || echo "false")
    if [ "$has_pro" != "true" ]; then
        infra_error "$COMP" "PUSH_SPAWN_BLOCKED $tid ($agent): push requires OAuth but has_pro_token=$has_pro"
        return 1
    fi

    if [ ! -f "$creds_file" ]; then
        infra_error "$COMP" "PUSH $tid ($agent): no credentials file — cannot spawn one-shot"
        return 1
    fi

    local oauth_token
    oauth_token=$(/opt/homebrew/bin/python3 -c "import json; print(json.load(open('$creds_file'))['claudeAiOauth']['accessToken'])" 2>/dev/null)
    if [ -z "$oauth_token" ]; then
        infra_error "$COMP" "PUSH $tid ($agent): couldn't read OAuth token"
        return 1
    fi

    local push_log="${workspace}/push_tasks.log"
    local mcp_config="${workspace}/.mcp.json"
    local now_ts
    now_ts=$(date '+%Y-%m-%d %H:%M:%S')
    echo "[$now_ts] START $tid: ${desc:0:120}" >> "$push_log"

    # Read TELEGRAM_STATE_DIR from the agent's plist so push one-shots
    # use the correct bot (fixes recurring bot-collision where derek's
    # pushes grabbed admin's @NewDerekBot instead of @farlenTestBot).
    local tg_state_dir=""
    local agent_plist="$HOME/Library/LaunchAgents/com.${agent}-agent.daemon.plist"
    if [ -f "$agent_plist" ]; then
        tg_state_dir=$(/opt/homebrew/bin/python3 -c "
import plistlib
with open('$agent_plist', 'rb') as f:
    print(plistlib.load(f).get('EnvironmentVariables', {}).get('TELEGRAM_STATE_DIR', ''))
" 2>/dev/null || true)
    fi

    # Isolated config dir for push one-shots. Sharing CLAUDE_CONFIG_DIR with the
    # live --channels session makes the concurrent `claude -p` contend over the
    # live session's state and drops the telegram plugin's stdin, killing it —
    # the watchdog then restarts the whole agent. A separate ...-cron dir removes
    # that contention. Self-heal if missing.
    local cron_cfg="$HOME/.claude-${agent}-cron"
    if [ ! -f "$cron_cfg/.claude.json" ]; then
        mkdir -p "$cron_cfg"
        [ -f "$HOME/.claude-${agent}/.claude.json" ] && cp "$HOME/.claude-${agent}/.claude.json" "$cron_cfg/.claude.json"
        [ -f "$HOME/.claude-${agent}/settings.json" ] && cp "$HOME/.claude-${agent}/settings.json" "$cron_cfg/settings.json"
        # Seed the live dir's already-patched telegram plugin. Without this the
        # one-shot finds no plugin, auto-installs a FRESH (unpatched) copy from
        # the marketplace, and that copy ignores TELEGRAM_SEND_ONLY and polls —
        # stealing the live --channels session's getUpdates slot (the bug this
        # whole path exists to avoid). See patch_telegram_plugin.py (send-only-v1).
        [ -d "$HOME/.claude-${agent}/plugins" ] && cp -R "$HOME/.claude-${agent}/plugins" "$cron_cfg/" 2>/dev/null || true
    fi
    # Belt-and-suspenders: re-apply the plugin patch to the cron dir in case the
    # marketplace re-pulled an unpatched copy. The patcher globs every ~/.claude-*
    # dir (incl. -cron) and is idempotent.
    /opt/homebrew/bin/python3 "$HOME/agent-platform/scripts/patch_telegram_plugin.py" >/dev/null 2>&1 || true

    # Run in background — scheduler shouldn't block on claude's execution time
    (
        cd "$workspace"
        env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN \
            PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin" \
            CLAUDE_CODE_OAUTH_TOKEN="$oauth_token" \
            CLAUDE_CONFIG_DIR="$cron_cfg" \
            AGENT_NAME="$agent" \
            SUPABASE_URL="$SUPABASE_URL" \
            SUPABASE_SERVICE_KEY="$SUPABASE_KEY" \
            ${tg_state_dir:+TELEGRAM_STATE_DIR="$tg_state_dir"} \
            TELEGRAM_SEND_ONLY=1 \
            "$CLAUDE_BIN" -p "SCHEDULED TASK [$tid]: $desc" \
            --dangerously-skip-permissions \
            --mcp-config "$mcp_config" \
            >> "$push_log" 2>&1
        local rc=$?
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] END $tid (exit=$rc)" >> "$push_log"
        if [ "$rc" -eq 0 ]; then
            # Phase 1.2 — completion-gated last_fired_at update. Only fires when
            # the spawned claude -p actually exited 0, so crashes/hangs leave
            # the timestamp stale and the staleness alerter eventually pages.
            # Loud-fail: if env loading at line 14-28 didn't populate the key,
            # the Python block silently skips Supabase. Surface that instead of
            # letting the column rot.
            if [ -z "$SUPABASE_KEY" ]; then
                infra_error "$COMP" "PUSH $tid ($agent): SUPABASE_SERVICE_KEY empty in scheduler env — last_fired_at writeback to Supabase will be SKIPPED (column will rot, staleness alerts will fire). Check $HOME/Library/LaunchAgents/com.admin-agent.daemon.plist exists and has the key."
            fi
            TID="$tid" TASKS_FILE="$TASKS_FILE" SUPABASE_SERVICE_KEY="$SUPABASE_KEY" SUPABASE_URL="$SUPABASE_URL" \
            /opt/homebrew/bin/python3 - <<'PYEOF' >> "$push_log" 2>&1
import json, os, urllib.request
from urllib.parse import quote
from datetime import datetime, timezone
now = datetime.now(timezone.utc).isoformat()
tid = os.environ.get("TID", "")
tasks_file = os.environ.get("TASKS_FILE", "")
svc = os.environ.get("SUPABASE_SERVICE_KEY", "")
base = os.environ.get("SUPABASE_URL", "https://YOUR_SUPABASE_PROJECT_ID.supabase.co")
if svc and tid:
    try:
        req = urllib.request.Request(
            f"{base}/rest/v1/scheduled_tasks?id=eq.{quote(tid, safe='')}",
            data=json.dumps({"last_fired_at": now}).encode(),
            method="PATCH",
            headers={"apikey": svc, "Authorization": f"Bearer {svc}",
                     "Content-Type": "application/json", "Prefer": "return=minimal"})
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"  [Phase 1.2] last_fired_at Supabase patch failed: {e}")
if tasks_file:
    try:
        with open(tasks_file) as f: tasks = json.load(f)
        for t in tasks:
            if t.get("id") == tid:
                t["last_fired_at"] = now
                break
        with open(tasks_file, "w") as f: json.dump(tasks, f, indent=2)
    except Exception as e:
        print(f"  [Phase 1.2] local cache update failed: {e}")
PYEOF
        fi
    ) &

    return 0
}



# Check if agent is idle (at prompt, not mid-conversation)
# Returns 0 if idle, 1 if busy
# Flood cap: if ≥ MAX_STACKED_TASKS (default 3) "SCHEDULED TASK" occurrences in the last 40
# lines, treat as busy — prevents pileup (observed on dereklm: 11 stacked tasks).
agent_is_idle() {
    local session="$1"
    local max_stacked="${MAX_STACKED_TASKS:-3}"
    local pane_tail
    pane_tail=$($TMUX_BIN capture-pane -t "$session" -p -S -40 2>/dev/null)
    # Claude Code shows "❯" when ready for input; if missing, agent is busy
    if ! echo "$pane_tail" | grep -q "❯"; then
        return 1
    fi
    # Flood cap: count stacked SCHEDULED TASK injections in recent pane
    local stacked_count
    stacked_count=$(echo "$pane_tail" | grep -c "SCHEDULED TASK" 2>/dev/null || echo 0)
    if [ "$stacked_count" -ge "$max_stacked" ]; then
        return 1
    fi
    # Also check the last content line — if it ends with a queued SCHEDULED TASK,
    # send-keys would concatenate, not fire. Treat as busy.
    local last_content
    last_content=$(echo "$pane_tail" | grep -v '^\s*$' | grep -v '─' | grep -v '⏵' | tail -1)
    if echo "$last_content" | grep -q "SCHEDULED TASK"; then
        return 1
    fi
    return 0
}

# Per-agent cooldown file directory
COOLDOWN_DIR="/tmp/scheduler_cooldowns"
mkdir -p "$COOLDOWN_DIR"

# Check if agent was fired recently (within cooldown_secs). Returns 0 if clear, 1 if cooling down.
agent_cooldown_clear() {
    local agent="$1"
    local cooldown_secs="${2:-90}"
    local cooldown_file="$COOLDOWN_DIR/${agent}_last_fired"
    if [ ! -f "$cooldown_file" ]; then
        return 0
    fi
    local last_fired now elapsed
    last_fired=$(cat "$cooldown_file" 2>/dev/null)
    now=$(date +%s)
    elapsed=$((now - last_fired))
    if [ "$elapsed" -lt "$cooldown_secs" ]; then
        return 1
    fi
    return 0
}

# Record that we just fired a task to this agent
agent_cooldown_set() {
    local agent="$1"
    date +%s > "$COOLDOWN_DIR/${agent}_last_fired"
}

# Try to fetch tasks from Supabase; fall back to local JSON cache
TASKS_SOURCE="supabase"
if [ -n "$SUPABASE_KEY" ]; then
    SUPABASE_TASKS=$(curl -sf "${SUPABASE_URL}/rest/v1/scheduled_tasks?active=eq.true&select=id,agent_name,schedule,task_description,recurring,active,last_fired_at,trigger" \
        -H "apikey: $SUPABASE_KEY" \
        -H "Authorization: Bearer $SUPABASE_KEY" 2>/dev/null)
    if [ $? -ne 0 ] || [ -z "$SUPABASE_TASKS" ]; then
        TASKS_SOURCE="local"
        infra_warn "$COMP" "Supabase fetch failed, falling back to local cache"
    fi
else
    TASKS_SOURCE="local"
fi

if [ "$TASKS_SOURCE" = "local" ] && [ ! -f "$TASKS_FILE" ]; then
    exit 0
fi

# ==========================================
# TWO-WAY SYNC — reconcile Supabase ↔ local
# ==========================================
# Runs every 10 minutes. Three cases:
#   1. Local only → push to Supabase
#   2. Supabase only → pull to local (handled by cache write below)
#   3. Both exist but differ → alert admin, don't auto-resolve
if [ "$TASKS_SOURCE" = "supabase" ] && [ -f "$TASKS_FILE" ] && [ $((NOW_MIN % 10)) -eq 0 ]; then
    python3 << 'SYNC_EOF'
import json, os, sys, urllib.request, urllib.error

tasks_file = os.environ.get("TASKS_FILE", "scheduled_tasks.json")
svc_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
base_url = os.environ.get("SUPABASE_URL", "")
chat_id = os.environ.get("ADMIN_TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

if not svc_key or not base_url:
    sys.exit(0)

# Load local tasks
try:
    with open(tasks_file) as f:
        local_tasks = {t["id"]: t for t in json.load(f)}
except:
    sys.exit(0)

# Load Supabase tasks
try:
    url = f"{base_url}/rest/v1/scheduled_tasks?select=id,agent_name,schedule,task_description,recurring,active,last_fired_at,trigger"
    req = urllib.request.Request(url, headers={
        "apikey": svc_key, "Authorization": f"Bearer {svc_key}"
    })
    resp = urllib.request.urlopen(req)
    db_tasks = {t["id"]: t for t in json.loads(resp.read())}
except:
    sys.exit(0)

alerts = []

# Case 1: Local only → push to Supabase
for tid, task in local_tasks.items():
    if tid not in db_tasks:
        try:
            body = json.dumps({
                "id": tid,
                "agent_name": task.get("agent_name", "derek"),
                "created_by": task.get("agent_name", "derek"),
                "schedule": task["schedule"],
                "task_description": task["task_description"],
                "recurring": task.get("recurring", True),
                "active": task.get("active", True),
            }).encode()
            req = urllib.request.Request(
                f"{base_url}/rest/v1/scheduled_tasks",
                data=body, method="POST",
                headers={
                    "apikey": svc_key, "Authorization": f"Bearer {svc_key}",
                    "Content-Type": "application/json", "Prefer": "return=minimal"
                }
            )
            urllib.request.urlopen(req)
            print(f"SYNC: pushed local-only task {tid} to Supabase")
        except Exception as e:
            print(f"SYNC: failed to push {tid}: {e}", file=sys.stderr)

# Case 3: Both exist but differ → alert
COMPARE_FIELDS = ["schedule", "task_description", "active"]
for tid in local_tasks:
    if tid in db_tasks:
        for field in COMPARE_FIELDS:
            local_val = local_tasks[tid].get(field)
            db_val = db_tasks[tid].get(field)
            if local_val != db_val:
                alerts.append(f"Task {tid}: '{field}' differs — local={local_val}, db={db_val}")

if alerts:
    # Send alert via Telegram
    try:
        bot_envfile = os.path.expanduser("~/.claude/channels/telegram/.env")
        bot_token = ""
        if os.path.exists(bot_envfile):
            for line in open(bot_envfile):
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    bot_token = line.strip().split("=", 1)[1]
        if bot_token and chat_id:
            msg = "⚠️ SYNC CONFLICT — local vs Supabase:\n" + "\n".join(alerts[:5])
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": msg}).encode()
            import urllib.parse
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                data=data, method="POST"
            )
            urllib.request.urlopen(req)
    except:
        pass
    print(f"SYNC: {len(alerts)} conflict(s) flagged to admin")
SYNC_EOF
    export TASKS_FILE SUPABASE_URL SUPABASE_SERVICE_KEY
fi

# Get current time components (local timezone)
NOW_MIN=$(date +%-M)
NOW_HOUR=$(date +%-H)
NOW_DOM=$(date +%-d)
NOW_MON=$(date +%-m)
NOW_DOW=$(date +%u)  # 1=Monday, 7=Sunday
# Convert to cron dow (0=Sunday, 6=Saturday)
if [ "$NOW_DOW" -eq 7 ]; then
    NOW_DOW=0
fi

# Parse cron field and check if current value matches
matches_field() {
    local field="$1"
    local current="$2"
    local max="$3"

    # Wildcard
    if [ "$field" = "*" ]; then
        return 0
    fi

    # Step: */N
    if [[ "$field" =~ ^\*/([0-9]+)$ ]]; then
        local step="${BASH_REMATCH[1]}"
        if [ $((current % step)) -eq 0 ]; then
            return 0
        fi
        return 1
    fi

    # Range: N-M
    if [[ "$field" =~ ^([0-9]+)-([0-9]+)$ ]]; then
        local start="${BASH_REMATCH[1]}"
        local end="${BASH_REMATCH[2]}"
        if [ "$current" -ge "$start" ] && [ "$current" -le "$end" ]; then
            return 0
        fi
        return 1
    fi

    # List: N,M,O
    IFS=',' read -ra values <<< "$field"
    for val in "${values[@]}"; do
        # Each value could be a range
        if [[ "$val" =~ ^([0-9]+)-([0-9]+)$ ]]; then
            local start="${BASH_REMATCH[1]}"
            local end="${BASH_REMATCH[2]}"
            if [ "$current" -ge "$start" ] && [ "$current" -le "$end" ]; then
                return 0
            fi
        elif [ "$val" -eq "$current" ] 2>/dev/null; then
            return 0
        fi
    done

    return 1
}

# Check if a cron expression matches the current time
cron_matches() {
    local cron="$1"
    read -r c_min c_hour c_dom c_mon c_dow <<< "$cron"

    matches_field "$c_min" "$NOW_MIN" 59 || return 1
    matches_field "$c_hour" "$NOW_HOUR" 23 || return 1
    matches_field "$c_dom" "$NOW_DOM" 31 || return 1
    matches_field "$c_mon" "$NOW_MON" 12 || return 1
    matches_field "$c_dow" "$NOW_DOW" 6 || return 1

    return 0
}

# Process tasks
FIRED=0
ERRORS=0

if [ "$TASKS_SOURCE" = "supabase" ]; then
    # Write Supabase response to temp file to avoid quoting issues
    TMPFILE=$(mktemp /tmp/sched_tasks.XXXXXX)
    echo "$SUPABASE_TASKS" > "$TMPFILE"
    TASK_OUTPUT=$(python3 -c "
import json, sys

with open('$TMPFILE') as f:
    tasks = json.load(f)

# trigger overlay from local JSON — supabase may not have a 'trigger' column,
# so we overlay it from the local JSON (which the MCP tools maintain) so PUSH
# vs PULL tagging actually takes effect.
local_trigger = {}
try:
    with open('$TASKS_FILE') as _lf:
        for _t in json.load(_lf):
            if _t.get('trigger'):
                local_trigger[_t['id']] = _t['trigger']
except Exception:
    pass

for i, task in enumerate(tasks):
    if not task.get('active', True):
        continue
    trig = task.get('trigger') or local_trigger.get(task['id'], 'pull')
    print(f\"{i}|{task['id']}|{task['agent_name']}|{task['schedule']}|{task.get('recurring', True)}|{trig}|{task['task_description']}\")
" 2>&1)
    rm -f "$TMPFILE"
    # Also update local cache from Supabase for offline fallback
    echo "$SUPABASE_TASKS" | python3 -c "
import json, sys
tasks = json.load(sys.stdin)
with open('$TASKS_FILE', 'w') as f:
    json.dump(tasks, f, indent=2)
" 2>/dev/null
else
    TASK_OUTPUT=$(python3 -c "
import json, sys

with open('$TASKS_FILE') as f:
    tasks = json.load(f)

for i, task in enumerate(tasks):
    if not task.get('active', True):
        continue
    print(f\"{i}|{task['id']}|{task['agent_name']}|{task['schedule']}|{task.get('recurring', True)}|{task.get('trigger', 'pull')}|{task['task_description']}\")
" 2>&1)
fi

PARSE_EXIT=$?
if [ $PARSE_EXIT -ne 0 ]; then
    infra_error "$COMP" "Failed to parse tasks ($TASKS_SOURCE): $TASK_OUTPUT"
    exit 1
fi

echo "$TASK_OUTPUT" | while IFS='|' read -r idx tid agent schedule recurring trigger desc; do
    [ -z "$tid" ] && continue

    if cron_matches "$schedule"; then
        # Pull-based delivery: for channels-mode agents, don't send-keys. The
        # agent will call list_due_tasks on its next turn and pick up this task
        # via the MCP tool. Tasks flagged trigger=push bypass this and use the
        # send-keys path (for tasks that must fire regardless of user activity).
        if agent_uses_channels "$agent"; then
            if [ "$trigger" = "push" ]; then
                if spawn_push_oneshot "$agent" "$tid" "$desc"; then
                    # last_fired_at now updates inside the subshell on successful completion (Phase 1.2).
                    # Scheduler may re-fire within the same minute if the spawn crashes — that's by design.
                    infra_info "$COMP" "PUSH $tid ($agent): one-shot claude -p spawned"
                fi
            else
                infra_info "$COMP" "PULL $tid ($agent): delivered via list_due_tasks (not send-keys)"
            fi
            continue
        fi

        session="$(get_session "$agent")"
        if [ -z "$session" ]; then
            infra_error "$COMP" "Unknown agent '$agent' for task $tid — task cannot fire"
            continue
        fi

        if ! $TMUX_BIN has-session -t "$session" 2>/dev/null; then
            infra_error "$COMP" "Session '$session' not running — task $tid ($agent) skipped: ${desc:0:80}"
            continue
        fi

        # Gate: skip if agent hasn't finished startup (.ready file missing)
        # EXCEPTION: admin_poll / admin_tasks — these are the bootstrap mechanism;
        # they must fire even without .ready so the agent reads startup-instructions
        # and touches .ready. Otherwise we deadlock: no .ready → no fire → no .ready.
        agent_ws="$(get_workspace "$agent")"
        if [ ! -f "$agent_ws/.ready" ]; then
            case "$tid" in
                *admin_poll*|*admin_tasks*)
                    infra_info "$COMP" "BOOTSTRAP-FIRE $tid ($agent): .ready missing but this is a boot task"
                    ;;
                *)
                    infra_info "$COMP" "SKIPPED $tid ($agent): agent still starting up (.ready not found)"
                    continue
                    ;;
            esac
        fi

        # Gate: skip if agent is busy (mid-conversation, not at prompt)
        if ! agent_is_idle "$session"; then
            infra_info "$COMP" "SKIPPED $tid ($agent): agent busy (not at prompt)"
            continue
        fi

        # Gate: skip if we recently fired a task to this agent (90s cooldown prevents pileup)
        if ! agent_cooldown_clear "$agent" 90; then
            infra_info "$COMP" "SKIPPED $tid ($agent): cooldown active (fired recently)"
            continue
        fi

        # Fire the task
        if $TMUX_BIN send-keys -t "$session" "SCHEDULED TASK [$tid]: $desc" Enter 2>/dev/null; then
            infra_info "$COMP" "FIRED $tid ($agent): ${desc:0:80}"
            agent_cooldown_set "$agent"
            FIRED=$((FIRED + 1))
        else
            infra_error "$COMP" "send-keys failed for $tid ($agent) session '$session'"
            continue
        fi

        # Update last_fired_at — Supabase is source of truth, local JSON is cache
        TID="$tid" RECURRING="$recurring" TASKS_FILE="$TASKS_FILE" SUPABASE_SERVICE_KEY="$SUPABASE_KEY" SUPABASE_URL="$SUPABASE_URL" \
        python3 - <<'PYEOF' 2>/dev/null
import json, urllib.request, urllib.error, os
from urllib.parse import quote
from datetime import datetime, timezone

now = datetime.now(timezone.utc).isoformat()
tid = os.environ.get('TID', '')
recurring = os.environ.get('RECURRING', '')
tasks_file = os.environ.get('TASKS_FILE', '')
deactivate = recurring.lower() == 'false'

svc_key = os.environ.get('SUPABASE_SERVICE_KEY', '')
base_url = os.environ.get('SUPABASE_URL', 'https://YOUR_SUPABASE_PROJECT_ID.supabase.co')

# Primary: Update Supabase
if svc_key and tid:
    try:
        url = f'{base_url}/rest/v1/scheduled_tasks?id=eq.{quote(tid, safe="")}'
        body = json.dumps({'last_fired_at': now} | ({'active': False} if deactivate else {})).encode()
        req = urllib.request.Request(url, data=body, method='PATCH', headers={
            'apikey': svc_key, 'Authorization': f'Bearer {svc_key}',
            'Content-Type': 'application/json', 'Prefer': 'return=minimal'
        })
        urllib.request.urlopen(req)
    except Exception:
        pass

# Cache: Update local JSON
if tasks_file:
    try:
        with open(tasks_file) as f:
            tasks = json.load(f)
        for task in tasks:
            if task['id'] == tid:
                task['last_fired_at'] = now
                if deactivate:
                    task['active'] = False
                break
        with open(tasks_file, 'w') as f:
            json.dump(tasks, f, indent=2)
    except Exception:
        pass
PYEOF
    fi
done

# ==========================================
# HEARTBEAT EXECUTOR — runs every 10 minutes
# ==========================================
# Checks for agents with active heartbeats and fires a single
# "check your heartbeats" message per agent per cycle.
# The agent then calls list_heartbeats and processes each watch.

if [ $((NOW_MIN % 10)) -eq 0 ] && [ -n "$SUPABASE_KEY" ]; then
    # HEARTBEATS now pull-based — each agent calls list_due_heartbeats via MCP
    # on user turns. The old tmux send-keys dispatch doesn't work in --channels
    # mode (Telegram plugin owns stdin). Here we just log the count for audit.
    HB_AGENTS=$(curl -sf "${SUPABASE_URL}/rest/v1/heartbeats?active=eq.true&select=id" \
        -H "apikey: $SUPABASE_KEY" \
        -H "Authorization: Bearer $SUPABASE_KEY" 2>/dev/null)
    if [ -n "$HB_AGENTS" ] && [ "$HB_AGENTS" != "[]" ]; then
        HB_COUNT=$(echo "$HB_AGENTS" | python3 -c "import json,sys; print(len(json.load(sys.stdin)))" 2>/dev/null || echo 0)
        infra_info "$COMP" "HEARTBEATS pull-only: $HB_COUNT active (no send-keys)"
    fi
fi


# ==========================================
# STALENESS ALERTER — Phase 1.2
# ==========================================
# With completion-gated last_fired_at, push tasks that crash/hang leave their
# last_fired_at stale. This block detects tasks > 2× their expected cadence
# and alerts admin via Telegram. Dedupes per-task within 6h.

STALE_ALERT_DIR="/tmp/scheduler_stale_alerts"
mkdir -p "$STALE_ALERT_DIR"
STALE_DEDUPE_SECS=21600   # 6 hours

# Only check every 10 min to save API calls + avoid Telegram rate limits
if [ $((NOW_MIN % 10)) -eq 0 ] && [ -n "$SUPABASE_KEY" ]; then
    STALE_ROWS=$(curl -sf "${SUPABASE_URL}/rest/v1/scheduled_tasks?active=eq.true&trigger=eq.push&select=id,agent_name,schedule,task_description,last_fired_at" \
        -H "apikey: $SUPABASE_KEY" \
        -H "Authorization: Bearer $SUPABASE_KEY" 2>/dev/null)
    if [ -n "$STALE_ROWS" ] && [ "$STALE_ROWS" != "[]" ]; then
        STALE_LIST=$(echo "$STALE_ROWS" | /opt/homebrew/bin/python3 - <<'PYEOF'
import json, sys
from datetime import datetime, timezone, timedelta
tasks = json.load(sys.stdin)
now = datetime.now(timezone.utc)

def threshold_for(cron_expr):
    parts = (cron_expr or "").split()
    if len(parts) < 5: return timedelta(hours=48)
    minute, hour, dom, mon, dow = parts[:5]
    # Weekly (specific dow, any dom)
    if dow not in ("*", "?") and dom == "*":
        return timedelta(days=14)
    # Every-N-min (*/N minute)
    if "/" in minute:
        return timedelta(hours=2)
    # Hourly (* hour, specific minute)
    if hour == "*" and minute != "*":
        return timedelta(hours=6)
    # Daily (specific hour)
    if hour != "*":
        return timedelta(hours=48)
    return timedelta(hours=48)

for t in tasks:
    lfa = t.get("last_fired_at")
    if not lfa: continue
    try:
        last = datetime.fromisoformat(lfa.replace("Z", "+00:00"))
    except Exception:
        continue
    th = threshold_for(t.get("schedule", ""))
    age = now - last
    if age > th:
        desc = (t.get("task_description") or "").split("\n")[0][:80]
        print(f"{t.get('id')}|{t.get('agent_name','?')}|{t.get('schedule','')}|{round(age.total_seconds()/3600,1)}|{round(th.total_seconds()/3600,1)}|{desc}")
PYEOF
)
        if [ -n "$STALE_LIST" ]; then
            _ADMIN_BOT=$(/opt/homebrew/bin/python3 -c "import json; print(json.load(open('/Users/YOUR_MAC_USERNAME/.claude/channels/telegram_admin/access.json')).get('botToken',''))" 2>/dev/null || true)
            _FARLEN="YOUR_TELEGRAM_CHAT_ID"
            while IFS='|' read -r tid agent schedule age_h thr_h desc; do
                [ -z "$tid" ] && continue
                _dedupe_file="$STALE_ALERT_DIR/$tid"
                if [ -f "$_dedupe_file" ]; then
                    _last_alert=$(cat "$_dedupe_file" 2>/dev/null || echo 0)
                    _now_ts=$(date +%s)
                    if [ $((_now_ts - _last_alert)) -lt "$STALE_DEDUPE_SECS" ]; then
                        continue
                    fi
                fi
                infra_warn "$COMP" "STALE $tid ($agent): last_fired ${age_h}h ago, threshold ${thr_h}h"
                if [ -n "$_ADMIN_BOT" ]; then
                    _msg="⏱ Stale push task — ${agent} ${schedule} — last fired ${age_h}h ago (threshold ${thr_h}h). Task: ${desc}"
                    curl -sf -X POST "https://api.telegram.org/bot${_ADMIN_BOT}/sendMessage" \
                        -d "chat_id=${_FARLEN}" \
                        -d "text=${_msg}" >/dev/null 2>&1 || true
                fi
                date +%s > "$_dedupe_file"
            done <<< "$STALE_LIST"
            unset _ADMIN_BOT
        fi
    fi
fi
