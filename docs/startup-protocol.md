# Startup Protocol

This document describes the complete launch sequence for an agent in the Agent Platform, from launchd starting the process through the agent being fully operational and ready to receive messages. It covers the launch gate system that prevents agents from responding before they have full context.

---

## Why the Gate Exists

An AI agent without its memory is dangerous. If an agent starts responding to user messages before it has loaded its identity, memories, and responsibilities, it will:

1. **Lose continuity** -- it will not remember previous conversations, user preferences, or ongoing projects.
2. **Violate safety rules** -- it will not know its behavioral boundaries or the approval gate configuration.
3. **Break persona** -- it will respond as a generic assistant instead of the persona the user expects.
4. **Duplicate work** -- it may re-register scheduled tasks, re-send greetings, or repeat actions already completed.

The launch gate ensures that no outbound communication (Telegram replies, iMessage replies) can happen until the agent has explicitly confirmed it has loaded everything and is ready. The gate is a file-based flag: the agent itself creates the `.ready` file only after completing the full startup protocol.

---

## Launch Sequence

### Step 1: launchd starts the daemon

The macOS init system (launchd) reads the agent's plist and starts the launcher script:

```
~/Library/LaunchAgents/com.<agent-name>-agent.daemon.plist
    │
    ▼ ProgramArguments: ["/bin/bash", "~/.claude/launcher.sh"]
```

The plist provides critical environment variables:
- `HOME` -- user home directory
- `AGENT_NAME` -- the agent's identity (e.g., "derek", "vera")
- `ANTHROPIC_API_KEY` or `CLAUDE_CODE_OAUTH_TOKEN` -- authentication
- `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` -- database access
- `MCP_CONNECTION_NONBLOCKING` -- set to `true` so MCP server startup does not block Claude
- `APPROVAL_GATE_ADMIN_CHAT` -- Telegram chat ID for admin approval routing

The plist has `RunAtLoad: true` and `KeepAlive: true`, meaning the agent starts on boot and restarts automatically if it crashes.

### Step 2: launcher.sh kills stale sessions and creates tmux

The launcher script (`scripts/launcher.sh`, deployed to `~/.claude/launcher.sh`) does the following:

```bash
# 1. Set up PATH and environment
export PATH="/opt/homebrew/bin:$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
unset DISABLE_TELEMETRY  # required for channel notifications

# 2. Kill any stale tmux session from a previous run
tmux kill-session -t "<agent-name>-agent" 2>/dev/null
sleep 2

# 3. Start Claude Code in a new detached tmux session
tmux new-session -d -s "<agent-name>-agent" \
  "claude --channels plugin:telegram@claude-plugins-official"
```

The `--channels` flag registers the Telegram channel plugin, which enables inbound message delivery and outbound reply tools.

### Step 3: launcher.sh sends startup instructions

After a 15-second delay (to let Claude Code boot and establish the MCP connection), the launcher sends the startup trigger:

```bash
sleep 15
tmux send-keys -t "<agent-name>-agent" \
  "Read ~/.claude/startup-instructions.md and follow the instructions." Enter
```

This text is injected into the Claude Code session as if a user typed it. Claude reads the file and begins the startup protocol.

### Step 4: launcher.sh monitors the session

The launcher enters a monitoring loop:

```bash
while tmux has-session -t "<agent-name>-agent" 2>/dev/null; do
    sleep 30
done
```

If the tmux session dies (Claude Code crashes, context limit reached, etc.), the loop exits, the script exits with code 0, and launchd restarts the entire process.

---

## The Launch Gate

### How It Works

The launch gate is implemented by the `approval-gate.py` PreToolUse hook, but with a specific startup behavior: before the `.ready` file exists, ALL outbound communication tools are blocked.

The gate operates as follows:

1. **On launcher start**: the launcher deletes the `.ready` file (if it exists from a previous session), ensuring the agent starts in a locked state.

2. **During startup**: any attempt to use Telegram reply, iMessage reply, or other outbound communication tools is intercepted by the approval gate. If `.ready` does not exist, the action is blocked.

3. **After startup protocol completes**: the agent creates the `.ready` file, signaling that it has full context. All subsequent outbound communication tools are allowed through (subject to normal approval rules).

### Configuration

The gate is configured in the agent's `settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|mcp__plugin_imessage_imessage__reply",
        "hooks": [
          {
            "type": "command",
            "command": "python3 <platform-dir>/hooks/approval-gate.py"
          }
        ]
      }
    ]
  }
}
```

Note that Telegram reply (`mcp__plugin_telegram_telegram__reply`) is deliberately NOT in the matcher for the approval gate during normal operation, because gating Telegram replies would break the agent's ability to respond. The startup gate is the special case where even Telegram is blocked until the agent is ready.

---

## Startup Instructions

When Claude Code reads `startup-instructions.md` (from `configs/startup-instructions.md`, deployed to the agent's workspace), it receives this protocol:

```markdown
# Startup Instructions

You just started via the persistent daemon (launchd + tmux). Do the following:

1. Send a Telegram message to your human's chat_id: "Back online." (Keep it short.)
2. Read your memory files to get context on who you are and what's going on.
3. Adopt your persona from memory.
4. Scheduled tasks are persistent in Supabase -- they survive restarts.
   - Do NOT re-register crons on startup -- they already exist in the database.
5. State is persistent in Supabase with local fallback.
   - Do NOT read/write local state files directly -- the MCP tools handle write-through caching.
6. Check for pending admin_tasks assigned to you via list_admin_tasks.
7. Env vars are in your daemon plist.

After completing these steps, you're ready. Wait for messages.
```

---

## The 5-Step Startup Protocol (Extended)

The basic startup instructions above are the minimum. In a production deployment, agents follow an extended 5-step protocol that ensures deep context loading. This protocol is embedded in the agent's CLAUDE.md or appended to startup instructions:

### Step 1: Load Identity

The agent reads its soul file from memory:

```
Read ~/agent-name/memory/agent_soul.md
```

This file defines the agent's personality, values, decision principles, and communication style. It is the agent's core identity -- without it, the agent is a generic assistant.

### Step 2: Load All Memories

The agent reads the memory index, then loads every referenced file:

```
Read ~/agent-name/memory/MEMORY.md              # Index (lists all memory files)
Read ~/agent-name/memory/user_*.md               # User profiles
Read ~/agent-name/memory/feedback*.md            # All feedback memories
Read ~/agent-name/memory/project_*.md            # All project memories
Read ~/agent-name/memory/reference_*.md          # All reference memories
Read ~/agent-name/memory/failed_approaches.md    # Known dead ends
```

This is not selective. The agent reads ALL memory files, not just the ones that seem relevant to a hypothetical next task. The point is to restore full context so the agent can handle anything the user throws at it.

### Step 3: Load Responsibilities

The agent checks its scheduled tasks and pending admin tasks:

```
MCP call: list_scheduled_tasks      # See what crons are active
MCP call: list_pending_tasks        # Check for inter-agent tasks
MCP call: my_skills                 # Verify capabilities
```

This step confirms the agent's operational state: what it is supposed to be doing, what others have asked it to do, and what tools it has access to.

### Step 4: Open the Gate

After all context is loaded, the agent creates the `.ready` file:

```
Write ~/.claude/agent-name.ready    # Or touch the file via Bash
```

From this moment, outbound communication tools are unblocked. The agent is fully operational.

### Step 5: Session Recap

The agent reads the most recent session summary and sends a brief recap to the user:

```
Read ~/agent-name/memory/sessions/   # Find latest session file
```

Then sends a 2-4 bullet Telegram message summarizing what happened in the last session:

```
Back online.

Last session:
- Finished the weekly finance report and deployed it
- Updated Vera's OAuth token (was expiring)
- You asked me to check on the BugHerd tickets -- will do that now
- Open item: waiting on your approval for the email draft
```

This recap serves two purposes:
1. **Continuity signal** -- the user knows the agent remembers everything
2. **Action resumption** -- open items from the last session are surfaced immediately

---

## Memory Injection Details

The memory system is file-based, stored at `~/<agent-name>/memory/`. On startup, the agent loads these categories:

### Soul File (`*_soul.md`)

The agent's identity document. Contains:
- Name and persona description
- Personality traits and communication style
- Core values and decision-making principles
- Relationship to the user
- What the agent is NOT (boundaries)

Example: a soul file might define that the agent is direct, hates sycophancy, uses a specific tone, and has a particular sense of humor. Without this file, the agent defaults to Claude's baseline personality.

### User Profile (`user_*.md`)

Everything the agent knows about its user:
- Name, role, expertise level
- Communication preferences (terse vs. verbose, preferred channels)
- Access setup (devices, network topology)
- Work context (company, projects, goals)

### Feedback Memories (`feedback*.md`)

Behavioral corrections and confirmations from the user. These are the most critical memories for maintaining quality:
- "Don't ask permission for local actions, just act"
- "Always combine web search + internal knowledge for research"
- "Send brief Telegram updates as work happens, not just at the end"
- "Never use hacky fallbacks; handle edge cases properly from the start"

Each feedback entry includes the rule, why it exists, and how to apply it.

### Project Memories (`project_*.md`)

Active work context:
- Current projects, their status, and key decisions
- Architecture choices and their rationale
- Deployment details (URLs, credentials, configs)
- Stakeholder requirements

### Reference Memories (`reference_*.md`)

Pointers to external systems:
- API endpoints and their quirks
- Cron schedules and what they do
- Email account configurations
- Service credentials and how to use them

### Failed Approaches (`failed_approaches.md`)

Known dead ends that the agent should not repeat:
- Approaches that were tried and failed
- Tools or APIs that do not work as expected
- Configuration mistakes that waste time

### Latest Session Summary (`sessions/`)

The most recent session file contains:
- What was discussed and done
- Decisions made
- Open items and follow-ups
- Memories that were created or updated during the session

---

## Crash Recovery

When an agent crashes (context limit, runtime error, network issue):

1. The tmux session dies
2. `launcher.sh`'s monitoring loop detects this and exits
3. launchd's `KeepAlive: true` restarts the launcher
4. The full startup sequence repeats from Step 1
5. The agent loads all memories, including the session summary from before the crash
6. The `.ready` gate ensures no premature responses during reload

The session memory cron (every 30 minutes) ensures that even if a crash occurs mid-conversation, most context is captured in a session summary. The worst case is losing up to 30 minutes of unsummarized conversation.

---

## Timing

Approximate startup timeline:

| Time | Event |
|------|-------|
| T+0s | launchd starts launcher.sh |
| T+2s | Stale tmux session killed, new session created |
| T+5s | Claude Code CLI initializes, MCP server connects |
| T+15s | Launcher sends "Read startup-instructions.md" |
| T+20s | Agent reads startup instructions |
| T+25s | Agent sends "Back online" Telegram message |
| T+30s | Agent reads soul file and memory index |
| T+45s | Agent finishes reading all memory files |
| T+50s | Agent checks scheduled tasks and admin tasks |
| T+55s | Agent creates .ready file (gate opens) |
| T+60s | Agent sends session recap to user |
| T+65s | **Agent is fully operational** |

Total startup time: approximately 60-90 seconds, depending on the number of memory files and Supabase latency.

---

## Verifying the Gate

To check whether an agent's gate is open:

```bash
# Check if .ready file exists
ls -la ~/.claude/<agent-name>.ready

# Check if the agent session is running
tmux has-session -t <agent-name>-agent && echo "running" || echo "down"

# Watch the agent boot in real time
tmux attach -t <agent-name>-agent
# (Ctrl-B D to detach without killing the session)
```

To manually force-open the gate (emergency override):

```bash
touch ~/.claude/<agent-name>.ready
```

To manually close the gate (force the agent to re-run startup):

```bash
rm ~/.claude/<agent-name>.ready
```
