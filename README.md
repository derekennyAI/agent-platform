# Agent Platform

A complete platform for running persistent AI agents powered by Claude Code. Each agent has its own workspace, credentials vault, skills, scheduled tasks, and communication channels.

## What you get

- **Persistent agents** that run 24/7 as macOS daemons (launchd + tmux)
- **Credential vault** — OAuth tokens and API keys stored in Supabase, scoped per agent
- **Dynamic skills** — agents can use and build new capabilities on the fly
- **Workspace isolation** — each agent has its own directory, credentials, and permissions
- **Scheduled tasks** — cron-like automation (reports, monitoring, reminders)
- **Multi-channel communication** — Telegram, iMessage, email
- **Security** — post-build skill validation, workspace isolation, credential scoping

## Prerequisites

- macOS (Apple Silicon recommended, 16GB+ RAM)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- [Node.js](https://nodejs.org/) 18+
- Python 3.10+
- A [Supabase](https://supabase.com/) project (free tier works)
- An [Anthropic API key](https://console.anthropic.com/) or Claude Max subscription

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/derekennyAI/agent-platform.git
cd agent-platform
cp .env.example .env
# Edit .env with your API keys
```

### 2. Set up the database

Run the SQL in `schema/create_harness_schema.sql` in your Supabase SQL editor. This creates the tables for:
- `skills` — skill catalog
- `skill_permissions` — which agent can use which skill
- `agent_credentials` — encrypted credential vault
- `admin_tasks` — inter-agent communication

### 3. Install MCP server dependencies

```bash
cd mcp-server
npm install
cd ..
```

### 4. Create your first agent

```bash
python3 skills/agent-setup/create_agent.py
```

This will:
- Create the agent's workspace directory
- Generate CLAUDE.md with the agent's identity
- Set up .mcp.json for MCP server access
- Create the launchd plist for the daemon
- Register the agent in the database

### 5. Start the agent

```bash
launchctl load ~/Library/LaunchAgents/com.<agent-name>.daemon.plist
```

Your agent is now running. Connect it to Telegram or iMessage to start chatting.

## Architecture

```
agent-platform/           # This repo — the shared platform
├── mcp-server/           # MCP admin-control server (Node.js)
│   ├── server.js         # MCP tools: credentials, skills, scheduling
│   ├── vault_client.py   # Python vault access library
│   ├── vault_template.py # Importable module for skill scripts
│   └── skill_validator.py # Post-build security scanner
├── skills/               # Reusable skills (available to all agents)
│   ├── skill-maker/      # Meta-skill: builds new skills
│   ├── shell/            # Shell command execution + Gmail tools
│   ├── agent-setup/      # Agent creation + OAuth connection
│   ├── playwright/       # Browser automation
│   ├── diagnose/         # System diagnostics
│   └── ux-review/        # UX analysis
├── schema/               # Supabase database schema
├── configs/              # Agent config templates
├── launchers/            # Daemon plist templates
└── scripts/              # Security monitoring

~/agent-name/             # Each agent's workspace (created by setup)
├── CLAUDE.md             # Agent identity and rules
├── .mcp.json             # MCP server connection
├── .config/agent-name/   # Local credential cache
├── memory/               # Persistent memory files
└── skills/               # Agent-specific skills (if any)
```

## Key Concepts

### Credential Vault
All credentials are stored in Supabase (`agent_credentials` table), scoped by agent name. Scripts access them via `vault_client.py`:

```python
from vault_template import get_cred, get_creds
api_key = get_cred("service_name", "api_key")
```

### Workspace Isolation
Each agent runs in its own directory and can only access its own credentials. The MCP server enforces this — `get_credential` automatically scopes to the calling agent's `AGENT_NAME`.

### Skills
Skills are capabilities that agents can use. They're registered in the `skills` table and granted via `skill_permissions`. Agents discover available skills with the `my_skills` MCP tool.

### Scheduled Tasks
Agents can create recurring tasks (crons) via the MCP scheduler. The `scheduler_executor.sh` runs every minute via crontab and executes due tasks.

## Building Skills

Use the `skill-maker` skill or create manually:

1. Create a directory under `skills/`
2. Write a `SKILL.md` with a description (this is what the agent reads to decide when to use it)
3. Write scripts using the vault-aware pattern
4. Run `skill_validator.py` to check for security violations
5. Register in the database and grant to agents

See `skills/skill-maker/SKILL.md` for the full guide including credential patterns.

## Connecting Services

### Gmail / Google Calendar
```bash
python3 skills/agent-setup/gmail_connect.py --agent <name>
```
Follow the OAuth flow. Tokens are automatically stored in the vault.

### Other Services
Store credentials manually via the MCP `store_credential` tool, or use `vault_client.py` directly.

## Security

- **Credential scoping**: Each agent can only read its own credentials
- **Skill permissions**: Agents only see skills they've been granted
- **Post-build validation**: `skill_validator.py` scans for hardcoded secrets, cross-workspace access, and non-vault credential patterns
- **Workspace isolation**: Behavioral rules in CLAUDE.md + MCP scoping (true filesystem sandboxing requires Docker — see roadmap)
- **Security monitoring**: `security_watch.py` runs hourly via crontab

## License

Private — contact enny.ai for licensing.
