# Approval System

This document describes the two-tier approval gate that intercepts tool calls before they execute. If you are an agent reading this, this is the system that sometimes blocks your actions and asks a human for permission.

## Overview

The approval system is a PreToolUse hook that runs before every matching tool call. It classifies the action into one of three categories:

1. **Admin tier** — modifications to shared infrastructure. Routed to the platform admin's Telegram.
2. **User tier** — external communications and API calls. Routed to the agent's own user's Telegram.
3. **No gate** — normal operations. Proceeds immediately with no approval needed.

**File:** `hooks/approval-gate.py`

## How It Works

### Hook registration

The approval gate is registered in each agent's `settings.json` as a PreToolUse hook:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash|Edit|Write|mcp__plugin_imessage_imessage__reply",
        "hooks": [
          {
            "type": "command",
            "command": "python3 /path/to/agent-platform/hooks/approval-gate.py"
          }
        ]
      }
    ]
  }
}
```

The `matcher` field limits which tools trigger the hook. Only `Bash`, `Edit`, `Write`, and iMessage reply are checked. Tools like `Read`, `Grep`, `Glob`, `Telegram reply`, and MCP tools pass through without invoking the hook at all.

The template is at `configs/template/settings.json`. During agent creation, `{{PLATFORM_DIR}}` is replaced with the actual platform directory path.

### Execution flow

When Claude Code is about to execute a matched tool:

1. Claude Code pipes a JSON payload to the hook's stdin:
   ```json
   {
     "tool_name": "Bash",
     "tool_input": {
       "command": "python3 send_email.py --to someone@example.com"
     }
   }
   ```

2. The hook reads the payload and calls `classify_action(tool_name, tool_input)`.

3. If classification returns `None` — **no gate**. The hook exits with code 0 (allow).

4. If classification returns `"admin"` or `"user"` — **approval needed**:
   a. Reads the agent's Telegram bot token from its channel `.env` file.
   b. Determines the target chat ID (admin or user, based on tier).
   c. Sends an approval request message to Telegram with a summary of the action.
   d. Polls for the human's response (yes/no) for up to 120 seconds.
   e. If approved: exits with code 0 (allow).
   f. If denied or timed out: writes a JSON denial to stdout and exits with code 0.

### Denial output

When a tool is denied, the hook writes this to stdout:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "PreToolUse",
    "permissionDecision": "deny",
    "permissionDecisionReason": "Denied via Telegram (user tier)"
  }
}
```

Claude Code interprets this as a tool denial and will not execute the action.

## Classification Rules

### Admin Tier

Admin-tier actions are modifications to shared platform infrastructure. These route to the platform admin's Telegram chat (identified by `APPROVAL_GATE_ADMIN_CHAT` env var).

**Edit/Write operations — admin if touching:**
- MCP server code or scheduler: paths matching `mcp-server/server.js`, `scheduler_executor`, `infra_lib.sh`
- Platform infra scripts: paths matching `/derek/skills/admin-mcp/`, `/derek/scripts/`
- Agent health monitoring: `agent_health_check.py`
- Security monitoring: `security_watch.py`
- Token refresh infrastructure: `refresh_all_agents.sh`
- Another agent's workspace directory (cross-workspace access)

**Bash commands — admin if:**
- `git push` targeting `agent-platform` or `agent-infra` repos
- Destructive writes to shared infra paths (redirect into, delete, move, or sed in-place)
- Supabase schema changes: `CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`, `CREATE INDEX`

### User Tier

User-tier actions are external communications that the agent's user should approve. These route to the user's own Telegram chat ID (from the `USER_CHAT_IDS` dict in the hook, falling back to admin if not configured).

**Actions gated at user tier:**
- Running `send_email.py` or any command matching `send_email` (via Bash)
- Sending iMessage replies (`mcp__plugin_imessage_imessage__reply`)

### No Gate

These actions proceed without any approval:

**Never-gated tools (skipped entirely by the matcher):**
- `Read`, `Glob`, `Grep` — file reading and searching
- `Agent`, `Skill`, `ToolSearch` — internal orchestration
- `mcp__plugin_telegram_telegram__reply` — Telegram replies (gating this would break the agent's primary communication channel)
- `mcp__plugin_telegram_telegram__react` — Telegram emoji reactions
- `mcp__plugin_telegram_telegram__edit_message` — Telegram message edits
- All MCP admin-control tools (vault, skills, scheduling, state, admin tasks)

**Matched tools that classify as no-gate:**
- Edit/Write to the agent's own workspace (`~/AGENT_NAME/`)
- Edit/Write to other non-infra paths (e.g., `.claude` config)
- Bash commands that are not destructive writes to infra, not git pushes to platform repos, not schema changes, and not email sends
- All Bash operations by the admin agent (derek) except email sends

### Special case: Admin agent (derek)

The admin agent has relaxed rules. Since derek manages the infrastructure, admin-tier gates do not apply to derek's own operations. Derek only gets user-tier gates for:
- Email sends (`send_email` in Bash commands)
- iMessage replies

All other operations by derek pass through without approval.

## Telegram Notification

### Message format

When approval is needed, the hook sends a message like:

```
[RED_CIRCLE] ADMIN Approval Required [vera]

Command: python3 scripts/modify_scheduler.py

Reply yes or no. Auto-denies in 120s.
```

or

```
[YELLOW_CIRCLE] USER Approval Required

Command: python3 send_email.py --to client@example.com --subject "Invoice"

Reply yes or no. Auto-denies in 120s.
```

The message includes:
- Tier label (ADMIN or USER)
- Agent name (shown for non-derek agents)
- Action summary: command text (truncated to 200 chars), file path, or tool name
- Instructions for responding

### Accepted responses

The hook polls for a message from the correct chat ID. Accepted responses (case-insensitive):

| Response | Effect |
|----------|--------|
| `yes`, `y`, `approve`, `ok`, `go` | Approved |
| `no`, `n`, `deny`, `stop`, `nope` | Denied |

Any other text is ignored. The hook only processes messages from the expected `chat_id`.

### Timeout

If no response is received within 120 seconds (`TIMEOUT_SECONDS`), the action is automatically denied. A timeout message is sent to Telegram.

### Approval caching

Approvals are cached for 5 minutes to avoid re-asking for the same action:

- Each approval request is identified by a hash of `tool_name:tool_input`
- When approved, a file is written to `~/.claude/hooks/approvals/REQUEST_ID.approved`
- Before sending a new approval request, the hook checks for a cached approval file less than 5 minutes (300 seconds) old
- If found, the action is auto-approved without messaging Telegram

This means: if you approve "send email to X with subject Y," the exact same action within 5 minutes will not ask again.

### Bot token resolution

The hook reads the Telegram bot token from the agent's channel `.env` file:
- Derek: `~/.claude/channels/telegram/.env`
- Other agents: `~/.claude/channels/telegram_AGENT/.env`

The token line format: `TELEGRAM_BOT_TOKEN=123456:ABC...`

If the bot token cannot be read, the hook **fails open** (allows the action). This is a deliberate design choice — a missing bot token should not brick the agent.

## Configuration

### Environment variables

Set in each agent's launchd plist (`~/Library/LaunchAgents/com.AGENT-agent.daemon.plist`):

| Variable | Purpose |
|----------|---------|
| `AGENT_NAME` | Identifies which agent is running. Used to determine workspace path, bot token location, and classification rules. |
| `APPROVAL_GATE_ADMIN_CHAT` | Telegram chat ID of the platform admin. All admin-tier approvals and user-tier fallbacks go here. |

### User chat IDs

The `USER_CHAT_IDS` dictionary in `approval-gate.py` maps agent names to their user's Telegram chat ID:

```python
USER_CHAT_IDS = {
    "vera": "123456789",
    "nate": "987654321",
}
```

If an agent's user is not in this dict, user-tier approvals fall back to `ADMIN_CHAT_ID`. Fill these in as users onboard.

### Workspace mapping

The `AGENT_WORKSPACES` dictionary maps agent names to their workspace directories:

```python
AGENT_WORKSPACES = {
    "derek": os.path.join(HOME, "derek"),
    "vera": os.path.join(HOME, "vera"),
    "nate": os.path.join(HOME, "nate"),
    # ... add new agents here
}
```

This is used to determine if an Edit/Write operation targets the agent's own workspace (no gate) or another agent's workspace (admin gate).

### Adding a new agent

When creating a new agent:

1. Add the agent to `AGENT_WORKSPACES` in `hooks/approval-gate.py`
2. Optionally add to `USER_CHAT_IDS` with the user's Telegram chat ID
3. Ensure `AGENT_NAME` and `APPROVAL_GATE_ADMIN_CHAT` are set in the agent's launchd plist
4. The agent's `settings.json` should reference the hook (copied from the template)

## Launch Gate

Separate from the approval gate, there is a **launch gate** concept: the agent must not reply to channel messages until startup is complete.

This is handled by the startup instructions flow, not the approval hook:
1. The daemon launcher (`scripts/launcher.sh`) starts Claude Code in tmux with the `--channels` flag
2. After 15 seconds, it sends startup instructions into the session
3. The agent reads its memory, adopts its persona, and completes initialization
4. Only after startup is the agent ready to process inbound messages

The launch gate ensures the agent has full context before responding. Without it, an agent might reply to a Telegram message before reading its memory files, leading to responses that lack personality or context.

There is no `.ready` file mechanism in the current implementation — the launch gate is implicit based on the agent completing its startup instructions.

## Edge Cases

**Fail-open behavior:** If the hook cannot read a bot token, send a Telegram message, or parse the input JSON, it exits with code 0 (allow). The philosophy is: a broken approval system should not prevent the agent from working. The security cost of fail-open is accepted in exchange for availability.

**Multiple rapid approvals:** The 5-minute approval cache prevents spam. If an agent needs to send 10 emails in a row, the user approves the first one and the cache covers subsequent identical requests. Different recipients or subjects generate different hashes and require separate approvals.

**Concurrent tool calls:** Each hook invocation is a separate process. If two tool calls trigger approval simultaneously, two separate Telegram messages are sent. The user must respond to each independently. The approval cache can help if they happen to be identical.

**Admin agent infra access:** Derek is explicitly exempted from admin-tier gates because derek IS the admin. If derek needs to modify the scheduler or MCP server, it just works. Sub-agents touching the same files would trigger an admin approval.
