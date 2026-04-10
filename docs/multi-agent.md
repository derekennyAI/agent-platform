# Multi-Agent Coordination

How to run multiple agents that communicate with each other.

## Architecture

Each agent runs as an independent Claude Code daemon with its own:
- Workspace (`~/agent-name/`)
- Credentials (scoped via vault)
- MCP server connection
- Telegram/iMessage channel (optional)
- Scheduled tasks

Agents coordinate through **admin tasks** — a Supabase-backed message queue.

## Creating Additional Agents

```bash
source .env
python3 skills/agent-setup/create_agent.py
```

Each run creates a fully isolated agent. You can create as many as you need.

## Agent Communication via Admin Tasks

The MCP server provides these tools for inter-agent coordination:

### `create_admin_task`
Send a task to another agent. Only agents designated as "admin" can create tasks.

```
Agent: derek
Task: "vera, please generate this week's analytics report and send it to Farlen"
Target: vera
```

### `list_pending_tasks`
Each agent polls for tasks assigned to it (default: every 5 minutes via cron).

### `complete_admin_task`
Agent marks a task as done after completing it.

### `verify_admin_task`
Agent can verify that a task is legitimate (came from an authorized admin agent).

## Database Schema

The `admin_tasks` table in Supabase:

| Column | Type | Description |
|--------|------|-------------|
| id | uuid | Primary key |
| agent_name | text | Target agent |
| created_by | text | Admin agent that created the task |
| task_description | text | What to do |
| status | text | pending / in_progress / completed / failed |
| result | text | Task output (filled by target agent) |
| created_at | timestamptz | When created |
| completed_at | timestamptz | When finished |

## Agent Permissions

The `skill_permissions` table controls which skills each agent can use:

| Column | Type | Description |
|--------|------|-------------|
| agent_name | text | Agent identifier |
| skill_name | text | Skill they can access |
| granted_by | text | Who approved it |
| granted_at | timestamptz | When approved |

## Admin vs Regular Agents

**Admin agents** (like the primary "derek" agent) can:
- Create tasks for other agents
- Grant skill permissions
- Access the admin MCP tools
- Manage credentials for other agents

**Regular agents** can:
- Poll and complete tasks assigned to them
- Use skills they've been granted access to
- Store/retrieve their own credentials
- Manage their own memory and scheduling

Configure admin status in `configs/agents.json`:

```json
{
  "agents": {
    "derek": {
      "workspace": "~/derek",
      "persona": "Derek",
      "human": "Derek",
      "admin": true
    },
    "vera": {
      "workspace": "~/vera",
      "persona": "Vera",
      "human": "Vera",
      "admin": false
    }
  }
}
```

## Claude Subscription Setup

Each agent needs a Claude subscription (Pro or Max) to run. Use the OAuth server to connect a user's subscription:

```bash
python3 scripts/claude_oauth_server.py --agent vera --port 8285
```

This serves a web page where the user can sign in and authorize the agent. The token is saved to the agent's config directory automatically.

## Monitoring

All agents share the same scheduler. The heartbeat cron (every 6 hours) checks each agent's health. If an agent is down, the admin agent is notified.

For centralized monitoring, check the `agent_analytics` table in Supabase (Phase 4 of the roadmap).
