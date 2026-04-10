# Agent Platform

A complete platform for running persistent AI agents powered by Claude Code. Each agent has its own workspace, credentials vault, skills, scheduled tasks, and communication channels.

## What you get

- **Persistent agents** that run 24/7 as macOS daemons (launchd + tmux)
- **Supabase-backed state** — scheduled tasks, credentials, analytics, and agent state all persist in the database with automatic local fallback
- **Credential vault** — OAuth tokens and API keys stored in Supabase, scoped per agent
- **Dynamic skills** — agents can use and build new capabilities on the fly
- **Workspace isolation** — each agent has its own directory, credentials, and permissions
- **Scheduled tasks** — cron-like automation managed via MCP tools, persisted in Supabase
- **Write-through cache** — all DB writes sync to local files automatically; reads fall back to local if Supabase is down
- **Multi-channel communication** — Telegram, iMessage, email
- **Multi-agent coordination** — admin tasks queue, inter-agent communication
- **Memory system** — persistent file-based memory that builds over time
- **Security** — post-build skill validation, workspace isolation, credential scoping

## Prerequisites

- macOS (Apple Silicon recommended, 16GB+ RAM)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- [Node.js](https://nodejs.org/) 18+
- Python 3.10+
- [tmux](https://github.com/tmux/tmux) (`brew install tmux`)
- A [Supabase](https://supabase.com/) project (free tier works)
- An Anthropic API key or Claude Max subscription

## Quick Start

```bash
git clone https://github.com/derekennyAI/agent-platform.git
cd agent-platform
./setup.sh
```

The setup script will:
1. Check prerequisites (node, python3, tmux, claude)
2. Create `.env` from template (edit with your API keys)
3. Install MCP server dependencies
4. Check Supabase tables
5. Set up the scheduler crontab entry
6. Guide you through creating your first agent

### Create your first agent

```bash
python3 skills/agent-setup/create_agent.py \
  --name myagent \
  --persona "Alex" \
  --human "Your Name" \
  --bot-token "123456:ABC..." \
  --user-id "your-telegram-id"
```

### Load default scheduled tasks

```bash
python3 scripts/load_crons.py --agent myagent
```

### Start the agent

```bash
launchctl load ~/Library/LaunchAgents/com.myagent-agent.daemon.plist
```

Your agent is now running. Message it on Telegram.

## Architecture

```
agent-platform/               # This repo — the shared platform
├── setup.sh                   # One-command setup
├── mcp-server/                # MCP admin-control server (Node.js)
│   ├── server.js              # MCP tools: vault, skills, scheduling, state
│   ├── scheduler_executor.sh  # Fires due tasks (runs every minute via crontab)
│   ├── infra_lib.sh           # Structured logging library
│   ├── vault_client.py        # Python vault access library
│   ├── vault_template.py      # Importable module for skill scripts
│   └── skill_validator.py     # Post-build security scanner
├── skills/                    # Reusable skills (available to all agents)
│   ├── skill-maker/           # Meta-skill: builds new skills
│   ├── shell/                 # Shell command execution + Gmail tools
│   ├── agent-setup/           # Agent creation + OAuth connection
│   ├── playwright/            # Browser automation
│   ├── frontend-design/       # UI/UX design guidance
│   ├── diagnose/              # System diagnostics
│   └── ux-review/             # UX analysis
├── schema/                    # Supabase database schema (11 tables)
├── configs/                   # Agent config templates + default crons
├── scripts/                   # Launcher, cron loader, security monitoring
│   ├── launcher.sh            # Templated daemon launcher
│   ├── load_crons.py          # Push default crons to Supabase
│   ├── claude_oauth_server.py # OAuth callback server (Google, Microsoft)
│   ├── security_watch.py      # Hourly security monitoring
│   └── security_alert.sh      # Alert helper
└── docs/                      # Guides and documentation

~/agent-name/                  # Each agent's workspace (created by setup)
├── CLAUDE.md                  # Agent identity, safety rules, capabilities
├── .mcp.json                  # MCP server connection
├── settings.json              # Model selection + permissions
├── startup-instructions.md    # What to do on boot
├── .config/agent-name/        # Local credential cache
├── .state/                    # Local state cache (write-through from Supabase)
├── memory/                    # Persistent memory files
│   ├── MEMORY.md              # Memory index
│   ├── *_soul.md              # Agent identity/personality
│   ├── user_*.md              # User profile
│   ├── feedback.md            # User corrections + confirmed approaches
│   └── sessions/              # Conversation summaries
└── analytics.jsonl            # Usage analytics
```

## Database Schema

The platform uses 11 Supabase tables (run `schema/create_harness_schema.sql`):

| Table | Purpose |
|-------|---------|
| `agents` | Central registry of all agents |
| `scheduled_tasks` | Persistent cron jobs (Supabase-primary, local fallback) |
| `skills` | Catalog of available capabilities |
| `skill_permissions` | Which agent can use which skill |
| `agent_sessions` | Session metadata and duration |
| `interaction_logs` | Structured conversation metadata |
| `infra_events` | Centralized infrastructure logging |
| `agent_credentials` | Scoped credential vault |
| `agent_state` | Persistent key-value state (idempotency markers, flags) |
| `agent_analytics` | Usage events and session logs |
| `admin_tasks` | Inter-agent task queue |

## MCP Tools

Agents interact with the platform through these MCP tools:

### Credential Vault
- `store_credential` — Save a credential (scoped to agent)
- `get_credential` — Retrieve a credential
- `list_credentials` — List stored credentials
- `revoke_credential` — Remove a credential

### Services
- `connect_service` — OAuth flow for Gmail, Microsoft, etc.
- `disconnect_service` — Remove a connected service

### Skills
- `my_skills` — List skills granted to this agent (user-friendly descriptions)
- `list_skills` — List skills with internal details
- `list_skill_catalog` — Browse all available skills
- `run_skill` — Execute a skill script
- `grant_skill` / `revoke_skill` — Manage permissions

### Scheduling
- `schedule_task` — Create a persistent cron job
- `list_scheduled_tasks` — View your scheduled tasks
- `update_scheduled_task` — Modify schedule or description
- `delete_scheduled_task` — Remove a task

### State (Write-Through Cache)
- `get_state` — Read a state value (Supabase-first, local fallback)
- `set_state` — Write a state value (both Supabase and local)
- `delete_state` — Remove a state key
- `list_state` — List all state keys

### Admin / Multi-Agent
- `create_admin_task` — Assign a task to another agent
- `list_pending_tasks` — Check for tasks assigned to you
- `verify_task` — Verify a task is legitimate
- `complete_task` — Mark a task as done

### Observability
- `log_interaction` — Log conversation metadata
- `log_infra_event` — Log infrastructure events
- `start_session` / `end_session` — Track session lifecycle
- `get_agent_info` — Get info about an agent
- `list_agents` — List all registered agents
- `update_agent_status` — Update agent status (active, idle, blocked, etc.)

## How Scheduling Works

1. Agents create/manage crons via MCP tools → stored in Supabase
2. One system crontab entry runs `scheduler_executor.sh` every minute
3. The executor reads due tasks from Supabase (falls back to local JSON cache)
4. Due tasks are fired into the agent's tmux session via `tmux send-keys`
5. After firing, the executor updates `last_fired_at` in both Supabase and local cache

Agents never touch crontab directly. The executor is the only bridge between the database and the agents.

## Write-Through Cache Pattern

All Supabase-backed data uses the same pattern:
- **Writes** go to both Supabase AND local files simultaneously
- **Reads** try Supabase first, fall back to local if unavailable
- This means agents work offline (local cache) and auto-sync when connectivity returns

## Connecting Services

### Telegram
1. Create a bot via @BotFather on Telegram
2. Pass the bot token to `create_agent.py --bot-token`
3. The agent's launcher starts Claude Code with the `--channels` flag

### Gmail / Google Calendar
```bash
# Start the OAuth server, then tell your agent "connect my Gmail"
python3 scripts/claude_oauth_server.py
```

### Microsoft 365
Same OAuth server supports Microsoft — agent asks user to tap the auth link.

See `docs/connecting-services.md` for full details.

## Multi-Agent Setup

Create additional agents with `create_agent.py`. Each gets:
- Isolated workspace and credentials
- Own Telegram bot
- Own scheduled tasks
- Communication via `admin_tasks` table

One agent can be designated as "admin" with supervisory access. See `docs/multi-agent.md`.

## Security

- **Credential scoping**: Each agent can only read its own credentials
- **Skill permissions**: Agents only see skills they've been granted
- **Post-build validation**: `skill_validator.py` scans for hardcoded secrets, cross-workspace access
- **Workspace isolation**: Behavioral rules in CLAUDE.md + MCP scoping
- **Security monitoring**: `security_watch.py` runs hourly via crontab
- **Double-confirm destructive actions**: Agents require two confirmations before deleting anything

## Documentation

- [Setup Guide](docs/setup-guide.md) — Detailed walkthrough
- [Memory System](docs/memory-system.md) — How agent memory works
- [Multi-Agent](docs/multi-agent.md) — Running multiple agents
- [Connecting Services](docs/connecting-services.md) — Gmail, Microsoft, iCloud, Notion
- [QMD Setup](docs/qmd-setup.md) — On-device semantic search
- [Frontend Design](skills/frontend-design/SKILL.md) — UI/UX design skill
- [Roadmap](docs/roadmap.md) — What's next

## License

MIT — see LICENSE
