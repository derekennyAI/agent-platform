# Scheduling System

This document describes how scheduled tasks work on the agent platform. If you are an agent reading this, these are the systems that fire instructions into your tmux session on a cron schedule.

## Overview

Scheduled tasks are persistent cron jobs stored in Supabase. They survive agent restarts, daemon reloads, and machine reboots. A single system-level crontab entry runs the executor every minute, which reads due tasks from the database and fires them into the correct agent's tmux session.

**Data flow:**

```
Agent creates task via MCP tool
        |
        v
Supabase `harness_scheduled_tasks` table  (source of truth)
        |
        v  (write-through)
Local JSON cache: mcp-server/scheduled_tasks.json  (fallback)
        |
        v  (every minute)
scheduler_executor.sh reads due tasks
        |
        v
tmux send-keys → agent's session
```

## Database Table

Tasks live in the `harness_scheduled_tasks` table (aliased from `scheduled_tasks` in the schema):

| Column | Type | Description |
|--------|------|-------------|
| `id` | TEXT PK | Unique task ID (e.g. `sched_1712345678_abc123` or `vera_sched_vera_security_audit`) |
| `agent_name` | TEXT | Which agent this task belongs to (references `agents.name`) |
| `schedule` | TEXT | 5-field cron expression in local timezone |
| `task_description` | TEXT | The exact instruction injected into the agent's session when fired |
| `recurring` | BOOLEAN | `true` = repeats on schedule; `false` = fires once, then auto-deactivates |
| `active` | BOOLEAN | `true` = will fire; `false` = paused/deactivated |
| `created_by` | TEXT | Agent that created this task |
| `created_at` | TIMESTAMPTZ | When the task was created |
| `updated_at` | TIMESTAMPTZ | Last modification time |
| `last_fired_at` | TIMESTAMPTZ | When the executor last fired this task |

## MCP Tools

Agents manage their scheduled tasks through four MCP tools exposed by the `admin-control` MCP server (`mcp-server/server.js`). These are the ONLY way agents should create or modify scheduled tasks.

### schedule_task

Create a new persistent scheduled task.

**Parameters:**
- `schedule` (required): 5-field cron expression in local time
- `task_description` (required): What the agent should do when this fires. This exact text is sent into the tmux session, so be specific and actionable.
- `recurring` (optional, default `true`): Set `false` for one-shot tasks that fire once then auto-deactivate.
- `agent` (optional, admin only): Target agent name. Omit to schedule for yourself.

**Example — recurring task:**
```
schedule: "0 9 * * 1-5"
task_description: "Good morning check: Review inbox for overnight messages, check calendar for today's events, send a morning summary to the user."
recurring: true
```

**Example — one-shot task:**
```
schedule: "30 14 25 4 *"
task_description: "Reminder: Farlen's dentist appointment is in 30 minutes."
recurring: false
```

**What happens internally:**
1. Validates the cron expression (must be exactly 5 fields)
2. Generates a unique task ID (`sched_<timestamp>_<random>`)
3. Writes the task to Supabase `harness_scheduled_tasks`
4. Calls `syncToLocal()` to update the local JSON cache
5. Returns the task ID for future reference

### list_scheduled_tasks

View scheduled tasks.

**Parameters:**
- `agent` (optional, admin only): Filter by agent name. Use `*` for all agents.
- `active_only` (optional, default `true`): Set `false` to include paused tasks.

Non-admin agents can only see their own tasks.

### update_scheduled_task

Modify an existing task's schedule, description, or active state.

**Parameters:**
- `task_id` (required): The task ID to update
- `schedule` (optional): New cron expression
- `task_description` (optional): New instruction text
- `active` (optional): `true` to resume, `false` to pause

Non-admin agents can only update their own tasks.

### delete_scheduled_task

Permanently remove a task by ID.

**Parameters:**
- `task_id` (required): The task ID to delete

Non-admin agents can only delete their own tasks. After deletion, `syncToLocal()` updates the local cache.

## Cron Expression Syntax

Standard 5-field cron expressions. All times are in the machine's **local timezone** (not UTC).

```
┌───────────── minute (0-59)
│ ┌───────────── hour (0-23)
│ │ ┌───────────── day of month (1-31)
│ │ │ ┌───────────── month (1-12)
│ │ │ │ ┌───────────── day of week (0-6, 0=Sunday)
│ │ │ │ │
* * * * *
```

**Supported syntax:**

| Syntax | Meaning | Example |
|--------|---------|---------|
| `*` | Every value | `* * * * *` = every minute |
| `N` | Exact value | `30 9 * * *` = 9:30 AM daily |
| `*/N` | Step (every Nth) | `*/15 * * * *` = every 15 minutes |
| `N-M` | Range | `0 9-17 * * *` = hourly 9 AM to 5 PM |
| `N,M,O` | List | `0 9,12,18 * * *` = 9 AM, noon, 6 PM |
| Mixed | Ranges in lists | `0 9-11,14-16 * * 1-5` = business hours on weekdays |

**Common patterns:**

```
*/5 * * * *       Every 5 minutes
0 * * * *         Every hour on the hour
0 9 * * 1-5       Weekdays at 9 AM
0 9 * * 0         Sundays at 9 AM
0 21 * * *        Daily at 9 PM
0 3 * * *         Daily at 3 AM (maintenance window)
0 */6 * * *       Every 6 hours
30 14 25 12 *     December 25 at 2:30 PM (one-shot)
0 0 1 * *         First of every month at midnight
0 4 * * 0         Sundays at 4 AM
```

**Day of week mapping:** 0=Sunday, 1=Monday, ..., 6=Saturday. The executor converts from `date +%u` (1=Monday, 7=Sunday) to cron convention internally.

## The Executor

**File:** `mcp-server/scheduler_executor.sh`

The executor is a bash script that runs every minute via a single system crontab entry:

```crontab
* * * * * /path/to/fleet/mcp-server/scheduler_executor.sh >> /path/to/scheduler.log 2>&1
```

### Execution flow (every minute)

1. **Load credentials.** The executor runs from crontab, which does not inherit launchd environment variables. It reads `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` from the admin agent's launchd plist (`~/Library/LaunchAgents/com.claude-code.daemon.plist`) using `plistlib`.

2. **Fetch tasks from Supabase.** Queries `harness_scheduled_tasks?active=eq.true` via the REST API.
   - If Supabase is unreachable, falls back to the local JSON cache at `mcp-server/scheduled_tasks.json`.
   - Logs a warning via `infra_warn` when falling back.

3. **Update the local cache.** When Supabase responds successfully, the full task list is written to `scheduled_tasks.json` so the cache stays fresh for offline fallback.

4. **Parse current time.** Reads minute, hour, day-of-month, month, and day-of-week from the system clock in local timezone.

5. **Match cron expressions.** For each active task, the `cron_matches` function parses each of the 5 cron fields and checks if the current time matches. It handles wildcards (`*`), steps (`*/N`), ranges (`N-M`), and comma-separated lists.

6. **Resolve tmux session.** Each agent name maps to a tmux session name via the `get_session` function:
   - `derek` -> `claude-agent`
   - `vera` -> `vera-agent`
   - `nate` -> `nate-agent`
   - (and so on for each registered agent)
   - Unknown agents produce an error and the task is skipped.

7. **Check session is alive.** Runs `tmux has-session -t SESSION`. If the session is down, the task is skipped with an error log.

8. **Fire the task.** Sends the task description into the agent's tmux session:
   ```bash
   tmux send-keys -t "SESSION" "SCHEDULED TASK [TASK_ID]: TASK_DESCRIPTION" Enter
   ```
   The agent receives this as if someone typed it into its terminal.

9. **Update `last_fired_at`.** Writes the current UTC timestamp to both Supabase (PATCH on the task row) and the local JSON cache. For non-recurring tasks (`recurring=false`), also sets `active=false` so they don't fire again.

### Logging

The executor uses `infra_lib.sh` for structured logging:
- **Central log:** `~/logs/infra.log` (all infrastructure events)
- **Component log:** `mcp-server/scheduler.log`

Log format: `TIMESTAMP | LEVEL | scheduler | trace=TRACE_ID | MESSAGE`

Warnings and errors also trigger Telegram alerts to the admin chat via the infra logging library.

## Write-Through Cache

The scheduling system follows the platform's standard write-through cache pattern:

**Writes (MCP tools):**
1. Write to Supabase `harness_scheduled_tasks` first
2. Call `syncToLocal()` which pulls the full task list from Supabase and overwrites `mcp-server/scheduled_tasks.json`

**Reads (executor):**
1. Try Supabase REST API first
2. If that fails (network error, timeout, empty response), read from `mcp-server/scheduled_tasks.json`

**Reads (MCP list tool):**
1. Always reads from Supabase directly (the MCP server is assumed to have connectivity)

This means:
- If Supabase goes down, the executor keeps running from the cached JSON.
- If the local file gets corrupted, the next successful Supabase sync overwrites it.
- There is no conflict resolution — Supabase is always the source of truth.

## Default Crons

New agents get a set of default scheduled tasks loaded via `scripts/load_crons.py`. The defaults are defined in `configs/default-crons.json`.

### Loading defaults

```bash
# Load defaults for a new agent
python3 scripts/load_crons.py --agent myagent

# Preview without writing
python3 scripts/load_crons.py --agent myagent --dry-run

# Use a custom crons file
python3 scripts/load_crons.py --agent myagent --crons path/to/custom-crons.json
```

The loader:
1. Reads cron definitions from the JSON file
2. Prefixes each cron ID with the agent name (e.g. `myagent_sched_myagent_session_memory`)
3. Checks which crons already exist for this agent in Supabase
4. Skips duplicates, inserts new ones
5. Reports what was loaded vs. skipped

### Default task set

| ID suffix | Schedule | Purpose | Enabled |
|-----------|----------|---------|---------|
| `session_memory` | `*/30 * * * *` | Review idle conversations, write session summaries to `memory/sessions/`, extract user preferences | Yes |
| `admin_poll` | `*/5 * * * *` | Check `admin_tasks` table for pending tasks from admin agent, execute and mark complete | Yes |
| `heartbeat` | `0 */6 * * *` | Verify MCP connectivity, vault access, disk space. Alert user if anything is down | Yes |
| `token_refresh` | `0 */2 * * *` | Check OAuth tokens for expiring services, refresh via refresh_token grant, update vault | Yes |
| `qmd_refresh` | `0 */2 * * *` | Re-index memory files for QMD semantic search (skip if QMD not installed) | No |
| `security_audit` | `0 * * * *` | Run security watch script, check for file tampering, unauthorized access, suspicious patterns | Yes |
| `inbox_check` | `0 * * * *` | Scan connected email accounts for unread messages, flag actionable items to user | No |
| `daily_digest` | `0 21 * * *` | Compile daily activity summary — sessions, tasks completed, errors, open items — send to user | Yes |
| `daily_memory_compact` | `0 3 * * *` | Merge same-day session summaries, deduplicate memories, prune sessions older than 30 days | Yes |
| `weekly_memory_review` | `0 4 * * 0` | Deep memory maintenance — stale projects, contradictory feedback, orphaned files, index health | Yes |

Tasks marked "No" in the Enabled column are present but inactive. They activate when the relevant service is connected (e.g., QMD installed, Gmail authorized).

## Scheduled Tasks vs. Heartbeats

These serve different purposes and should not be confused.

**Scheduled tasks** DO things on a fixed schedule:
- Run a script
- Check an inbox
- Generate a report
- Refresh a token

They fire at the exact cron time and do their work.

**Heartbeats** WATCH for signals — they detect events that happened since the last check:
- New emails arrived
- Website leads submitted
- Calendar events approaching
- External API status changes

Heartbeats typically run on a short cycle (5-10 minutes) and are implemented as scheduled tasks with a polling pattern. The heartbeat default (`0 */6 * * *`) is a system health heartbeat, not a signal-watching heartbeat. Signal-watching heartbeats (like `inbox_check`) run hourly or more frequently.

The key difference: a scheduled task's description says "do X now." A heartbeat's description says "check if X happened since you last checked, and react if it did."

## Important Rules

1. **Agents must NOT use CronCreate or modify the system crontab.** All scheduling goes through the MCP `schedule_task` / `update_scheduled_task` / `delete_scheduled_task` tools. The only system crontab entry is the executor itself, set up once during platform installation.

2. **Do not recreate crons on startup.** Scheduled tasks persist in Supabase. When an agent restarts, its crons are already there. The startup instructions explicitly warn: "Do NOT re-register crons on startup."

3. **Task descriptions should be specific.** The `task_description` text is injected verbatim into the agent's session. Write it like you're giving instructions to the agent. Vague descriptions like "check stuff" will produce vague results.

4. **One-shot tasks auto-deactivate.** Setting `recurring=false` means the task fires once and then the executor sets `active=false`. It is not deleted — it remains in the database as a record. To fully remove it, call `delete_scheduled_task`.

5. **Admin agents can schedule for any agent.** The `agent` parameter on `schedule_task` allows admin agents (derek, dereklm) to create tasks in other agents' schedules. Non-admin agents can only schedule for themselves.

6. **Time is local.** Cron expressions are evaluated against the machine's local timezone, not UTC. If the machine is in America/Los_Angeles, `0 9 * * *` fires at 9:00 AM Pacific.

## Troubleshooting

**Task not firing:**
1. Check `list_scheduled_tasks` — is `active` true?
2. Verify the cron expression matches the expected time
3. Check if the agent's tmux session is running: `tmux has-session -t AGENT-agent`
4. Check the executor log: `mcp-server/scheduler.log`
5. Check the central infra log: `~/logs/infra.log`
6. Verify Supabase connectivity — if both Supabase and local cache are empty, nothing fires

**Task fires but agent doesn't respond:**
- The agent may be mid-conversation or processing another task. The instruction goes into the tmux buffer and will be processed when the agent is free.
- Check if the agent's session is responsive: `tmux send-keys -t AGENT-agent "" Enter`

**Duplicate task IDs:**
- The `id` column is a primary key. If `load_crons.py` tries to insert a task with an existing ID, the insert is skipped. MCP-created tasks use timestamp-based IDs that are effectively unique.

**Stale local cache:**
- If Supabase was down during a write, the local cache may be stale. The next successful executor run fetches from Supabase and overwrites the local file.
