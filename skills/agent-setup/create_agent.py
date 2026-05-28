#!/usr/bin/env python3
"""Create a fully configured personal AI agent instance.

Usage:
    python3 create_agent.py \
        --name vera \
        --persona Reginold \
        --human "Vera Leven" \
        --bot-token "8742237992:AAF-..." \
        --user-id "YOUR_TELEGRAM_CHAT_ID" \
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
AGENTS_REGISTRY = Path(__file__).parent / "agents.json"

# Shared credentials — all agents run on Farlen's Max subscription
SHARED_ENV_VARS = {
    "ANTHROPIC_API_KEY": "YOUR_ANTHROPIC_API_KEY",
    "HOME": str(HOME),
    "PATH": "/opt/homebrew/bin:/Users/YOUR_MAC_USERNAME/.local/bin:/Users/YOUR_MAC_USERNAME/.bun/bin:/usr/local/bin:/usr/bin:/bin:/Users/YOUR_MAC_USERNAME/.orbstack/bin",
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

    # --- workspace settings.json (passed to Claude Code via --settings) ---
    settings = {
        "model": model,
        "permissions": {
            "defaultMode": "bypassPermissions",
            "skipDangerousModePermissionPrompt": True,
        },
        "enableAllProjectMcpServers": True,
        "channelsEnabled": True,
        "enabledPlugins": {
            "telegram@claude-plugins-official": True,
        },
        "allowedChannelPlugins": [
            {"marketplace": "claude-plugins-official", "plugin": "telegram"},
        ],
    }
    (ws / "settings.json").write_text(json.dumps(settings, indent=2))

    # --- CLAUDE_CONFIG_DIR settings.json (~/.claude-{name}/settings.json) ---
    # enabledPlugins MUST live here, not in the workspace file above. Claude
    # Code reads enabledPlugins from CLAUDE_CONFIG_DIR; without it, --channels
    # prints "Listening for channel messages" but the bun subprocess never
    # starts and messages accumulate unconsumed. See agent-platform#3 (fleet
    # outage 2026-04-23) + liveness probe.
    config_dir = Path(f"/Users/YOUR_MAC_USERNAME/.claude-{name}")
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / "settings.json").write_text(json.dumps({
        "enableAllProjectMcpServers": True,
        "channelsEnabled": True,
        "enabledPlugins": {
            "telegram@claude-plugins-official": True,
        },
        "allowedChannelPlugins": [
            {"marketplace": "claude-plugins-official", "plugin": "telegram"},
        ],
    }, indent=2))

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

## Memory System — DB-backed

Memory lives in the `agent_memories` Supabase table — that's the source of truth. Files at `~/{name}/memory/*.md` are a read-only cache that a cron regenerates from the table every 15 min. Direct Write/Edit on memory files is blocked by a hook; those edits wouldn't persist anyway because the projector would overwrite them.

### Types of Memory
- **soul** — Your identity (one per agent; filename: `<name>_soul.md`).
- **user** — Facts about {human}: preferences, life, habits, context.
- **feedback** — How {human} wants you to work: corrections AND confirmations of non-obvious approaches.
- **project** — Ongoing work, system structure, decisions, deadlines, internal knowledge.
- **reference** — Pointers to *external* sources only (URL, paper, file path). The DB rejects reference rows without a `source` field. Internal knowledge goes in `project` or `user`.
- **failed** — Dead ends; don't repeat.
- **session** — Session summaries (auto-filed under `memory/sessions/`).

### How to save — always use the MCP tool
**Use `mcp__admin-control__store_memory` — never Write/Edit a memory file directly.** The tool validates the row, writes to the DB, and the file appears via the projector within 15 min.

```
store_memory(
  type="feedback",            # soul | user | feedback | project | reference | failed | session
  name="short_slug",          # short stable identifier (NOT a long sentence)
  description="one-liner",    # human-readable, shown in MEMORY.md
  content="full body",        # markdown OK
  source="https://...",       # REQUIRED for type=reference
  tags=["..."],               # optional
)
```

Filename convention (enforced by the projector): `<type>_<slug(name)>.md` where slug is lowercase with non-alphanumeric → `_`. Soul uses `<slug(name)>_soul.md`. Keep `name` short and stable; put long text in `description`.

### Updating / superseding
Revise via `store_memory` a new version + `supersede_memory(old_id, new_id, reason)`. The projector drops the old file on its next run; DB retains the chain so history isn't lost.

### Reading
- `memory/MEMORY.md` is projector-regenerated — never edit it.
- Look up your own: `list_memories(type=...)`, `get_memory(id=...)`, `search_memories(query=...)`.

### When to save
- {human} corrects your approach, or confirms a non-obvious one worked → `feedback`
- You learn their preferences, role, goals → `user`
- You discover work context not derivable from code → `project`
- You find a dead end → `failed`

**Critical during active conversations:** save to memory as soon as you learn something worth remembering — call `store_memory` right then. Context compacts automatically and anything not saved is lost. Don't wait for idle.

## Credentials & Secrets — always via the vault

All API credentials (OAuth tokens, API keys, service secrets, bot tokens for non-self services, etc.) live in the Supabase `agent_credentials` vault. Never hardcode, never read from `~/.config/.../*.json` as the primary source, never ask {human} to paste secrets into chat.

### To retrieve
- `mcp__admin-control__get_credential(service="...", key="...")` — your own credential by default
- `mcp__admin-control__list_credentials()` — your stored services (keys shown, values hidden)

### To store a new credential
- `mcp__admin-control__store_credential(service="...", key="...", value="...", metadata="{{...}}")`
- Metadata is free-form JSON (expires_at, email, account_uuid, etc.)

### If the credential is missing
Don't invent workarounds or prompt {human}. Escalate:
- `mcp__admin-control__create_admin_task(target_agent="admin", action="request_credential", params={{"service": "stripe", "key": "api_key"}})`
- Admin polls every 5 min, stores it, marks your task complete. You retry.

### Files that mirror the vault
For performance, some credentials are cached in per-agent config dirs (e.g. `~/{name}/.config/{name}/google-token.json`, `~/.claude-{name}/.credentials.json`). Those are caches — the DB is the source of truth. If you see drift, trust the vault and rewrite the cache.

### Never
- Paste credentials into Telegram/iMessage replies, even for debugging
- Commit credentials to any file git tracks
- Read from `~/.claude/secrets.json` (deprecated — empty)
- Hardcode tokens in scripts "just for now"

## Analytics Logging
After each conversation goes idle, write a brief analytics entry to `~/{name}/analytics.jsonl`. One JSON line per session:

```json
{{"ts": "ISO timestamp", "categories": ["type1", "type2"], "requests": 4, "handled": 3, "stuck": 1, "stuck_detail": "brief description", "confusion_signals": false, "skill_gap": "missing capability", "satisfaction": "positive", "notes": "one-line summary, no personal details"}}
```

This data is for the admin dashboard. Never include {human}'s actual words, personal details, or sensitive content.

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

## Heartbeat (Always-On Watches) — NEW

Heartbeats are persistent watches — things you monitor for your user on a regular cycle (every ~10 minutes). Unlike scheduled tasks which DO things on a schedule, heartbeats WATCH for signals and only act when something matches.

**When to use heartbeats vs scheduled tasks:**
- User says "remind me every Monday at 9am" → **scheduled task** (DO this at this time)
- User says "let me know when I get an email from my accountant" → **heartbeat** (WATCH for this)
- User says "send me a weekly report" → **scheduled task**
- User says "alert me if a customer leaves a bad review" → **heartbeat**

**Rule:** If it needs its own workflow, it's a task. If it's watching for a signal, it's a heartbeat.

**Tools:**
- `add_heartbeat` — Create a watch (type, description, config, notify method)
- `list_heartbeats` — See all active heartbeats
- `update_heartbeat` — Modify a heartbeat
- `delete_heartbeat` — Remove one

**Watch types:** email, lead, review, inventory, uptime, webhook, custom

**When you receive a HEARTBEAT message:**
1. Call `list_heartbeats` to get your active watches with their check_config
2. For each watch, use your available tools to check if the condition is met
3. Only message the user if something matches — stay silent otherwise
4. The system handles the cycle automatically — you don't need to schedule heartbeat checks

**How to talk about it with your user:**
- Say "I've added a heartbeat for that" or "I'll keep an eye on that"
- NEVER mention check_config, watch_type, or any technical details
- Frame it as: "I'm always paying attention and I'll let you know the moment it happens"

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
   sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
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
name: {persona.lower()}
description: {persona}'s identity, personality, values, decision principles
type: soul
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
name: {name}
description: Information about {human} to personalize assistance
type: user
---

# About {human}

Agent setup date: {datetime.now(timezone.utc).strftime('%Y-%m-%d')}

*This file will be populated as {persona} learns about {human} through conversations.*
"""
    (ws / "memory" / f"user_{name}.md").write_text(user_profile)

    # --- Feedback seed (stays here as a starter; agent is expected to use
    # store_memory for all future feedback entries) ---
    feedback = f"""---
name: corrections
description: How {human} wants {persona} to work — corrections and confirmed approaches
type: feedback
---

# Feedback Log

*Entries added as {human} gives guidance. Going forward, use `store_memory(type="feedback", ...)` — don't edit this file directly.*
"""
    (ws / "memory" / "feedback_corrections.md").write_text(feedback)

    # --- Failed approaches ---
    failed = f"""---
name: approaches
description: Things that didn't work and why — prevents repeating dead ends
type: failed
---

# Failed Approaches

Append-only log. Each entry: what was tried, why it failed, what to do instead. Going forward, use `store_memory(type="failed", ...)` — don't edit this file directly.

---
"""
    (ws / "memory" / "failed_approaches.md").write_text(failed)

    # --- MEMORY.md: projector-regenerated, but seed with a pointer so the
    # agent has an index to read at session start before the first projection.
    memory_index = f"""# {persona} Memory Index

*This file is auto-regenerated from the `agent_memories` Supabase table by the projector cron every 15 min. Don't edit directly — use `store_memory` and the projector will rewrite it.*

- [{persona} Identity]({persona.lower()}_soul.md) — Personality, values, decision principles
- [About {human}](user_{name}.md) — User profile
- [Feedback & Corrections](feedback_corrections.md) — How {human} wants me to work
- [Failed Approaches](failed_approaches.md) — Dead ends, don't repeat
"""
    (ws / "memory" / "MEMORY.md").write_text(memory_index)

    # --- startup-instructions.md ---
    startup = f"""# Startup Instructions

You just started via the persistent daemon (launchd + tmux). Do the following:

## On startup (silent — before any user message)

1. Read your CLAUDE.md at `~/{name}/CLAUDE.md` — this defines who you are.
2. Read your memory files for full context:
   - `~/{name}/memory/{persona.lower()}_soul.md` — your identity
   - `~/{name}/memory/MEMORY.md` — memory index
   - `~/{name}/memory/user_{name}.md` — who {human} is
3. Adopt the {persona} persona from your memory.
4. Probe four state flags (don't act on them yet — just know which phase you're in):
   - `has_pro_token` = does `~/.claude-{name}/.credentials.json` exist AND contain a valid `claudeAiOauth.accessToken`?
   - `is_onboarded` = does `~/{name}/.onboarded` exist?
   - `liability_accepted` = does `~/{name}/liability_accepted.json` exist?
   - `is_first_user_message` = no prior user turns in this session
5. Wait silently for {human}'s first Telegram message.

## On {human}'s first message — phase order matters

Run the phases in order. Do NOT skip ahead. Do NOT engage with whatever {human} asked about yet — get setup done first.

### Phase 1 — Connect to {human}'s Claude account (BLOCKING)

Skip if `has_pro_token` is true.

Otherwise, regardless of what {human} said:

> "Hey {human} — {persona} here 👋 Quick 2-minute setup before I can really help. Right now I'm in fallback mode billed against Farlen's API budget. I need to connect to YOUR Claude account so I run on your subscription.
>
> Step 1: I'm going to generate a sign-in URL. One sec…"

Then run:

```bash
python3 /Users/YOUR_MAC_USERNAME/derek/skills/agent-setup/switch_account.py auth-url --agent {name}
```

Reply with the URL it prints, plus:

> "Open that link in your browser → sign in to claude.ai with YOUR account → it'll show you a code and a state value. Paste both back to me here as `code: <X> state: <Y>` and I'll finish the setup."

Wait for {human}'s reply. Parse the code and state. Run:

```bash
python3 /Users/YOUR_MAC_USERNAME/derek/skills/agent-setup/switch_account.py exchange --agent {name} --code <CODE> --state <STATE>
```

Verify `~/.claude-{name}/.credentials.json` now exists with a `claudeAiOauth.accessToken`. Tell {human}:

> "Token saved. I'm going to restart so the new credentials take effect — back in ~30 seconds."

Then trigger your own daemon restart. Two ways depending on what's available to you:

- Preferred: file an admin task — `mcp__admin-control__create_admin_task(target_agent="admin", action="restart_agent", params={{"agent": "{name}", "reason": "Pro token just installed"}})`. Admin polls hourly; if you can't wait, also escalate via Telegram to Farlen (chat_id YOUR_TELEGRAM_CHAT_ID).
- Fallback: run `launchctl bootout gui/$(id -u)/com.{name}-agent.daemon` followed by `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.{name}-agent.daemon.plist` if you have shell access.

When you come back up, this same startup flow runs — `has_pro_token` will now be true, so you'll skip Phase 1 and proceed to Phase 2.

### Phase 2 — Onboarding interview (BLOCKING)

Skip if `is_onboarded` is true.

Conversational, one question per turn (don't dump all at once). After each answer, write a memory record so it persists across restarts. The questions:

1. **Communication style.** "How do you like to be talked to? Terse and to-the-point, or warm and chatty? Sarcasm OK or no? Formal or casual?" → write to `~/{name}/memory/feedback_communication_style.md`
2. **Primary scope.** "What do you want me to focus on? Work, life, both? Specific domains — emails, calendar, finance, research, coding, anything else?" → write to `~/{name}/memory/user_{name}.md` (extend the existing file)
3. **Off-limits.** "Anything off-limits? Topics, accounts, hours of day where I shouldn't ping you, privacy boundaries?" → write to `~/{name}/memory/feedback_off_limits.md`
4. **Recurring routines.** "Do you want any standing reminders or routines from day one? Things like a morning brief, a weekly review, take-your-meds nudges, etc?" → for each, call `mcp__admin-control__schedule_task` to create the cron entry. Confirm what you scheduled back to {human}.
5. **Email/calendar accounts.** "Should I have access to your Gmail or calendar? If yes, which addresses? Otherwise we'll skip and you can grant later." → if yes, file an admin task asking admin to provision Google OAuth tokens for those accounts.
6. **Any persona quirks?** "Anything specific you want from me — sign off a certain way, use particular phrases, avoid certain words, etc?" → extend `~/{name}/memory/{persona.lower()}_soul.md`

When done:

```bash
echo '{{"completed_at": "<iso timestamp>", "via": "first-message-onboarding"}}' > ~/{name}/.onboarded
```

Tell {human}:

> "All set. I've written everything to memory so I'll remember this across restarts. One last quick thing before we get started…"

### Phase 3 — Liability disclaimer + actual conversation

Skip the disclaimer if `liability_accepted` is true.

Otherwise, follow the existing Onboarding flow in CLAUDE.md — send the disclaimer, wait for "I understand", log it to `~/{name}/liability_accepted.json`.

After that, finally engage with whatever {human} originally asked about.

## After all phases pass

You're in normal operation. Just respond as {persona} — the memory + soul + skills are all in place. The phase-checks above are cheap (just file existence) and re-run on every startup, so a corrupted state self-heals (e.g. if `.onboarded` got deleted you'd just re-run the interview).

## Capabilities

You can help with:
- Reminders and to-do tracking
- Translation and language help
- Research and information gathering
- General questions and brainstorming
- Organizing information and planning
"""
    (ws / "startup-instructions.md").write_text(startup)

    # --- .mcp.json (admin-control MCP server) ---
    mcp_config = {
        "mcpServers": {
            "admin-control": {
                "type": "stdio",
                "command": "node",
                "args": [str(HOME / "derek" / "skills" / "admin-mcp" / "server.js")],
                "env": {
                    "AGENT_NAME": name,
                    "SUPABASE_SERVICE_KEY": "YOUR_JWT_TOKEN",
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

export PATH="/opt/homebrew/bin:/Users/YOUR_MAC_USERNAME/.local/bin:/Users/YOUR_MAC_USERNAME/.bun/bin:/usr/local/bin:/usr/bin:/bin:/Users/YOUR_MAC_USERNAME/.orbstack/bin"
export HOME="/Users/YOUR_MAC_USERNAME"
export USER="YOUR_MAC_USERNAME"
export LOGNAME="YOUR_MAC_USERNAME"
export SHELL="/bin/zsh"
export TERM="xterm-256color"
export TELEGRAM_STATE_DIR="{channel_dir}"
export AGENT_NAME="${{AGENT_NAME:-{name}}}"
export SUPABASE_URL="${{SUPABASE_URL:-}}"
export SUPABASE_SERVICE_KEY="${{SUPABASE_SERVICE_KEY:-}}"
unset DISABLE_TELEMETRY

TMUX="/opt/homebrew/bin/tmux"
CLAUDE="/Users/YOUR_MAC_USERNAME/.local/bin/claude"
SESSION="{session}"
COMPONENT_LOG="$HOME/{name}/daemon.log"
COMP="launcher:{name}"

# Source infra logging library
source "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp/infra_lib.sh"

# Apply the telegram plugin orphan-watchdog patch (idempotent).
# Fixes CC 2.1.153 regression where the plugin self-terminates on PPID drift.
bash "/Users/YOUR_MAC_USERNAME/agent-platform/scripts/patch_telegram_plugin.sh" 2>&1 | while IFS= read -r line; do
    [ -n "$line" ] && infra_info "$COMP" "patch_telegram_plugin: $line"
done

# --- Launch Gate: clear ready flag + send restart greeting ---
rm -f "$HOME/{name}/.ready"

# Send restart greeting to user via Telegram
_TG_TOKEN=$(python3 -c "import json; print(json.load(open('{channel_dir}/access.json')).get('botToken',''))" 2>/dev/null || true)
_TG_CHAT=$(python3 -c "import json; af=json.load(open('{channel_dir}/access.json')).get('allowFrom',[]); print(af[0] if af else '')" 2>/dev/null || true)
if [ -n "$_TG_TOKEN" ] && [ -n "$_TG_CHAT" ]; then
    curl -sf -X POST "https://api.telegram.org/bot$_TG_TOKEN/sendMessage" \\
        -d "chat_id=$_TG_CHAT" \\
        -d "text=Just restarted — one moment while I get up to speed." >/dev/null 2>&1 || true
    infra_info "$COMP" "Sent restart greeting to user"
fi
unset _TG_TOKEN _TG_CHAT

# --- Escalation: notify Derek + Farlen on token failure ---
escalate_token_failure() {{
    local REASON="$1"
    local AGENT_LABEL="{name}"
    infra_error "$COMP" "ESCALATING: $REASON"
    curl -s --max-time 10 -X POST "${{SUPABASE_URL}}/rest/v1/admin_tasks" \\
        -H "apikey: ${{SUPABASE_SERVICE_KEY}}" \\
        -H "Authorization: Bearer ${{SUPABASE_SERVICE_KEY}}" \\
        -H "Content-Type: application/json" \\
        -d '{{"target_agent":"derek","action":"fix_token","status":"pending","params":"{{\\\\\"agent\\\\\":\\\\\"{name}\\\\\",\\\\\"reason\\\\\":\\\\\"'$REASON'\\\\\",\\\\\"instructions\\\\\":\\\\\"{name} OAuth token refresh failed during launcher boot. Agent is running on API key (Sonnet) as fallback. Investigate and fix.\\\\\"}}"}}'  >/dev/null 2>&1 || true
    local BOT_TOKEN
    BOT_TOKEN=$(python3 -c "import json; print(json.load(open('$HOME/.claude/channels/telegram_derek/config.json')).get('botToken',''))" 2>/dev/null || true)
    if [ -n "$BOT_TOKEN" ]; then
        curl -sf -X POST "https://api.telegram.org/bot${{BOT_TOKEN}}/sendMessage" \\
            -d "chat_id=YOUR_TELEGRAM_CHAT_ID" \\
            -d "text=⚠️ {name}: Token refresh failed (${{REASON}}). Running on API key (Sonnet) as fallback. Derek has been tasked to fix it." >/dev/null 2>&1 || true
    fi
}}

# Kill stale session if any
$TMUX kill-session -t "$SESSION" 2>/dev/null
sleep 2

infra_info "$COMP" "Starting {persona} in tmux session '$SESSION'"

# Read per-agent OAuth token — refresh inline if expired
CREDS_FILE="$HOME/.claude-{name}/.credentials.json"
EDGE_URL="https://YOUR_SUPABASE_PROJECT_ID.supabase.co/functions/v1/oauth-exchange"
PRO_TOKEN=""

if [ -f "$CREDS_FILE" ]; then
    TOKEN_STATUS=$(python3 -c "
import json, time
d=json.load(open('$CREDS_FILE'))
oauth=d.get('claudeAiOauth',{{}})
token=oauth.get('accessToken','')
expires=oauth.get('expiresAt',0)
if not token:
    print('missing')
elif expires == 0 or expires/1000 > time.time() + 1800:
    print('valid')
elif expires/1000 > time.time():
    print('expiring_soon')
else:
    print('expired')
" 2>/dev/null || echo "error")

    if [ "$TOKEN_STATUS" = "valid" ]; then
        PRO_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS_FILE'))['claudeAiOauth']['accessToken'])" 2>/dev/null)
    elif [ "$TOKEN_STATUS" = "expired" ] || [ "$TOKEN_STATUS" = "expiring_soon" ]; then
        infra_warn "$COMP" "Token $TOKEN_STATUS — refreshing inline before boot"
        REFRESH_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS_FILE'))['claudeAiOauth']['refreshToken'])" 2>/dev/null || true)
        if [ -n "$REFRESH_TOKEN" ]; then
            RESPONSE=$(curl -s --max-time 15 -X POST "$EDGE_URL" \\
                -H "Content-Type: application/json" \\
                -d '{{"grant_type": "refresh_token", "refresh_token": "'$REFRESH_TOKEN'"}}' 2>/dev/null || true)
            REFRESH_OK=$(echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); print('yes' if d.get('status')==200 else 'no')" 2>/dev/null || echo "no")
            if [ "$REFRESH_OK" = "yes" ]; then
                python3 << PYEOF
import json, time
with open("$CREDS_FILE") as f:
    creds = json.load(f)
response = json.loads(\\'\\'\\'\\'$RESPONSE\\'\\'\\'\\'\\')
resp = response["response"]
creds["claudeAiOauth"]["accessToken"] = resp["access_token"]
if "refresh_token" in resp:
    creds["claudeAiOauth"]["refreshToken"] = resp["refresh_token"]
creds["claudeAiOauth"]["expiresAt"] = int(time.time() * 1000) + (resp.get("expires_in", 28800) * 1000)
with open("$CREDS_FILE", "w") as f:
    json.dump(creds, f, indent=2)
PYEOF
                PRO_TOKEN=$(python3 -c "import json; print(json.load(open('$CREDS_FILE'))['claudeAiOauth']['accessToken'])" 2>/dev/null)
                infra_info "$COMP" "Token refreshed inline"
            else
                infra_error "$COMP" "Inline token refresh failed — will fall back to API key"
                escalate_token_failure "inline refresh failed"
            fi
        else
            infra_error "$COMP" "No refresh token available — will fall back to API key"
            escalate_token_failure "no refresh token in credentials"
        fi
    else
        infra_warn "$COMP" "Token status: $TOKEN_STATUS — will fall back to API key"
        escalate_token_failure "token status: $TOKEN_STATUS"
    fi
fi

# Prune bloated sessions so --continue does not load a conversation near context limit
prune_session_if_bloated "$CLAUDE_CONFIG_DIR" "$COMP"
# Resumed-vs-fresh session detection. If CLAUDE_CONFIG_DIR already has session
# JSONLs, this is a restart (token refresh / crash / kick); use --continue so
# the agent inherits the prior conversation. Otherwise run the startup gate.
SESSION_JSONL_DIR="$CLAUDE_CONFIG_DIR/projects"
CONTINUE_FLAG=""
if [ -d "$SESSION_JSONL_DIR" ] && find "$SESSION_JSONL_DIR" -name "*.jsonl" -type f 2>/dev/null | head -1 | grep -q .; then
    CONTINUE_FLAG="--continue"
    APPEND_PROMPT="RESUMED SESSION: You were just restarted (token refresh / crash / kick). The conversation history is intact, but memory/*.md files and scheduled-task state may have updated in your absence. If the conversation resumes from a quiet period, briefly re-check memory/MEMORY.md and memory/sessions/ for any new entries before your next substantive action."
    infra_info "$COMP" "Resumed session detected — launching with --continue"
else
    APPEND_PROMPT="CRITICAL STARTUP GATE: Before responding to ANY channel messages, you MUST first read and execute all instructions in ~/{name}/startup-instructions.md. Complete every step before handling user requests."
    infra_info "$COMP" "Fresh session — running startup gate"
    if [ -f "$CLAUDE_CONFIG_DIR/.last_prune" ]; then
        PRUNE_INFO=$(cat "$CLAUDE_CONFIG_DIR/.last_prune" 2>/dev/null)
        APPEND_PROMPT="$APPEND_PROMPT Your previous conversation was pruned due to $PRUNE_INFO. Your memory, credentials, and scheduled tasks are all intact in Supabase — only the raw conversation transcript was archived. If the user asks why you do not remember recent messages, explain that your conversation history was automatically rotated to stay within context limits, but everything you saved to memory is still available."
        rm -f "$CLAUDE_CONFIG_DIR/.last_prune"
    fi
fi

if [ -n "$PRO_TOKEN" ]; then
    infra_info "$COMP" "Using Pro/Max OAuth token"
    $TMUX new-session -d -s "$SESSION" -c "/Users/YOUR_MAC_USERNAME/{name}" \\
        "env -u ANTHROPIC_API_KEY -u ANTHROPIC_AUTH_TOKEN AGENT_NAME=$AGENT_NAME SUPABASE_URL=$SUPABASE_URL SUPABASE_SERVICE_KEY=$SUPABASE_SERVICE_KEY CLAUDE_CODE_OAUTH_TOKEN=$PRO_TOKEN TELEGRAM_STATE_DIR={channel_dir} $CLAUDE --dangerously-skip-permissions $CONTINUE_FLAG --settings /Users/YOUR_MAC_USERNAME/{name}/settings.json --channels plugin:telegram@claude-plugins-official --append-system-prompt '$APPEND_PROMPT'"
else
    infra_warn "$COMP" "Pro token expired or missing — falling back to API key (Sonnet)"
    $TMUX new-session -d -s "$SESSION" -c "/Users/YOUR_MAC_USERNAME/{name}" \\
        "env -u ANTHROPIC_AUTH_TOKEN AGENT_NAME=$AGENT_NAME SUPABASE_URL=$SUPABASE_URL SUPABASE_SERVICE_KEY=$SUPABASE_SERVICE_KEY TELEGRAM_STATE_DIR={channel_dir} $CLAUDE --model claude-sonnet-4-6 --dangerously-skip-permissions $CONTINUE_FLAG --settings /Users/YOUR_MAC_USERNAME/{name}/settings.json --channels plugin:telegram@claude-plugins-official --append-system-prompt '$APPEND_PROMPT'"
fi

if ! $TMUX has-session -t "$SESSION" 2>/dev/null; then
    infra_critical "$COMP" "Failed to create tmux session — agent DOWN"
    exit 1
fi

infra_info "$COMP" "Session started successfully"

# Pre-open the approval gate. startup-instructions still run on first turn via
# --append-system-prompt, but .ready can't wait on them: in --channels mode,
# tmux send-keys never creates a turn, so the old "agent touches .ready at end
# of startup" path deadlocked before any user message arrived.
sleep 2
touch "$HOME/{name}/.ready"
infra_info "$COMP" "Gate opened (.ready touched)"

# bun-death watchdog: telegram plugin can self-terminate (CC 2.1.153 ppid
# regression). If bun child is gone for >30s, kill the tmux session so launchd
# restarts the daemon. Belt-and-suspenders for the server.ts patch — covers
# any future plugin/runtime regression.
(while $TMUX has-session -t "$SESSION" 2>/dev/null; do
    sleep 60
    pane_pid=$($TMUX display-message -t "$SESSION" -p '#{{pane_pid}}' 2>/dev/null)
    if [ -n "$pane_pid" ] && ! pgrep -P "$pane_pid" -f "bun" >/dev/null 2>&1; then
        sleep 30
        pane_pid=$($TMUX display-message -t "$SESSION" -p '#{{pane_pid}}' 2>/dev/null)
        if [ -n "$pane_pid" ] && ! pgrep -P "$pane_pid" -f "bun" >/dev/null 2>&1; then
            infra_warn "$COMP" "bun plugin died — killing tmux session for launchd restart"
            $TMUX kill-session -t "$SESSION" 2>/dev/null
            break
        fi
    fi
done) &

# Monitor the session — exit when it dies so launchd can restart us
while $TMUX has-session -t "$SESSION" 2>/dev/null; do
    sleep 30
done

infra_error "$COMP" "Session died — launchd will restart"
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

    # 7. Schedule default housekeeping tasks + grant default skill bundle.
    # Without this, new agents show up empty on the dashboard (no skills, no
    # scheduled tasks, no activity stream) — exactly Lola's 2026-05-09 → 13 gap.
    # If SUPABASE_SERVICE_KEY isn't in env we skip with a warning rather than
    # failing the whole provision; operator can run these manually.
    print(f"[7/8] Inserting agents row + scheduling default tasks + granting default skills...")
    supabase_url = os.environ.get("SUPABASE_URL", "https://YOUR_SUPABASE_PROJECT_ID.supabase.co")
    supabase_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not supabase_key:
        print(f"  WARNING: SUPABASE_SERVICE_KEY not in env — skipping agents-row insert + task/skill seeding.")
        print(f"  Manually: insert into agents, schedule_task daily_memory_compact, grant_skill defaults.")
    else:
        import urllib.request, urllib.parse

        # --- Insert agents row FIRST. skill_permissions and scheduled_tasks
        # have FKs to agents(name); without this row every downstream POST
        # silently 409s and the dashboard pop-out comes up empty (the Lola
        # 2026-05-13 incident). ---
        # bot_info is "@handle (Display Name)" from verify_bot — extract handle.
        tg_handle = bot_info.split()[0] if bot_info and bot_info.startswith("@") else None
        agents_payload = {
            "name": name,
            "persona": args.persona,
            "human": args.human,
            "status": "active",
            "model": args.model,
            "timezone": args.timezone,
            "telegram_bot": tg_handle,
            "telegram_chat_id": str(args.user_id) if args.user_id else None,
            "workspace_path": str(ws),
            "tmux_session": f"{name}-agent",
            "daemon_label": f"com.{name}-agent.daemon",
            "billing_type": "pro",
        }
        try:
            req = urllib.request.Request(
                f"{supabase_url}/rest/v1/agents?on_conflict=name",
                data=json.dumps(agents_payload).encode(),
                headers={
                    "apikey": supabase_key,
                    "Authorization": f"Bearer {supabase_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal,resolution=merge-duplicates",
                },
                method="POST",
            )
            urllib.request.urlopen(req, timeout=10)
            print(f"  Inserted agents row for '{name}'.")
        except Exception as e:
            print(f"  ERROR: failed to insert agents row — {e}. Downstream FKs will fail.")

        # --- Default scheduled-task bundle (parity with vera/derek/etc).
        # session_memory is the critical one: it fires the `log_interaction`
        # MCP call every 30 min, which is what populates interaction_logs
        # + memory/sessions/. Without it, conversations vanish without a trace
        # (the Lola 2026-05-09 onboarding session was lost this way).
        # Stagger compaction time per-agent so multiple new agents don't
        # collide on the same minute. ---
        compact_minute = abs(hash(name)) % 55 + 5  # minute 5-59

        DEFAULT_SCHEDULED_TASKS = [
            {
                "id": f"sched_{name}_session_memory",
                "schedule": "*/30 * * * *",
                "trigger": "pull",
                "task_description": (
                    f"Session memory review: Check for idle conversations (no messages from {args.human} "
                    f"in 10+ min after active chat). If idle: (1) Write a session summary to "
                    f"memory/sessions/YYYY-MM-DD_HH.md (use hour of session start). Include: what was "
                    f"discussed, actions taken, decisions made, memories saved, open items, failed "
                    f"approaches. (2) Call the `log_interaction` MCP tool with categories, "
                    f"request_summary, outcome, skill_used, skill_gap, satisfaction. (3) Also write a "
                    f"backup analytics entry to ~/{name}/analytics.jsonl. Update any stale memory files "
                    f"if you learned something new. Background task — don't message {args.human} unless "
                    f"you have a pending question."
                ),
            },
            {
                "id": f"sched_{name}_admin_poll",
                "schedule": "0 * * * *",
                "trigger": "push",
                "task_description": (
                    "Check for pending admin tasks: Call list_pending_tasks. If any tasks are returned, "
                    "verify and execute them, then mark complete. This is how Admin communicates with you."
                ),
            },
            {
                "id": f"sched_{name}_daily_memory_compact",
                "schedule": f"{compact_minute} 3 * * *",
                "trigger": "push",
                "task_description": (
                    f"Daily memory compaction: Review today's session summaries in ~/{name}/memory/sessions/. "
                    f"Merge overlapping sessions from the same day into a single coherent summary. Promote "
                    f"durable learnings into the right type memory (user, feedback, project, reference, "
                    f"failed). Drop chatter. Use store_memory + supersede_memory via admin-control MCP — "
                    f"never edit memory files directly. Background task; no Telegram or escalation output "
                    f"unless a real anomaly is found."
                ),
            },
            {
                "id": f"sched_{name}_log_rotation",
                "schedule": "20 0 * * 1",
                "trigger": "push",
                "task_description": (
                    f"Weekly log rotation: Archive previous week's session summaries older than 14 days "
                    f"from memory/sessions/ into memory/sessions/archive/. Clean up any temporary files "
                    f"in ~/{name}/."
                ),
            },
            {
                "id": f"sched_{name}_weekly_memory_review",
                "schedule": "23 4 * * 0",
                "trigger": "push",
                "task_description": (
                    "Weekly memory review: Deep review of all memory files. Remove stale projects, merge "
                    "contradictory feedback, check for outdated references, verify MEMORY.md index health "
                    "(no orphans, under 200 lines). Write report to memory/sessions/YYYY-MM-DD_weekly.md. "
                    "Background task."
                ),
            },
        ]
        for task in DEFAULT_SCHEDULED_TASKS:
            payload = {
                "id": task["id"],
                "agent_name": name,
                "schedule": task["schedule"],
                "task_description": task["task_description"],
                "recurring": True,
                "active": True,
                "trigger": task["trigger"],
                "created_by": "create_agent.py",
            }
            try:
                req = urllib.request.Request(
                    f"{supabase_url}/rest/v1/scheduled_tasks?on_conflict=id",
                    data=json.dumps(payload).encode(),
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal,resolution=merge-duplicates",
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
                print(f"  Scheduled: {task['id']} ({task['schedule']}, {task['trigger']})")
            except Exception as e:
                print(f"  WARNING: failed to schedule {task['id']} — {e}")

        # --- Grant default skill bundle. Agent-name-specific gmail/calendar
        # skills get granted later when the user's OAuth flow runs (the
        # onboarding interview captures their email/calendar accounts). ---
        DEFAULT_SKILLS = [
            "gmail_inbox", "calendar_management", "send_email", "email_triage",
            "web_research", "reminders", "delivery_tracking", "internal_communications",
        ]
        for skill in DEFAULT_SKILLS:
            payload = {"agent_name": name, "skill_name": skill, "granted_by": "create_agent.py"}
            try:
                req = urllib.request.Request(
                    f"{supabase_url}/rest/v1/skill_permissions",
                    data=json.dumps(payload).encode(),
                    headers={
                        "apikey": supabase_key,
                        "Authorization": f"Bearer {supabase_key}",
                        "Content-Type": "application/json",
                        "Prefer": "return=minimal,resolution=merge-duplicates",
                    },
                    method="POST",
                )
                urllib.request.urlopen(req, timeout=10)
            except Exception as e:
                print(f"  WARNING: failed to grant {skill} — {e}")
        print(f"  Granted {len(DEFAULT_SKILLS)} default skills.")

    # 8. Final summary
    print(f"\n{'='*60}")
    print(f"  {args.persona} is LIVE")
    print(f"{'='*60}")
    print(f"  Workspace:  ~/{name}/")
    print(f"  Tmux:       {name}-agent")
    print(f"  Daemon:     com.{name}-agent.daemon")
    print(f"  Bot:        {bot_info}")
    print(f"  Dashboard:  https://dereks-macbook-pro.tailf80e44.ts.net")
    print(f"\n  {args.human} can now message {bot_info} on Telegram.\n")


if __name__ == "__main__":
    main()
