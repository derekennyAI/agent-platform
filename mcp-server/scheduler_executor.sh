#!/bin/bash
# Scheduler Executor — runs every minute via system crontab
# Reads scheduled_tasks.json, fires any due tasks into the right agent's tmux session
# Part of admin-control MCP Module 4 (Cerebellum)

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TASKS_FILE="$SCRIPT_DIR/scheduled_tasks.json"
COMPONENT_LOG="$SCRIPT_DIR/scheduler.log"
TMUX_BIN="/opt/homebrew/bin/tmux"
COMP="scheduler"

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

# Exit if no tasks file
if [ ! -f "$TASKS_FILE" ]; then
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

TASK_OUTPUT=$(python3 -c "
import json, sys

with open('$TASKS_FILE') as f:
    tasks = json.load(f)

for i, task in enumerate(tasks):
    if not task.get('active', True):
        continue
    print(f\"{i}|{task['id']}|{task['agent_name']}|{task['schedule']}|{task.get('recurring', True)}|{task['task_description']}\")
" 2>&1)

PARSE_EXIT=$?
if [ $PARSE_EXIT -ne 0 ]; then
    infra_error "$COMP" "Failed to parse scheduled_tasks.json: $TASK_OUTPUT"
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

        # Update last_fired_at in local JSON and Supabase
        python3 -c "
import json, urllib.request, urllib.error
from datetime import datetime, timezone

now = datetime.now(timezone.utc).isoformat()

# Update local JSON
with open('$TASKS_FILE') as f:
    tasks = json.load(f)

deactivate = False
for task in tasks:
    if task['id'] == '$tid':
        task['last_fired_at'] = now
        if not task.get('recurring', True):
            task['active'] = False
            deactivate = True
        break

with open('$TASKS_FILE', 'w') as f:
    json.dump(tasks, f, indent=2)

# Sync to Supabase
try:
    svc_key = 'eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Im1mcnpoaWp2ZmJ3dW11dGFqcWVoIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc3MjgzNDMyNywiZXhwIjoyMDg4NDEwMzI3fQ.W6AmTsNcMNo4LHZcjKCOVgzWPasciEtM9KhLAkeKDKE'
    url = '${SUPABASE_URL}/rest/v1/harness_scheduled_tasks?id=eq.$tid'
    body = json.dumps({'last_fired_at': now} | ({'active': False} if deactivate else {})).encode()
    req = urllib.request.Request(url, data=body, method='PATCH', headers={
        'apikey': svc_key, 'Authorization': f'Bearer {svc_key}',
        'Content-Type': 'application/json', 'Prefer': 'return=minimal'
    })
    urllib.request.urlopen(req)
except Exception:
    pass  # Non-fatal — local JSON is the executor's source of truth
" 2>/dev/null
    fi
done
