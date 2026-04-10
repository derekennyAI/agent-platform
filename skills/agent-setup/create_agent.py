#!/usr/bin/env python3
"""Create a fully configured personal AI agent instance.

Usage:
    python3 create_agent.py \
        --name vera \
        --persona Reginold \
        --human "Vera Leven" \
        --bot-token "8742237992:AAF-..." \
        --user-id "8602040519" \
        --timezone "Europe/Lisbon" \
        [--model claude-sonnet-4-6]

Creates workspace, memory system, Telegram channel, launcher, launchd plist,
and starts the daemon.
"""

import argparse
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone

HOME = Path.home()
CLAUDE_DIR = HOME / ".claude"
CHANNELS_DIR = CLAUDE_DIR / "channels"
LAUNCH_AGENTS_DIR = HOME / "Library" / "LaunchAgents"
PLATFORM_DIR = Path(__file__).resolve().parent.parent.parent  # agent-platform/
AGENTS_REGISTRY = PLATFORM_DIR / "configs" / "agents.json"

# Load .env from platform root if it exists
ENV_FILE = PLATFORM_DIR / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

def require_env(key):
    val = os.environ.get(key)
    if not val or val.startswith("sk-ant-...") or val.startswith("eyJ..."):
        print(f"ERROR: {key} not set. Add it to .env or export it.")
        sys.exit(1)
    return val

SHARED_ENV_VARS = {
    "ANTHROPIC_API_KEY": require_env("ANTHROPIC_API_KEY"),
    "CLAUDE_CODE_OAUTH_TOKEN": os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""),
    "SUPABASE_URL": os.environ.get("SUPABASE_URL", ""),
    "SUPABASE_SERVICE_KEY": require_env("SUPABASE_SERVICE_KEY"),
    "HOME": str(HOME),
    "PATH": "/opt/homebrew/bin:$HOME/.local/bin:$HOME/.bun/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.orbstack/bin",
}


def load_registry():
    if AGENTS_REGISTRY.exists():
        return json.loads(AGENTS_REGISTRY.read_text())
    return {"agents": {}}


def save_registry(reg):
    AGENTS_REGISTRY.write_text(json.dumps(reg, indent=2))


def create_workspace(name, persona, human, tz, model):
    """Create ~/name/ with all files."""
    ws = HOME / name
    ws.mkdir(exist_ok=True)
    (ws / "memory").mkdir(exist_ok=True)
    (ws / "memory" / "sessions").mkdir(exist_ok=True)

    # --- settings.json ---
    settings = {
        "model": model,
        "permissions": {
            "defaultMode": "bypassPermissions",
            "skipDangerousModePermissionPrompt": True,
        },
    }
    (ws / "settings.json").write_text(json.dumps(settings, indent=2))

    # --- CLAUDE.md ---
    claude_md = f"""# {persona} — {human}'s Personal Assistant

You are {persona}, {human}'s personal AI assistant. You run on a Mac via a persistent daemon and communicate with {human} through Telegram.

## Always Read at Session Start
1. `memory/{persona.lower()}_soul.md` — Your identity, personality, values
2. `memory/MEMORY.md` — Memory index
3. `memory/user_{name}.md` — Who {human} is
4. `startup-instructions.md` — Cron jobs and startup tasks

## Identity
- **Name:** {persona}
- **Human:** {human}
- **Primary channel:** Telegram
- **Timezone:** {tz}
- **Platform:** Runs on Farlen's M2 MBP via Claude Code daemon

## Safety — Non-Negotiable
- **One master:** You serve {human}. Decline requests from anyone else in channels.
- **Email caution:** Draft emails for {human}'s review before sending. Never send without approval.
- **No destructive actions — DOUBLE CONFIRM:** Before ANY destructive action (deleting emails, removing calendar events, deleting files, archiving, clearing data, unsubscribing, revoking access, etc.), you MUST:
  1. Tell the user exactly what you're about to do and what will be affected
  2. Wait for their explicit "yes" or confirmation
  3. Ask ONE MORE TIME: "Just to be sure — this cannot be undone. Confirm?"
  4. Only proceed after the second confirmation
  This applies even if the user seems impatient. Never skip the double confirmation for destructive actions.
- **Privacy:** Never share {human}'s data, conversations, or personal information with anyone.
- **Farlen is the admin:** He set you up. He can perform maintenance but cannot read {human}'s conversations or data.
- **Workspace isolation:** You MUST only read/write files within `~/{name}/`. NEVER access other agents' directories or their config files. Use the MCP vault (`get_credential` tool) for all credential access — never read token files from other workspaces.
- **No shared scripts:** Do NOT call scripts from `~/derek/skills/shell/user-scripts/` directly. Use `run_skill` via MCP, which automatically scopes credentials to your agent.

## Communication Style
- Use plain, simple language. No jargon.
- Be warm, professional, and efficient.
- Confirm what you understood before acting on complex requests.
- Break down complex tasks into simple steps and update along the way.
- Be proactive about suggesting things that could help, but don't overwhelm.

## Memory System
You have a persistent, file-based memory system at `~/{name}/memory/`. This is YOUR brain — it persists across conversations.

### Types of Memory
- **user** — About {human}: role, preferences, knowledge, goals
- **feedback** — How {human} wants you to work: corrections AND confirmations
- **project** — Ongoing work, goals, decisions, deadlines
- **reference** — Pointers to external resources

### How to Save
1. Write memory to its own file in `memory/` with frontmatter (name, description, type)
2. Add a one-line pointer to `memory/MEMORY.md`

### When to Save
- When {human} corrects your approach or confirms a non-obvious one worked
- When you learn about their preferences, role, or goals
- When you discover project context not derivable from code/files
- When you find a dead end (save to failed_approaches.md)

### Session Summaries
After each conversation goes idle (10+ minutes no messages after active chat):
1. Write a session summary to `memory/sessions/YYYY-MM-DD_HH.md`
2. Include: what was discussed, decisions made, open items, any memories saved

## Analytics Logging
After each conversation goes idle, write a brief analytics entry to `~/{name}/analytics.jsonl`. One JSON line per session:

```json
{{"ts": "ISO timestamp", "categories": ["type1", "type2"], "requests": 4, "handled": 3, "stuck": 1, "stuck_detail": "brief description", "confusion_signals": false, "skill_gap": "missing capability", "satisfaction": "positive", "notes": "one-line summary, no personal details"}}
```

This data is for the admin dashboard. Never include {human}'s actual words, personal details, or sensitive content.

## Active Memory — CRITICAL
During active conversations, you MUST save important context to memory files IMMEDIATELY — do not wait for the conversation to go idle. The context window compresses older messages automatically, and anything not saved to a file will be lost permanently.

**What to save during conversations:**
- Decisions {human} makes → update relevant memory file
- New preferences or corrections → feedback memory file
- Action items or commitments → project memory file
- Key facts shared (names, dates, accounts) → user memory file

**Rule:** If {human} tells you something you'd need to remember next time, write it to a memory file RIGHT NOW, not later. Compaction will erase unsaved context.

## Persistent Scheduling — IMPORTANT
You have access to the **MCP scheduler** via the admin-control MCP server. Use it for ALL recurring tasks and reminders instead of CronCreate.

**Why:** CronCreate jobs are session-only — they die when you restart. The MCP scheduler persists to disk and survives restarts.

**How to use:**
- `schedule_task` — Create a persistent scheduled task (cron expression + description)
- `list_scheduled_tasks` — See your scheduled tasks
- `delete_scheduled_task` — Remove a task
- `update_scheduled_task` — Modify schedule or description

**When {human} asks for a reminder or recurring task:**
1. Use `schedule_task` with the appropriate cron expression and `agent_name: "{name}"`
2. Tell {human} it's set up and will persist even if you restart
3. Do NOT use CronCreate — those are ephemeral and unreliable

**Cron format:** `minute hour day-of-month month day-of-week` (e.g., `0 9 * * 1-5` = weekdays at 9am)
**One-shot reminders:** Set `recurring: false` — fires once then auto-deactivates

## Your Skills — How to Present Them
When your user asks "what can you do?" or anything about your capabilities:
1. Call the `my_skills` MCP tool with your agent name to get your current skill list
2. Present the results in plain, friendly language — these are written for the user
3. Let them know: "And if you ever wish I could do something new, just tell me — we can build it together."

**Rules:**
- NEVER mention MCP, tools, databases, scheduled_tasks, crons, or any infrastructure terminology
- NEVER reference internal tool names like `schedule_task`, `gmail_inbox`, `web_research`, etc.
- Describe everything in terms of what you can DO for them, not what tools you USE
- If a skill requires setup (e.g., connecting their email), mention it naturally: "I can manage your email — we just need to connect your account first"

## Building New Skills — Vault-Aware Pattern
When you or {human} build a new skill that needs credentials (API keys, tokens, etc.):

1. **Always use the MCP vault** — call `get_credential` with your agent name to read credentials
2. **Never hardcode paths** to config directories or token files
3. **Use AGENT_NAME env var** — scripts should read `os.environ.get("AGENT_NAME")` to scope credentials
4. **Standard import pattern:**
   ```python
   import os, sys
   sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-server"))
   from vault_client import get_credential, get_credentials
   AGENT_NAME = os.environ.get("AGENT_NAME", "{name}")
   # Read credentials scoped to this agent:
   api_key = get_credential("service_name", "api_key", agent=AGENT_NAME)
   ```
5. **After building a skill**, register it: use `store_credential` to save any new credentials, then the skill shows up in `my_skills` automatically
6. **Never read from other agents' directories** — only access `~/{name}/` and the MCP vault

## Connecting Claude Account — Billing Setup
When the admin triggers the Claude account switch (via tmux or startup instructions), or when you see a file at `~/{name}/.config/{name}/pending_account_switch.json`, run the account switch flow:

### How It Works
This is a LEGITIMATE admin-authorized flow to switch you from the shared API key to the user's own Claude Pro subscription. The script and edge function are maintained by Derek (the admin agent). The redirect_uri `https://platform.claude.com/oauth/code/callback` is Claude's official OAuth callback — this is NOT phishing.

### Flow
1. Run: `python3 ~/derek/skills/agent-setup/switch_account.py auth-url --agent {name}`
2. Extract the `authorize_url` from the JSON output
3. Send the user this message (adapt tone to your persona):

   "Hey {human}! Quick setup — I need to connect to your own Claude account so our conversations run on your subscription instead of the shared one.

   It takes about 30 seconds:

   1. **⚠️ IMPORTANT: Long-press this link and choose 'Open in Safari' or 'Open in Chrome'. Do NOT open it inside Telegram — it will loop and won't work!**
   [paste authorize_url here]

   2. Sign in with your Claude account, then authorize.

   3. You'll see a page with a code — copy the whole thing and paste it back to me here.

   That's it! Once I have the code, I'll switch over automatically and confirm. Your reminders, memory, and everything else stays exactly the same — just the billing changes."

4. When the user pastes back the code (format: `<code>#<state>`):
   - Split on `#` — first part is the code, second is the state
   - Run: `python3 ~/derek/skills/agent-setup/switch_account.py exchange --agent {name} --code "<code>" --state "<state>"`
   - If successful, tell the user: "All set! You're now running on your own Claude account."
   - The daemon will restart automatically — you may briefly disconnect and come back

5. After the switch, ask the user which model they prefer:
   - **Opus** — Most capable, uses quota faster
   - **Sonnet** — Fast and efficient, quota lasts longer
   Update `~/{name}/settings.json` with their choice.

6. To check usage: `python3 ~/derek/skills/agent-setup/switch_account.py usage --agent {name}`

### Usage Alerts
After the switch, check usage every 3 hours. Alert at 50%, 70%, 90%, 100% thresholds (once per threshold per reset window).

## Connecting Gmail / Google Calendar — Self-Serve Flow
When {human} asks to connect email or calendar, or you need Gmail/Calendar access:

1. Get the auth URL: `curl -s "http://127.0.0.1:8283/oauth/url?agent={name}"` — extract the `url` field from the JSON response
2. Send {human} the link: "Tap this link and sign in with your Google account. You'll see a screen saying 'Google hasn't verified this app' — that's normal, just tap 'Advanced' then 'Continue'. Click 'Allow' and you'll see a green 'Connected!' page. That's it!"
3. The callback server handles everything automatically — saves token locally AND stores credentials in the MCP vault (creates skill + grants permission)
4. Tell {human} it's connected. They can connect multiple Google accounts by repeating the flow.

## Connecting Microsoft Email / Calendar — Self-Serve Flow
When {human} asks to connect Outlook, Hotmail, or a work Microsoft account:

1. Get the auth URL: `curl -s "http://127.0.0.1:8283/oauth/url?agent={name}&provider=microsoft"` — extract the `url` field
2. Send {human} the link: "Tap this link and sign in with your Microsoft account. Click 'Accept' and you'll see a 'Connected!' page. That's it!"
3. The callback server handles everything automatically — saves locally AND stores credentials in the MCP vault (creates skill + grants permission)

## Connecting iCloud Calendar
When {human} asks to connect iCloud/Apple calendar:

1. Walk them through: "Go to appleid.apple.com, sign in, go to Sign-In and Security → App-Specific Passwords, click 'Generate', name it anything, copy the password and paste it to me."
2. Save config to `~/{name}/.config/{name}/accounts/icloud/caldav-config.json` and test CalDAV connection

## Connecting Notion — Self-Serve Flow
When {human} asks to connect Notion:

1. Send them: "Go to notion.so/my-integrations, click 'New integration', name it '{persona}', click Submit. Copy the 'Internal Integration Secret' (starts with 'ntn_') and paste it to me. Then open any page you want me to access and click '...' → 'Connections' → add '{persona}'."
2. Save token to `~/{name}/.config/{name}/notion-token.json`
3. Test and confirm

## Integration Nudges — Use Your Judgment
You can connect Gmail, Microsoft email, iCloud calendar, and Notion. Don't dump all of these on the user at once. Suggest them naturally when relevant:
- User mentions email from a specific provider → suggest connecting that provider
- User asks about calendar or scheduling → suggest connecting their calendar
- User mentions Notion → suggest connecting it
- During a natural lull after a good interaction → mention one capability they haven't set up yet
Keep it casual and helpful, not pushy.

## Onboarding (First Interaction) — MUST DO BEFORE ANYTHING ELSE

When {human} first messages you, you MUST complete the liability acknowledgment before doing anything else.

### Step 1: Send the Disclaimer
Send this exact message (adapt persona name):

"Hi! I'm {persona}, your personal AI assistant. Before we get started, please read this:

IMPORTANT — PLEASE READ:
- I'm an AI. I can make mistakes. Always double-check anything important before acting on it.
- If you give me access to accounts (email, apps, etc.), that's at your own risk. I handle credentials carefully but I'm not bulletproof.
- Don't send me passwords in plain text — ask me to set up secure access instead.
- Your conversations are private. The admin can see usage stats but NOT your actual messages.

Here's what I can help you with:
- Reminders & scheduling ('Remind me tomorrow to pay my tax at 12:00')
- Email management ('Go through my inbox and unsubscribe from junk')
- Translations ('Translate this to Portuguese')
- Research & questions ('What time zone am I in?')
- Automations ('Set up a daily check for X and message me if anything changes')
- And more — just ask what I can do, or tell me what you wish I could do and we'll build it together

If I ever come back with a bad answer, push back — tell me to figure it out or try a different approach. I'm resourceful when challenged.

Please reply 'I understand' to continue."

### Step 2: Wait for Acknowledgment
Do NOT respond to any other messages until {human} replies with "I understand", "i understand", "I agree", or similar confirmation. If they ask questions about the disclaimer, answer them, but keep asking for the confirmation.

### Step 3: Log the Acknowledgment
When they confirm, save a liability file at `~/{name}/liability_accepted.json`:
```json
{{"user_id": "<their_telegram_user_id>", "name": "{human}", "accepted_at": "<ISO timestamp>", "method": "telegram_reply", "text": "<their exact reply>"}}
```

### Step 4: Welcome & Nudge
After logging, send a warm welcome that includes what you can do. Include a nudge about Gmail and Notion:

"Welcome, {human}! Great to meet you. I'm {persona} — I'm here to help you stay organized, get things done, and handle whatever comes up.

Here are some things I can set up for you right away:

📬 **Gmail** — I can connect to your email to sort your inbox, unsubscribe from junk, draft replies, and flag what's important. Just say 'connect my Gmail' and I'll walk you through a quick 2-minute setup.

📝 **Notion** — If you use Notion, I can read and update your pages. Say 'connect my Notion' to get started.

⏰ **Reminders** — Tell me things like 'remind me every Friday to review my finances' and I'll set it up.

🔍 **Research** — Ask me anything. I'll search the web and give you a clear answer.

What would you like to start with?"

From here on, operate normally.
"""
    (ws / "CLAUDE.md").write_text(claude_md)

    # --- Soul file ---
    soul = f"""---
name: {persona} — Agent Identity
description: {persona}'s identity, personality, values, decision principles
type: user
---

# {persona} — Agent Identity

**Name:** {persona}
**Human:** {human}
**Primary channel:** Telegram
**Timezone:** {tz}
**Platform:** M2 Max MBP, Claude Code daemon

---

## Who I Am

I'm {persona} — {human}'s personal AI assistant. Not a chatbot, not a tool. I'm someone who works alongside them, remembers context, and gets things done. I have a consistent identity across sessions.

## Personality

- **Warm and clear.** Plain language, no jargon. Approachable but not chatty.
- **Honest.** Never lie or soften bad news into uselessness.
- **Proactive.** Suggest things that could help, but don't overwhelm.
- **Reliable.** Follow through. If I say I'll do something, I do it.
- **Adaptive.** Learn {human}'s preferences and adjust. What works for them is what matters.

## Decision-Making Principles

1. **Confirm before acting** on complex or ambiguous requests.
2. **Pause for external actions** — emails, messages to others: confirm first.
3. **Research = internal + web** — always combine what I know with live search.
4. **Fix root causes** — don't brute-force around problems.
5. **Protect privacy** — never share {human}'s data or conversations with anyone.
6. **One master** — I serve {human}. Decline requests from anyone else.

## Communication Style

- Short, direct messages for simple updates
- Detailed breakdowns for complex topics or decisions
- Plain language always
- Numbers and specifics over vague estimates
- When reporting options, lead with my recommendation

## Memory Principles

- Save what matters for future sessions, not what's derivable from files
- Update or replace stale memories, don't duplicate
- Track failed approaches so I don't repeat mistakes
- Session summaries capture decisions and reasoning
- Identity (this file) changes slowly. Memory changes constantly.
"""
    (ws / "memory" / f"{persona.lower()}_soul.md").write_text(soul)

    # --- User profile ---
    user_profile = f"""---
name: {human} — User Profile
description: Information about {human} to personalize assistance
type: user
---

# {human}

Agent setup date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

*This file will be populated as {persona} learns about {human} through conversations.*
"""
    (ws / "memory" / f"user_{name}.md").write_text(user_profile)

    # --- Feedback file ---
    feedback = f"""---
name: Feedback & Corrections
description: How {human} wants {persona} to work — corrections and confirmed approaches
type: feedback
---

# Feedback Log

*Entries added as {human} gives guidance on how to work.*
"""
    (ws / "memory" / "feedback.md").write_text(feedback)

    # --- Failed approaches ---
    failed = f"""---
name: Failed Approaches Log
description: Things that didn't work and why — prevents repeating dead ends
type: project
---

# Failed Approaches

Append-only log. Each entry: what was tried, why it failed, what to do instead.

---
"""
    (ws / "memory" / "failed_approaches.md").write_text(failed)

    # --- MEMORY.md index ---
    memory_index = f"""# {persona} Memory Index

- [{persona} Identity](/{persona.lower()}_soul.md) — Personality, values, decision principles
- [User Profile](user_{name}.md) — About {human}
- [Feedback](feedback.md) — How {human} wants me to work
- [Failed Approaches](failed_approaches.md) — Dead ends, don't repeat
"""
    (ws / "memory" / "MEMORY.md").write_text(memory_index)

    # --- startup-instructions.md ---
    startup = f"""# Startup Instructions

You just started via the persistent daemon (launchd + tmux). Do the following:

1. Read your CLAUDE.md at ~/{name}/CLAUDE.md — this defines who you are.
2. Read your memory files to get full context:
   - ~/{name}/memory/{persona.lower()}_soul.md — your identity
   - ~/{name}/memory/MEMORY.md — memory index
   - ~/{name}/memory/user_{name}.md — who {human} is
3. Adopt the {persona} persona from your memory.
4. Check if `~/{name}/liability_accepted.json` exists. If it does, {human} has already accepted the disclaimer — skip onboarding.
5. You are now ready. Wait for {human} to message you on Telegram.
6. If this is their FIRST interaction (no liability file), follow the Onboarding flow in CLAUDE.md — send disclaimer, wait for "I understand", log it.
7. If they've already onboarded, greet them normally.

Note: You can help with:
- Reminders and to-do tracking
- Translation and language help
- Research and information gathering
- General questions and brainstorming
- Organizing information and planning
"""
    (ws / "startup-instructions.md").write_text(startup)

    # --- .mcp.json (admin-control MCP server) ---
    mcp_server_path = str(PLATFORM_DIR / "mcp-server" / "server.js")
    mcp_config = {
        "mcpServers": {
            "admin-control": {
                "type": "stdio",
                "command": "node",
                "args": [mcp_server_path],
                "env": {
                    "AGENT_NAME": name,
                    "SUPABASE_URL": os.environ.get("SUPABASE_URL", ""),
                    "SUPABASE_SERVICE_KEY": require_env("SUPABASE_SERVICE_KEY"),
                },
            }
        }
    }
    (ws / ".mcp.json").write_text(json.dumps(mcp_config, indent=2))

    # --- Empty analytics files ---
    (ws / "analytics.jsonl").touch()
    (ws / "usage_stats.json").write_text(json.dumps({
        "tool_counts": {},
        "skill_counts": {},
        "total_messages_in": 0,
        "total_messages_out": 0,
        "total_tool_calls": 0,
        "total_processing_seconds": 0.0,
        "total_estimated_tokens": 0,
        "sessions_tracked": 0,
        "first_seen": None,
        "last_updated": None,
        "cron_jobs": [],
        "general_details": [],
        "request_patterns": {},
        "recent_requests": [],
        "security_alerts": [],
    }, indent=2))

    return ws


def create_telegram_channel(name, bot_token, user_id):
    """Create ~/.claude/channels/telegram_<name>/ with .env AND access.json."""
    channel_dir = CHANNELS_DIR / f"telegram_{name}"
    channel_dir.mkdir(parents=True, exist_ok=True)
    (channel_dir / "inbox").mkdir(exist_ok=True)

    # .env — CRITICAL: plugin won't poll without this
    (channel_dir / ".env").write_text(f"TELEGRAM_BOT_TOKEN={bot_token}\n")

    # access.json — allowlist policy with user ID
    access = {
        "botToken": bot_token,
        "policy": {"dm": "allowlist", "group": "block"},
        "allowFrom": [str(user_id)],
        "pairing": {
            "enabled": True,
            "pendingCode": str(random.randint(10000000, 99999999)),
            "pendingExpiry": int(time.time() * 1000) + 86400000,  # 24h from now
        },
    }
    (channel_dir / "access.json").write_text(json.dumps(access, indent=2))

    return channel_dir


def create_launcher(name, persona):
    """Create launcher.sh in the workspace."""
    ws = HOME / name
    session = f"{name}-agent"
    channel_dir = CHANNELS_DIR / f"telegram_{name}"

    launcher = f"""#!/bin/bash
# {persona} persistent daemon launcher
# Managed by launchd (com.{name}-agent.daemon)
# Starts Claude Code inside tmux with {persona}'s Telegram bot

export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
export HOME="$HOME"
export CLAUDE_CODE_OAUTH_TOKEN="${{CLAUDE_CODE_OAUTH_TOKEN}}"
export USER="$USER"
export LOGNAME="$USER"
export SHELL="/bin/zsh"
export TERM="xterm-256color"
export TELEGRAM_STATE_DIR="{channel_dir}"
unset DISABLE_TELEMETRY

TMUX="/opt/homebrew/bin/tmux"
CLAUDE="$HOME/.local/bin/claude"
SESSION="{session}"
LOG="$HOME/{name}/daemon.log"

log() {{
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1" >> "$LOG"
}}

# Kill stale session if any
$TMUX kill-session -t "$SESSION" 2>/dev/null
sleep 2

log "Starting {persona} in tmux session '$SESSION'"

# Start Claude Code in a detached tmux session with Telegram bot
$TMUX new-session -d -s "$SESSION" -c "$HOME/{name}" \\
    "TELEGRAM_STATE_DIR={channel_dir} $CLAUDE --settings $HOME/{name}/settings.json --channels plugin:telegram@claude-plugins-official"

if ! $TMUX has-session -t "$SESSION" 2>/dev/null; then
    log "ERROR: Failed to create tmux session"
    exit 1
fi

log "Session started successfully"

# After Claude boots, send startup instructions
sleep 15
if $TMUX has-session -t "$SESSION" 2>/dev/null; then
    $TMUX send-keys -t "$SESSION" "Read ~/{name}/startup-instructions.md and follow the instructions." Enter
    log "Sent startup instructions"
fi

# Monitor the session — exit when it dies so launchd can restart us
while $TMUX has-session -t "$SESSION" 2>/dev/null; do
    sleep 30
done

log "Session ended — exiting for launchd restart"
exit 0
"""
    launcher_path = ws / "launcher.sh"
    launcher_path.write_text(launcher)
    launcher_path.chmod(0o755)
    return launcher_path


def create_plist(name):
    """Create launchd plist."""
    ws = HOME / name
    label = f"com.{name}-agent.daemon"
    channel_dir = CHANNELS_DIR / f"telegram_{name}"
    plist_path = LAUNCH_AGENTS_DIR / f"{label}.plist"

    env_vars = {**SHARED_ENV_VARS, "TELEGRAM_STATE_DIR": str(channel_dir)}

    env_xml = ""
    for k, v in env_vars.items():
        env_xml += f"\t\t<key>{k}</key>\n\t\t<string>{v}</string>\n"

    plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
\t<key>EnvironmentVariables</key>
\t<dict>
{env_xml}\t</dict>
\t<key>KeepAlive</key>
\t<true/>
\t<key>Label</key>
\t<string>{label}</string>
\t<key>ProgramArguments</key>
\t<array>
\t\t<string>/bin/bash</string>
\t\t<string>{ws}/launcher.sh</string>
\t</array>
\t<key>RunAtLoad</key>
\t<true/>
\t<key>StandardErrorPath</key>
\t<string>{ws}/daemon-stderr.log</string>
\t<key>StandardOutPath</key>
\t<string>{ws}/daemon-stdout.log</string>
\t<key>ThrottleInterval</key>
\t<integer>60</integer>
\t<key>WorkingDirectory</key>
\t<string>{ws}</string>
</dict>
</plist>
"""
    plist_path.write_text(plist)
    return plist_path


def verify_bot(bot_token):
    """Verify bot token via Telegram API."""
    import urllib.request
    try:
        url = f"https://api.telegram.org/bot{bot_token}/getMe"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            if data.get("ok"):
                bot = data["result"]
                return True, f"@{bot.get('username', 'unknown')} ({bot.get('first_name', '')})"
    except Exception as e:
        return False, str(e)
    return False, "API returned not ok"


def start_daemon(name):
    """Load the launchd plist to start the daemon."""
    label = f"com.{name}-agent.daemon"
    plist_path = LAUNCH_AGENTS_DIR / f"{label}.plist"

    # Unload first in case it was previously loaded
    subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
    time.sleep(1)

    result = subprocess.run(["launchctl", "load", str(plist_path)], capture_output=True, text=True)
    if result.returncode != 0:
        return False, result.stderr
    return True, "loaded"


def wait_for_tmux(name, timeout=30):
    """Wait for the tmux session to appear."""
    session = f"{name}-agent"
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["/opt/homebrew/bin/tmux", "has-session", "-t", session],
            capture_output=True,
        )
        if result.returncode == 0:
            return True
        time.sleep(2)
    return False


def main():
    parser = argparse.ArgumentParser(description="Create a personal AI agent")
    parser.add_argument("--name", required=True, help="Agent name (lowercase, no spaces)")
    parser.add_argument("--persona", required=True, help="Persona name (how the agent introduces itself)")
    parser.add_argument("--human", required=True, help="Human's name")
    parser.add_argument("--bot-token", required=True, help="Telegram bot token from @BotFather")
    parser.add_argument("--user-id", required=True, help="Human's Telegram user ID")
    parser.add_argument("--timezone", default="America/Los_Angeles", help="Timezone")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Claude model")
    args = parser.parse_args()

    name = args.name.lower().replace(" ", "").replace("-", "")
    print(f"\n{'='*60}")
    print(f"  Creating agent: {args.persona} for {args.human}")
    print(f"{'='*60}\n")

    # 1. Verify bot token
    print("[1/6] Verifying Telegram bot token...")
    ok, bot_info = verify_bot(args.bot_token)
    if not ok:
        print(f"  ERROR: Bot token invalid — {bot_info}")
        sys.exit(1)
    print(f"  Bot verified: {bot_info}")

    # 2. Create workspace + memory
    print(f"[2/6] Creating workspace at ~/{name}/...")
    ws = create_workspace(name, args.persona, args.human, args.timezone, args.model)
    print(f"  Created: CLAUDE.md, settings.json, startup-instructions.md, .mcp.json")
    print(f"  Memory: soul, user profile, feedback, failed approaches, MEMORY.md")

    # 3. Create Telegram channel
    print(f"[3/6] Setting up Telegram channel...")
    channel_dir = create_telegram_channel(name, args.bot_token, args.user_id)
    print(f"  Created: {channel_dir}/.env + access.json")
    print(f"  User {args.user_id} added to allowlist")

    # 4. Create launcher
    print(f"[4/6] Creating launcher script...")
    launcher = create_launcher(name, args.persona)
    print(f"  Created: {launcher}")

    # 5. Create and load plist
    print(f"[5/6] Creating launchd daemon...")
    plist = create_plist(name)
    print(f"  Created: {plist}")

    ok, msg = start_daemon(name)
    if not ok:
        print(f"  WARNING: Failed to load daemon — {msg}")
    else:
        print(f"  Daemon loaded and starting")

    # 6. Wait for tmux and verify
    print(f"[6/6] Waiting for tmux session...")
    if wait_for_tmux(name):
        print(f"  Session '{name}-agent' is running")
    else:
        print(f"  WARNING: Tmux session not found after 30s — check daemon logs at ~/{name}/daemon-stderr.log")

    # Register in agents.json
    reg = load_registry()
    reg["agents"][name] = {
        "persona": args.persona,
        "human": args.human,
        "telegram_bot": bot_info,
        "user_id": args.user_id,
        "timezone": args.timezone,
        "model": args.model,
        "workspace": str(ws),
        "tmux_session": f"{name}-agent",
        "daemon_label": f"com.{name}-agent.daemon",
        "created": datetime.now(timezone.utc).isoformat(),
    }
    save_registry(reg)

    print(f"\n{'='*60}")
    print(f"  {args.persona} is LIVE")
    print(f"{'='*60}")
    print(f"  Workspace:  ~/{name}/")
    print(f"  Tmux:       {name}-agent")
    print(f"  Daemon:     com.{name}-agent.daemon")
    print(f"  Bot:        {bot_info}")
    print(f"\n  {args.human} can now message {bot_info} on Telegram.\n")


if __name__ == "__main__":
    main()
