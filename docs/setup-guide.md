# Setup Guide

Complete walkthrough for setting up the Agent Platform from scratch.

## Prerequisites

- macOS (Apple Silicon recommended)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- Node.js 18+ (`brew install node`)
- Python 3.10+ (comes with macOS)
- A [Supabase](https://supabase.com/) project
- An Anthropic API key or Claude Max subscription

## Step 1: Clone and Configure

```bash
git clone https://github.com/derekennyAI/agent-platform.git
cd agent-platform
cp .env.example .env
```

Edit `.env` with your credentials:
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_SERVICE_KEY` — from Supabase Settings > API > service_role key

## Step 2: Database Setup

1. Go to your Supabase dashboard > SQL Editor
2. Paste and run the contents of `schema/create_harness_schema.sql`
3. This creates: `skills`, `skill_permissions`, `agent_credentials`, `admin_tasks`

## Step 3: MCP Server

```bash
cd mcp-server
npm install
cd ..
```

The MCP server runs automatically when agents start — no separate daemon needed.

## Step 4: Create Your First Agent

```bash
source .env
python3 skills/agent-setup/create_agent.py
```

Follow the prompts. This creates:
- Agent workspace at `~/<agent-name>/`
- CLAUDE.md with the agent's identity
- .mcp.json connecting to the MCP server
- LaunchAgent plist for the daemon

## Step 5: Start the Agent

```bash
launchctl load ~/Library/LaunchAgents/com.<agent-name>.daemon.plist
```

Check it's running:
```bash
tmux attach -t <agent-name>-agent
```

## Step 6: Set Up Scheduling (Optional)

Add the system crontab entry for the scheduler:
```bash
crontab -e
# Add this line:
* * * * * /path/to/agent-platform/mcp-server/scheduler_executor.sh >> /tmp/scheduler.log 2>&1
```

Load default crons for your agent from `configs/default-crons.json`.

## Step 7: Connect Services (Optional)

### Gmail / Google Calendar
```bash
python3 skills/agent-setup/gmail_connect.py --agent <name>
```

You'll need a Google Cloud project with OAuth credentials. Follow the prompts.

### Telegram
1. Create a bot via @BotFather on Telegram
2. Install the Claude Code Telegram channels plugin
3. Configure the bot token in the agent's launcher

## Step 8: Security Monitoring (Optional)

```bash
# Add hourly security check to crontab
crontab -e
# Add:
0 * * * * python3 /path/to/agent-platform/scripts/security_watch.py
```

## Verifying Everything Works

1. **Agent running**: `tmux ls` shows the agent session
2. **MCP server**: Agent can use `my_skills` and `list_skill_catalog`
3. **Vault**: `store_credential` and `get_credential` work
4. **Scheduler**: Check `scheduler_executor.sh` logs
5. **Gmail**: Agent can read inbox via `gmail_inbox.py`

## Adding More Agents

Just run `create_agent.py` again with a new name. Each agent gets its own workspace, credentials, and permissions — completely isolated from other agents.

## Troubleshooting

- **Agent won't start**: Check `~/.claude/daemon-stderr.log`
- **MCP tools not working**: Verify `.mcp.json` has correct paths and env vars
- **Vault errors**: Ensure `SUPABASE_SERVICE_KEY` is set in the plist
- **Scheduler not firing**: Check crontab entry and `scheduler_executor.sh` permissions
