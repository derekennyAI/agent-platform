# Agent Setup Skill

Creates a fully configured personal AI agent instance on this Mac.

## Usage

```
Create a new agent with these details:
- Agent name: <name> (lowercase, no spaces — used for dirs/files/daemon)
- Persona name: <persona> (the name the agent goes by with its human)
- Human name: <human> (who the agent serves)
- Telegram bot token: <token> (from @BotFather)
- Telegram user ID: <user_id> (the human's Telegram user ID)
- Timezone: <tz> (e.g., America/Los_Angeles, Europe/Lisbon)
```

## What It Creates

1. **Workspace** — `~/<agent_name>/` with CLAUDE.md, settings.json, startup-instructions.md
2. **Memory system** — `~/<agent_name>/memory/` with MEMORY.md, soul file, user profile, feedback, failed approaches, sessions/
3. **Telegram channel** — `~/.claude/channels/telegram_<agent_name>/` with .env AND access.json (both required)
4. **Launcher** — `~/<agent_name>/launcher.sh` (tmux + Claude Code)
5. **Launchd plist** — `~/Library/LaunchAgents/com.<agent_name>-agent.daemon.plist`
6. **Analytics** — usage_stats.json, analytics.jsonl, logging instructions in CLAUDE.md
7. **Registers** agent in `~/derek/skills/agent-setup/agents.json` (registry for dashboard)

## Post-Creation

- Loads the launchd plist (starts the daemon)
- Waits for tmux session to appear
- Verifies Telegram bot is reachable via API
- Reports success with connection instructions

## Model

All new agents default to `claude-sonnet-4-6`. Can be changed in the agent's settings.json.
