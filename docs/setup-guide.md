# Setup Guide

Complete walkthrough for setting up the Fleet from scratch.

## Prerequisites

- macOS (Apple Silicon recommended)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed
- Node.js 18+ (`brew install node`)
- Python 3.10+ (comes with macOS)
- tmux (`brew install tmux`)
- A [Supabase](https://supabase.com/) project
- An Anthropic API key or Claude Max subscription

## Step 1: Clone and Run Setup

```bash
git clone https://github.com/ennyai/agent-platform.git
cd fleet
./setup.sh
```

First run creates `.env` from the template. Edit it with your credentials:
- `ANTHROPIC_API_KEY` — from console.anthropic.com
- `SUPABASE_URL` — your Supabase project URL
- `SUPABASE_SERVICE_KEY` — from Supabase Settings > API > service_role key

Then run `./setup.sh` again. It will install dependencies, set up the scheduler crontab, and guide you through the rest.

## Step 2: Database Setup

Go to your Supabase dashboard > SQL Editor and run the contents of `schema/create_harness_schema.sql`.

This creates 11 tables:
- `agents` — agent registry
- `scheduled_tasks` — persistent cron jobs
- `skills` / `skill_permissions` — capability catalog and access control
- `agent_sessions` / `interaction_logs` — conversation tracking
- `infra_events` — centralized infrastructure logging
- `agent_credentials` — scoped credential vault
- `agent_state` — persistent key-value state with local fallback
- `agent_analytics` — usage events
- `admin_tasks` — inter-agent task queue

## Step 3: Create Your First Agent

```bash
python3 skills/agent-setup/create_agent.py \
  --name alex \
  --persona "Alex" \
  --human "Your Name" \
  --bot-token "123456:ABC-your-telegram-bot-token" \
  --user-id "your-telegram-user-id" \
  --timezone "America/New_York"
```

**Need a Telegram bot?** Message @BotFather on Telegram, send `/newbot`, follow prompts.
**Need your Telegram user ID?** Message @userinfobot on Telegram.

This creates:
- Agent workspace at `~/alex/`
- CLAUDE.md with the agent's identity and safety rules
- Memory system (soul file, user profile, feedback log, MEMORY.md index)
- Settings (model selection, permissions)
- Startup instructions
- MCP server connection (.mcp.json)
- Telegram channel configuration
- LaunchAgent plist for the daemon

## Step 4: Load Default Crons

```bash
python3 scripts/load_crons.py --agent alex
```

This pushes the default scheduled tasks into Supabase:
- Session memory compaction (every 30 min)
- Admin task polling (every 5 min)
- Heartbeat check (every 6 hours)
- OAuth token refresh (every 2 hours)
- QMD index refresh (every 2 hours)
- Security audit (hourly)
- Daily memory compaction (3 AM)
- Weekly memory review (Sunday 4 AM)

Preview first with `--dry-run`:
```bash
python3 scripts/load_crons.py --agent alex --dry-run
```

## Step 5: Start the Agent

```bash
launchctl load ~/Library/LaunchAgents/com.alex-agent.daemon.plist
```

Check it's running:
```bash
tmux ls                    # Should show alex-agent session
tmux attach -t alex-agent  # Watch it boot up (Ctrl-B D to detach)
```

## Step 6: Connect Services (Optional)

### Gmail / Google Calendar
Your agent can walk users through this. Or run the OAuth server manually:
```bash
python3 scripts/claude_oauth_server.py
```
Then tell the agent "connect my Gmail" — it sends the user an auth link.

### Telegram
Already configured by `create_agent.py`. The user just messages the bot.

### Microsoft 365
Same OAuth server supports Microsoft. Agent sends user the auth link when asked.

### Notion
Agent walks user through creating an internal integration at notion.so/my-integrations.

## Verifying Everything Works

1. **Agent running**: `tmux ls` shows the agent session
2. **MCP tools**: Agent responds to messages and can use `my_skills`
3. **Vault**: `store_credential` and `get_credential` work
4. **Scheduler**: Check `/tmp/scheduler.log` for executor output
5. **State**: Agent can use `get_state` / `set_state`

## Adding More Agents

Run `create_agent.py` again with a new name. Each agent gets its own workspace, credentials, and permissions — completely isolated.

```bash
python3 skills/agent-setup/create_agent.py \
  --name jordan \
  --persona "Jordan" \
  --human "Another User" \
  --bot-token "different-bot-token" \
  --user-id "their-telegram-id"

python3 scripts/load_crons.py --agent jordan
launchctl load ~/Library/LaunchAgents/com.jordan-agent.daemon.plist
```

## Stopping / Restarting Agents

```bash
# Stop
launchctl unload ~/Library/LaunchAgents/com.alex-agent.daemon.plist

# Restart
launchctl unload ~/Library/LaunchAgents/com.alex-agent.daemon.plist
launchctl load ~/Library/LaunchAgents/com.alex-agent.daemon.plist
```

## Troubleshooting

- **Agent won't start**: Check `~/alex/daemon-stderr.log`
- **MCP tools not working**: Verify `.mcp.json` has correct paths and env vars
- **Vault errors**: Ensure `SUPABASE_SERVICE_KEY` is in the launchd plist
- **Scheduler not firing**: Check `crontab -l` for the executor entry, verify `scheduler_executor.sh` is executable
- **Telegram not connecting**: Verify bot token with `curl https://api.telegram.org/bot<TOKEN>/getMe`
