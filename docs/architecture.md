# Architecture

Complete architectural overview of the Fleet. This document describes how the platform runs persistent AI agents powered by Claude Code on macOS, covering every component from the daemon layer through the database.

---

## System Overview

The platform runs one or more AI agents as persistent background services on macOS. Each agent is a Claude Code CLI session running inside a tmux pseudo-terminal, managed by launchd (the macOS init system). Agents share a common infrastructure layer (MCP server, scheduler, hooks) but are isolated in their own workspaces with scoped credentials and permissions.

The core architecture:

```
                       ┌──────────────────────┐
                       │      launchd          │
                       │  (macOS init system)  │
                       └──────┬───────────────┘
                              │ starts/restarts on crash
                              ▼
                       ┌──────────────────────┐
                       │   launcher.sh         │
                       │   (per-agent)         │
                       └──────┬───────────────┘
                              │ creates tmux session
                              ▼
           ┌─────────────────────────────────────┐
           │         tmux session                 │
           │   ┌─────────────────────────────┐   │
           │   │   Claude Code CLI            │   │
           │   │   --channels telegram        │   │
           │   │   --append-system-prompt     │   │
           │   └──────┬──────────────────────┘   │
           └──────────┼──────────────────────────┘
                      │ MCP (stdio)
                      ▼
           ┌──────────────────────┐      ┌──────────────────┐
           │  admin-control MCP   │◄────►│    Supabase       │
           │  server (Node.js)    │      │  (11 tables)      │
           └──────────────────────┘      └──────────────────┘
```

Each agent has:
- A dedicated launchd plist at `~/Library/LaunchAgents/com.<agent-name>-agent.daemon.plist`
- A tmux session named `<agent-name>-agent`
- A workspace at `~/<agent-name>/`
- Its own Telegram bot (optional) for communication
- Scoped credentials in the shared Supabase vault
- Its own scheduled tasks, state, and memory

Agents are resilient: launchd has `KeepAlive` set to `true`, so if the tmux session dies, the launcher exits, and launchd restarts it automatically.

---

## Component Map

### Platform Repository (`fleet/`)

The shared platform code that all agents depend on:

```
fleet/
├── setup.sh                    # One-command setup (prereq check, npm install, crontab)
├── mcp-server/
│   ├── server.js               # MCP admin-control server — 6 modules, 30+ tools
│   ├── scheduler_executor.sh   # Cron dispatcher — runs every minute via system crontab
│   ├── scheduled_tasks.json    # Local cache of scheduled tasks (write-through from Supabase)
│   ├── infra_lib.sh            # Structured logging library (INFO/WARN/ERROR/CRITICAL + Telegram alerts)
│   ├── vault_client.py         # Python library for reading vault credentials via Supabase REST
│   ├── vault_template.py       # Importable module for skill scripts (provides get_cred, WORKSPACE, etc.)
│   └── skill_validator.py      # Post-build security scanner for skill code
├── hooks/
│   └── approval-gate.py        # PreToolUse hook — two-tier approval routing via Telegram
├── skills/                     # Reusable skills (shared across agents)
│   ├── agent-setup/            # Agent creation (create_agent.py), OAuth connection
│   ├── skill-maker/            # Meta-skill: agents build new skills
│   ├── shell/                  # Shell command execution + user scripts (Gmail, Toggl, etc.)
│   ├── playwright/             # Browser automation
│   ├── frontend-design/        # UI/UX design guidance
│   ├── diagnose/               # System diagnostics
│   └── ux-review/              # UX analysis
├── schema/
│   └── create_harness_schema.sql  # DDL for all 11 Supabase tables
├── configs/
│   ├── agents.json             # Agent registry template
│   ├── default-crons.json      # Default scheduled tasks (10 crons per agent)
│   ├── startup-instructions.md # Template startup instructions
│   └── template/               # Per-agent file templates
│       ├── CLAUDE.md           # Agent identity, safety rules, memory system, analytics
│       ├── .mcp.json           # MCP server connection config
│       ├── settings.json       # Model selection, permissions, hook config
│       └── launchd-daemon.plist # launchd plist template
├── scripts/
│   ├── launcher.sh             # Daemon launcher template (tmux + Claude Code)
│   ├── load_crons.py           # Push default crons from configs/ to Supabase
│   ├── claude_oauth_server.py  # OAuth callback server (Google, Microsoft)
│   ├── security_watch.py       # Security tripwire monitor
│   ├── security_alert.sh       # Alert helper
│   └── agent_health_check.py   # Health monitoring + auto-restart + dashboard
└── docs/                       # Documentation
```

### Agent Workspace (`~/<agent-name>/`)

Each agent gets an isolated workspace created by `create_agent.py`:

```
~/<agent-name>/
├── CLAUDE.md                   # Agent identity, safety rules, capabilities
├── .mcp.json                   # MCP server connection (points to platform mcp-server/)
├── settings.json               # Model (claude-opus-4-6), permissions (bypassPermissions), hooks
├── startup-instructions.md     # Boot-time instructions
├── .config/<agent-name>/       # Local credential cache
│   └── accounts/               # Per-account OAuth token directories
│       └── user_at_domain/
│           ├── google-token.json
│           └── microsoft-token.json
├── .state/                     # Local state cache (write-through from Supabase)
│   └── *.json                  # One file per state key
├── memory/                     # Persistent memory files
│   ├── MEMORY.md               # Memory index (loaded every conversation, <200 lines)
│   ├── *_soul.md               # Agent identity/personality
│   ├── user_*.md               # User profile
│   ├── feedback.md             # User corrections + confirmed approaches
│   ├── project_*.md            # Project-specific memories
│   ├── reference_*.md          # External resource pointers
│   ├── failed_approaches.md    # Known dead ends
│   └── sessions/               # Conversation summaries
│       ├── YYYY-MM-DD.md       # Daily summaries
│       └── YYYY-MM-DD_weekly.md # Weekly reviews
├── analytics.jsonl             # Local analytics backup (one JSON line per session)
├── skills/                     # Agent-specific skills (if any)
└── reports/                    # Generated reports (if any)
```

### Global Configuration (`~/.claude/`)

Shared across all agents on the machine:

```
~/.claude/
├── launcher.sh                 # Symlink or copy of platform launcher
├── settings.json               # Global Claude Code settings
├── daemon.log                  # Launcher log output
├── daemon-stdout.log           # Daemon stdout
├── daemon-stderr.log           # Daemon stderr
├── channels/
│   ├── telegram/               # Primary agent Telegram config
│   │   └── .env                # TELEGRAM_BOT_TOKEN
│   └── telegram_<agent>/       # Per-agent Telegram config
│       └── .env
├── hooks/
│   └── approvals/              # Approval gate cache (short-lived .approved files)
└── secrets.json                # Legacy credential file (replaced by vault)
```

---

## The MCP Admin-Control Server

The MCP server (`mcp-server/server.js`) is a single Node.js process that provides the shared infrastructure layer to all agents. It communicates with Claude Code via the Model Context Protocol over stdio. Each agent spawns its own instance of the server (configured via `.mcp.json` in the agent's workspace), but they all share the same Supabase backend.

The server identifies the calling agent via the `AGENT_NAME` environment variable passed in `.mcp.json`:

```json
{
  "mcpServers": {
    "admin-control": {
      "command": "node",
      "args": ["<platform-dir>/mcp-server/server.js"],
      "env": {
        "AGENT_NAME": "<agent-name>",
        "SUPABASE_URL": "<url>",
        "SUPABASE_SERVICE_KEY": "<key>"
      }
    }
  }
}
```

### Module 1: Admin Verification

Inter-agent task queue for coordinated work.

| Tool | Description | Access |
|------|-------------|--------|
| `create_admin_task` | Assign a task to another agent | Admin only |
| `verify_task` | Confirm a task is legitimate before executing | Any agent |
| `complete_task` | Mark a task as done | Target agent |
| `list_pending_tasks` | Check for tasks assigned to you | Any agent |

Admin agents (currently "derek" and "dereklm") can create tasks. Sub-agents poll for pending tasks every 5 minutes via a scheduled cron.

### Module 2: Credential Vault

Scoped secret storage backed by the `agent_credentials` table.

| Tool | Description | Access |
|------|-------------|--------|
| `store_credential` | Save a credential (upserts on conflict) | Own + admin cross-agent |
| `get_credential` | Retrieve a credential value | Own + admin cross-agent |
| `list_credentials` | List connected services (values hidden) | Own |
| `revoke_credential` | Remove a credential | Own + admin cross-agent |

Credentials are scoped by `(agent_name, service, credential_key)`. Non-admin agents can only access their own credentials.

### Module 2.5: Service Connection

Atomic operation that combines credential storage, skill catalog entry, and permission grant in one step.

| Tool | Description | Access |
|------|-------------|--------|
| `connect_service` | Store creds + create skill + grant permission | Own + admin cross-agent |
| `disconnect_service` | Revoke creds + revoke skill permission | Own + admin cross-agent |

The `SERVICE_SKILL_MAP` in server.js maps service names (gmail, google_calendar, microsoft_mail, notion, etc.) to skill categories and description templates.

### Module 3: Skills Management

Skill catalog and permission system backed by `skills` and `skill_permissions` tables.

| Tool | Description | Access |
|------|-------------|--------|
| `grant_skill` | Give an agent access to a skill | Admin only |
| `revoke_skill` | Remove access (soft revoke via `revoked_at`) | Admin only |
| `list_skill_catalog` | Browse all skills, optionally by category | Any agent |
| `list_skills` | List skills granted to an agent | Own + admin cross-agent |
| `my_skills` | User-friendly list of what you can do | Any agent |
| `run_skill` | Execute a skill script (permission-checked) | Granted agents |

`run_skill` checks the agent's permissions, looks up the `script_path` from the catalog, and executes the script with `python3` in a subprocess, passing `AGENT_NAME` and `SUPABASE_SERVICE_KEY` as env vars.

### Module 4: Persistent Scheduler (Cerebellum)

Cron-like scheduling backed by the `harness_scheduled_tasks` table.

| Tool | Description | Access |
|------|-------------|--------|
| `schedule_task` | Create a cron job (5-field cron expression, local TZ) | Own + admin cross-agent |
| `list_scheduled_tasks` | View scheduled tasks (admin can use `*` for all) | Own + admin cross-agent |
| `update_scheduled_task` | Modify schedule, description, or active state | Own + admin |
| `delete_scheduled_task` | Remove a task | Own + admin |

All writes go to Supabase first, then `syncToLocal()` pulls the full task list from Supabase and writes it to `mcp-server/scheduled_tasks.json` for the executor to read.

### Module 4b: Agent State

Persistent key-value state with write-through caching.

| Tool | Description | Access |
|------|-------------|--------|
| `get_state` | Read a value (Supabase first, local fallback) | Own |
| `set_state` | Write a value (both Supabase and local) | Own |
| `delete_state` | Remove a key (both Supabase and local) | Own |
| `list_state` | List all state keys | Own |

Local state files live at `~/<agent-name>/.state/<key>.json`.

### Module 5: Analytics and Logging

Observability tools for tracking sessions and infrastructure events.

| Tool | Description | Access |
|------|-------------|--------|
| `log_interaction` | Log conversation metadata (categories, outcome, skill gaps) | Any agent |
| `log_infra_event` | Log infrastructure events (errors, warnings) | Any agent |
| `start_session` | Register a new session | Any agent |
| `end_session` | Close a session with stats | Any agent |

### Module 6: Agent Registry

Central registry of all agents backed by the `agents` table.

| Tool | Description | Access |
|------|-------------|--------|
| `get_agent_info` | Get agent details (persona, status, model) | Own + admin cross-agent |
| `list_agents` | List all registered agents | Admin only |
| `update_agent_status` | Change agent status (active/idle/blocked/offline) | Admin only |

---

## Data Flow: Message to Response

Here is the full path of a Telegram message from a user to an agent and back:

### 1. Inbound Message

```
User's phone
    │ HTTPS (Telegram Bot API)
    ▼
Telegram servers
    │ Polling/webhook (managed by Claude Code Telegram channel plugin)
    ▼
Claude Code Telegram channel plugin
    │ Injects message as <channel source="telegram" ...> tag
    ▼
Claude Code CLI session (inside tmux)
```

The Telegram channel plugin (`plugin:telegram@claude-plugins-official`) is loaded via the `--channels` flag in launcher.sh. It handles all communication with the Telegram Bot API, including message polling. When a message arrives, it appears in the Claude Code session as a structured tag:

```xml
<channel source="telegram" chat_id="123456" message_id="789" user="farlen" ts="2026-04-12T10:00:00Z">
  User's message text here
</channel>
```

### 2. Processing

Claude Code processes the message using its full context:
1. **CLAUDE.md** (agent identity, safety rules, memory system instructions)
2. **Memory files** (loaded via MEMORY.md index at session start)
3. **MCP tools** (vault, scheduler, state, skills — all via the admin-control server)
4. **Built-in tools** (Bash, Read, Write, Edit, Grep, Glob, etc.)
5. **Channel plugins** (Telegram reply, react, edit_message)

### 3. Tool Use and Hooks

When Claude decides to use a tool, the PreToolUse hook fires first:

```
Claude wants to use a tool
    │
    ▼
PreToolUse hook fires (settings.json matcher)
    │ Matches: Bash, Edit, Write, iMessage reply
    ▼
approval-gate.py (hooks/approval-gate.py)
    │ classify_action(tool_name, tool_input)
    │
    ├── Returns None → tool executes immediately (no gate)
    ├── Returns "admin" → sends approval request to admin's Telegram
    └── Returns "user" → sends approval request to user's Telegram
                          │
                          ▼
                     Poll for yes/no response (120s timeout)
                          │
                          ├── Approved → tool executes
                          └── Denied/Timeout → tool blocked
```

### 4. Outbound Response

```
Claude Code generates response
    │
    ▼
Telegram reply tool (mcp__plugin_telegram_telegram__reply)
    │ chat_id + text + optional files
    ▼
Telegram Bot API (HTTPS POST)
    │
    ▼
User's phone (push notification)
```

The Telegram reply tool is never gated by the approval hook (it is in the "no gate" list) to prevent breaking the agent's ability to respond.

### 5. Scheduled Task Flow

```
System crontab (every minute)
    │
    ▼
scheduler_executor.sh
    │ Reads tasks from Supabase (falls back to local JSON)
    │ Evaluates cron expressions against current time
    │
    ▼ (for each due task)
tmux send-keys -t <agent>-agent "SCHEDULED TASK [<id>]: <description>" Enter
    │
    ▼
Claude Code receives the text as if typed by a user
    │ Processes it using full context + MCP tools
    │
    ▼
Updates last_fired_at in Supabase + local JSON
```

---

## Database Layer

The platform uses 11 Supabase tables. All table DDL is in `schema/create_harness_schema.sql`.

### Table Reference

| # | Table | Purpose | Key Columns |
|---|-------|---------|-------------|
| 1 | `agents` | Central agent registry | name, persona, human, status, model, timezone, billing_type |
| 2 | `scheduled_tasks` | Persistent cron jobs | id, agent_name, schedule, task_description, recurring, active, last_fired_at |
| 3 | `skills` | Skill catalog | name, description, user_description, category, requires_credentials, script_path |
| 4 | `skill_permissions` | Agent-skill access control | agent_name, skill_name, granted_by, revoked_at |
| 5 | `agent_sessions` | Session lifecycle tracking | agent_name, started_at, ended_at, messages_in/out, topics, ended_reason |
| 6 | `interaction_logs` | Conversation metadata | agent_name, categories, outcome, skill_used, skill_gap, satisfaction |
| 7 | `infra_events` | Centralized infra logging | level, component, trace_id, message, metadata |
| 8 | `agent_credentials` | Scoped credential vault | agent_name, service, credential_key, credential_value, metadata |
| 9 | `agent_state` | Persistent key-value state | agent_name, key, value (JSONB) |
| 10 | `agent_analytics` | Usage events | agent_name, event, metadata |
| 11 | `admin_tasks` | Inter-agent task queue | agent_name, created_by, task_description, status, result |

### Write-Through Cache Pattern

All Supabase-backed data follows the same caching pattern:

```
Agent calls MCP tool (e.g., set_state, schedule_task)
    │
    ├──► Write to Supabase (primary)
    │
    └──► Write to local file (cache)
         • State: ~/<agent>/.state/<key>.json
         • Tasks: mcp-server/scheduled_tasks.json
         • Credentials: ~/<agent>/.config/<agent>/accounts/

Agent calls MCP tool (e.g., get_state, list_scheduled_tasks)
    │
    ├──► Try Supabase first
    │    ├── Success → return data + update local cache
    │    └── Failure ──┐
    │                  ▼
    └──► Fall back to local file
```

This pattern provides:
- **Reliability**: Agents work offline or when Supabase is down (local cache serves reads)
- **Consistency**: Supabase is always the source of truth when available
- **Performance**: Local cache is updated on every successful Supabase read, staying fresh
- **Automatic sync**: The scheduler executor syncs its local task cache from Supabase on every run

### Credential Scoping

The `agent_credentials` table enforces isolation. The unique constraint is `(agent_name, service, credential_key)`. The MCP server checks `AGENT_NAME` on every credential operation:

- Non-admin agents can only read/write their own credentials
- Admin agents can read/write any agent's credentials (used during setup)
- The `vault_client.py` and `vault_template.py` libraries enforce this in Python scripts

### Indexes

Key indexes for performance:

```sql
idx_scheduled_tasks_active    — WHERE active = true (scheduler hot path)
idx_agent_credentials_lookup  — (agent_name, service) (vault lookups)
idx_agent_state_agent         — (agent_name) (state reads)
idx_infra_events_ts           — (timestamp) (log queries)
idx_admin_tasks_agent         — (agent_name, status) (task polling)
```

---

## Scheduler

The scheduler is a two-part system: the MCP tools manage task definitions in Supabase, and a bash executor fires due tasks into agent sessions.

### How It Works

1. **System crontab** runs `scheduler_executor.sh` every minute:
   ```
   * * * * * /path/to/fleet/mcp-server/scheduler_executor.sh >> /tmp/scheduler.log 2>&1
   ```

2. **Executor reads tasks** — tries Supabase first (via curl + REST API), falls back to local `scheduled_tasks.json` if Supabase is unreachable.

3. **Cron matching** — for each active task, the executor parses the 5-field cron expression and checks if it matches the current minute/hour/dom/month/dow. Supports wildcards (`*`), steps (`*/N`), ranges (`N-M`), and lists (`N,M,O`).

4. **Firing** — for each matching task, the executor:
   - Looks up the agent's tmux session name (e.g., `derek` -> `claude-agent`, `vera` -> `vera-agent`)
   - Checks that the session exists (`tmux has-session`)
   - Sends the task description into the session: `tmux send-keys -t <session> "SCHEDULED TASK [<id>]: <description>" Enter`
   - The text appears in Claude Code as if a user typed it

5. **Post-fire** — updates `last_fired_at` in both Supabase (via REST PATCH) and local JSON. Non-recurring tasks are deactivated (`active = false`).

6. **Local cache sync** — when reading from Supabase succeeds, the executor writes the full task list to local JSON, keeping the offline fallback fresh.

### Environment Loading

Since crontab does not inherit launchd environment variables, the executor reads `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` from the primary agent's launchd plist using Python's `plistlib`:

```bash
_PLIST="$HOME/Library/LaunchAgents/com.claude-code.daemon.plist"
python3 -c "import plistlib; ..."  # extracts env vars
eval "$_ENV_EXPORTS"
```

### Default Crons

Each agent gets 10 default scheduled tasks (defined in `configs/default-crons.json`):

| Schedule | Task | Enabled |
|----------|------|---------|
| `*/30 * * * *` | Session memory review/compaction | Yes |
| `*/5 * * * *` | Admin task polling | Yes |
| `0 */6 * * *` | Heartbeat check (MCP, vault, disk) | Yes |
| `0 */2 * * *` | OAuth token refresh | Yes |
| `0 */2 * * *` | QMD index refresh | No (requires QMD) |
| `0 * * * *` | Security audit | Yes |
| `0 * * * *` | Inbox check | No (requires Gmail) |
| `0 21 * * *` | Daily digest | Yes |
| `0 3 * * *` | Daily memory compaction | Yes |
| `0 4 * * 0` | Weekly memory review | Yes |

---

## Infrastructure Logging

The `infra_lib.sh` library provides structured logging for all bash-based infrastructure scripts:

```bash
source "infra_lib.sh"
infra_info  "scheduler" "Fired 3 tasks this run"
infra_warn  "token_refresh" "Gmail token expires in 30 minutes"
infra_error "scheduler" "Session 'vera-agent' not running — task skipped"
infra_critical "daemon" "Agent derek crashed — launchd restart imminent"
```

Log format: `TIMESTAMP | LEVEL | COMPONENT | trace=ID | MESSAGE`

Logs go to two destinations:
1. **Central log**: `~/logs/infra.log` (all systems, all agents)
2. **Component log**: `$COMPONENT_LOG` (set by the calling script)

Levels WARN, ERROR, and CRITICAL also send a Telegram alert to the admin chat using the primary agent's bot token.

---

## Security Model

### Workspace Isolation

Each agent's `CLAUDE.md` contains behavioral rules:
- Only read/write files within `~/<agent-name>/`
- Never access other agents' directories
- Use MCP vault for credentials, never hardcode tokens
- Use `AGENT_NAME` env var for scoping

### Approval Gate

The `hooks/approval-gate.py` PreToolUse hook provides two-tier approval:

**Admin tier** (routed to platform admin's Telegram):
- Modifying shared infra (MCP server, scheduler, security scripts)
- Cross-workspace file access
- Git push to platform repos
- Supabase schema changes (CREATE/ALTER/DROP TABLE)

**User tier** (routed to the agent's user's Telegram):
- Sending emails (`send_email`)
- Sending iMessages
- Falls back to admin if user chat ID is not configured

**No gate** (executes immediately):
- File reads, searches, grep
- Editing own workspace
- Telegram replies, reactions, edits
- Normal tool use

The primary admin agent ("derek") is exempt from admin-tier gates since it manages the infrastructure directly.

### Post-Build Skill Validation

`skill_validator.py` scans skill code for:
- Hardcoded credential file paths (secrets.json, google-token.json, etc.)
- Cross-workspace file access patterns
- Non-vault credential loading (direct file reads of token files)
- Hardcoded API keys, JWTs, OAuth secrets
- Known secret patterns (Anthropic, GitHub, Slack, Google)

Certain infrastructure files (vault_client.py, server.js, create_agent.py, etc.) are in the allowlist.

### Security Monitoring

`scripts/security_watch.py` runs hourly via cron and checks:
- File integrity of critical files (AGENTS.md, SOUL.md, MEMORY.md)
- Security canary presence
- Suspicious exec patterns (rm -rf, base64|sh, ssh-keygen, etc.)
- Unauthorized cross-workspace access

---

## File System Layout Summary

```
~/Library/LaunchAgents/
└── com.<agent>-agent.daemon.plist    # launchd plist (per agent)

~/.claude/
├── launcher.sh                       # Daemon launcher
├── channels/telegram*/               # Telegram bot tokens
├── hooks/approvals/                  # Approval gate cache
└── daemon*.log                       # Daemon logs

~/<agent-name>/                       # Agent workspace
├── CLAUDE.md                         # Identity + rules
├── .mcp.json                         # MCP config
├── settings.json                     # Model + hooks
├── startup-instructions.md           # Boot protocol
├── memory/                           # Persistent memory
├── .state/                           # State cache
├── .config/<agent>/                  # Credential cache
└── analytics.jsonl                   # Usage log

fleet/                       # Shared platform
├── mcp-server/                       # MCP server + scheduler + vault libs
├── hooks/                            # PreToolUse approval gate
├── skills/                           # Shared skill library
├── schema/                           # Database DDL
├── configs/                          # Templates + defaults
└── scripts/                          # Launcher, cron loader, security, health
```
