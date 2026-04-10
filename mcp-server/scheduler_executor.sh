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
# (crontab jobs don't inherit launchd env vars)
if [ -z "$SUPABASE_SERVICE_KEY" ]; then
    _PLIST="$HOME/Library/LaunchAgents/com.claude-code.daemon.plist"
    if [ -f "$_PLIST" ]; then
        _ENV_EXPORTS=$(python3 << PYEOF
import plistlib, shlex
with open("$_PLIST", "rb") as f:
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
SUPABASE_URL="${SUPABASE_URL:-}"
SUPABASE_KEY="${SUPABASE_SERVICE_KEY:-}"

# Source infra logging library
source "$SCRIPT_DIR/infra_lib.sh"

# Agent name → tmux session name (bash 3.x compatible — no associative arrays)
get_session() {
    case "$1" in
        derek)   echo "claude-agent" ;;
        dereklm) echo "dereklm-agent" ;;
        vera)    echo "vera-agent" ;;
        nate)    echo "nate-agent" ;;
        blake)   echo "blake-agent" ;;
        julie)   echo "julie-agent" ;;
        test)    echo "test-agent" ;;
        macgyver) echo "macgyver-agent" ;;
        *)       echo "" ;;
    esac
}

# Try to fetch tasks from Supabase; fall back to local JSON cache
TASKS_SOURCE="supabase"
if [ -n "$SUPABASE_KEY" ]; then
    SUPABASE_TASKS=$(curl -sf "${SUPABASE_URL}/rest/v1/harness_scheduled_tasks?active=eq.true&select=id,agent_name,schedule,task_description,recurring,active,last_fired_at" \
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

for i, task in enumerate(tasks):
    if not task.get('active', True):
        continue
    print(f\"{i}|{task['id']}|{task['agent_name']}|{task['schedule']}|{task.get('recurring', True)}|{task['task_description']}\")
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
    print(f\"{i}|{task['id']}|{task['agent_name']}|{task['schedule']}|{task.get('recurring', True)}|{task['task_description']}\")
" 2>&1)
fi

PARSE_EXIT=$?
if [ $PARSE_EXIT -ne 0 ]; then
    infra_error "$COMP" "Failed to parse tasks ($TASKS_SOURCE): $TASK_OUTPUT"
    exit 1
fi

echo "$TASK_OUTPUT" | while IFS='|' read -r idx tid agent schedule recurring desc; do
    [ -z "$tid" ] && continue

    if cron_matches "$schedule"; then
        session="$(get_session "$agent")"
        if [ -z "$session" ]; then
            infra_error "$COMP" "Unknown agent '$agent' for task $tid — task cannot fire"
            continue
        fi

        if ! $TMUX_BIN has-session -t "$session" 2>/dev/null; then
            infra_error "$COMP" "Session '$session' not running — task $tid ($agent) skipped: ${desc:0:80}"
            continue
        fi

        # Fire the task
        if $TMUX_BIN send-keys -t "$session" "SCHEDULED TASK [$tid]: $desc" Enter 2>/dev/null; then
            infra_info "$COMP" "FIRED $tid ($agent): ${desc:0:80}"
            FIRED=$((FIRED + 1))
        else
            infra_error "$COMP" "send-keys failed for $tid ($agent) session '$session'"
            continue
        fi

        # Update last_fired_at — Supabase is source of truth, local JSON is cache
        python3 -c "
import json, urllib.request, urllib.error, os
from datetime import datetime, timezone

now = datetime.now(timezone.utc).isoformat()
tid = '$tid'
recurring = '$recurring'
deactivate = recurring.lower() == 'false'

svc_key = os.environ.get('SUPABASE_SERVICE_KEY', '')
base_url = os.environ.get('SUPABASE_URL', 'https://mfrzhijvfbwumutajqeh.supabase.co')

# Primary: Update Supabase
if svc_key:
    try:
        url = f'{base_url}/rest/v1/harness_scheduled_tasks?id=eq.{tid}'
        body = json.dumps({'last_fired_at': now} | ({'active': False} if deactivate else {})).encode()
        req = urllib.request.Request(url, data=body, method='PATCH', headers={
            'apikey': svc_key, 'Authorization': f'Bearer {svc_key}',
            'Content-Type': 'application/json', 'Prefer': 'return=minimal'
        })
        urllib.request.urlopen(req)
    except Exception:
        pass

# Cache: Update local JSON
try:
    with open('$TASKS_FILE') as f:
        tasks = json.load(f)
    for task in tasks:
        if task['id'] == tid:
            task['last_fired_at'] = now
            if deactivate:
                task['active'] = False
            break
    with open('$TASKS_FILE', 'w') as f:
        json.dump(tasks, f, indent=2)
except Exception:
    pass
" 2>/dev/null
    fi
done
