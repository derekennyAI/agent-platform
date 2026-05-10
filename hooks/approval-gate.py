#!/usr/bin/env python3
"""
PreToolUse hook: Two-tier approval gate using Supabase.

Flow:
1. Hook catches a risky operation
2. Checks Supabase for a recent approval matching this request
3. If approved → allow (exit silently)
4. If no approval → write pending request to Supabase → deny
5. The agent sees the denial, sends user a Telegram message explaining what needs approval
6. User approves via Telegram (agent updates Supabase row)
7. Agent retries → hook sees approval → allows

No Telegram polling from the hook = no conflict with the channels plugin.

ADMIN TIER: Shared infra changes → routed to platform admin
USER TIER: Operational actions → routed to agent's own user
NO GATE: Own workspace edits, reads, Telegram replies
"""

import json, sys, os, time, urllib.request, urllib.parse, hashlib, re

# --- Config ---
AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
HOME = os.path.expanduser("~")
SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# Agent workspace directories
AGENT_WORKSPACES = {
    "admin": os.path.join(HOME, "admin"),
    "derek": os.path.join(HOME, "derekPersonal"),
    "dereklm": os.path.join(HOME, "dereklm"),
    "vera": os.path.join(HOME, "vera"),
    "nate": os.path.join(HOME, "nate"),
    "blake": os.path.join(HOME, "blake"),
    "julie": os.path.join(HOME, "julie"),
    "macgyver": os.path.join(HOME, "macgyver"),
}

# --- Shared infra paths (admin gate if touched by sub-agents) ---
SHARED_INFRA_PATTERNS = [
    r"/derek/skills/admin-mcp/",
    r"/derek/scripts/",
    r"mcp-server/server\.js",
    r"scheduler_executor",
    r"infra_lib\.sh",
    r"refresh_all_agents\.sh",
    r"security_watch\.py",
    r"agent_health_check\.py",
]


# --- Classification ---

def classify_action(tool_name, tool_input):
    """Returns: "admin", "user", or None (no gate)."""

    # --- Always ungated (read-only, navigation, telemetry) ---
    UNGATED_TOOLS = {
        "Read", "Glob", "Grep", "Agent", "TaskCreate", "TaskUpdate",
        "TaskGet", "TaskList", "TaskOutput", "TaskStop",
        "mcp__plugin_telegram_telegram__react",
        "mcp__plugin_telegram_telegram__reply",
        "mcp__plugin_telegram_telegram__edit_message",
        "mcp__plugin_telegram_telegram__download_attachment",
        "mcp__plugin_imessage_imessage__chat_messages",
        "ToolSearch", "Skill", "CronCreate", "CronDelete", "CronList",
        "Monitor", "WebFetch", "WebSearch",
    }

    # --- MCP tools: read-only / telemetry (always ungated) ---
    UNGATED_MCP = {
        "mcp__admin-control__get_credential",
        "mcp__admin-control__list_credentials",
        "mcp__admin-control__list_scheduled_tasks",
        "mcp__admin-control__list_pending_tasks",
        "mcp__admin-control__list_skill_catalog",
        "mcp__admin-control__list_skills",
        "mcp__admin-control__list_agents",
        "mcp__admin-control__list_state",
        "mcp__admin-control__get_state",
        "mcp__admin-control__get_agent_info",
        "mcp__admin-control__my_skills",
        "mcp__admin-control__verify_task",
        "mcp__admin-control__complete_task",
        "mcp__admin-control__list_heartbeats",
        "mcp__admin-control__log_interaction",
        "mcp__admin-control__log_infra_event",
        "mcp__admin-control__start_session",
        "mcp__admin-control__end_session",
    }

    # --- MCP tools: user-tier gated (destructive or outbound) ---
    USER_GATED_MCP = {
        "mcp__admin-control__schedule_task",
        "mcp__admin-control__update_scheduled_task",
        "mcp__admin-control__delete_scheduled_task",
        "mcp__admin-control__store_credential",
        "mcp__admin-control__revoke_credential",
        "mcp__admin-control__connect_service",
        "mcp__admin-control__disconnect_service",
        "mcp__admin-control__grant_skill",
        "mcp__admin-control__revoke_skill",
        "mcp__admin-control__set_state",
        "mcp__admin-control__delete_state",
        "mcp__admin-control__update_agent_status",
        "mcp__admin-control__run_skill",
        "mcp__admin-control__add_heartbeat",
        "mcp__admin-control__update_heartbeat",
        "mcp__admin-control__delete_heartbeat",
    }

    # --- Admin-tier MCP tools ---
    ADMIN_GATED_MCP = {
        "mcp__admin-control__create_admin_task",
    }

    if tool_name in UNGATED_TOOLS or tool_name in UNGATED_MCP:
        return None

    if tool_name in ADMIN_GATED_MCP:
        # Derek IS the admin — admin tasks from Derek are internal coordination, not external
        if AGENT_NAME == "derek":
            return None
        return "admin"

    if tool_name in USER_GATED_MCP:
        # Derek manages infra — scheduling and state are part of his job
        if AGENT_NAME == "derek":
            return None
        return "user"

    # --- iMessage send ---
    if tool_name == "mcp__plugin_imessage_imessage__reply":
        return "user"

    # --- Derek: gate email, calendar writes, iMessage sends ---
    if AGENT_NAME == "derek":
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            # Auto-allow daily digest emails to Farlen
            if re.search(r"send_email", cmd, re.IGNORECASE):
                if re.search(r"farlen@enny\.ai", cmd) and re.search(r"Daily Brief", cmd):
                    return None
                return "user"
            # Calendar writes (curl or Python urllib)
            if re.search(r"googleapis\.com/calendar", cmd, re.IGNORECASE):
                if re.search(r"(-X\s*(POST|PUT|PATCH|DELETE)|method\s*=\s*['\"]?(POST|PUT|PATCH|DELETE))", cmd, re.IGNORECASE):
                    return "user"
            if re.search(r"graph\.microsoft\.com/.*/events", cmd, re.IGNORECASE):
                if re.search(r"(-X\s*(POST|PUT|PATCH|DELETE)|method\s*=\s*['\"]?(POST|PUT|PATCH|DELETE))", cmd, re.IGNORECASE):
                    return "user"
            if re.search(r"caldav\.icloud\.com", cmd, re.IGNORECASE):
                if re.search(r"(-X\s*(PUT|DELETE|PROPPATCH)|method\s*=\s*['\"]?(PUT|DELETE|PROPPATCH))", cmd, re.IGNORECASE):
                    return "user"
            return None
        return None

    # === Sub-agent rules below ===

    my_workspace = AGENT_WORKSPACES.get(AGENT_NAME, "")

    # --- Edit/Write ---
    if tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path", "")
        if my_workspace and file_path.startswith(my_workspace):
            return None
        for pattern in SHARED_INFRA_PATTERNS:
            if re.search(pattern, file_path):
                return "admin"
        for agent, ws in AGENT_WORKSPACES.items():
            if agent != AGENT_NAME and ws and file_path.startswith(ws):
                return "admin"
        return None

    # --- Bash ---
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")

        # Admin tier: shared infra modifications
        if re.search(r"git push", cmd, re.IGNORECASE):
            if re.search(r"agent-platform|agent-infra", cmd, re.IGNORECASE):
                return "admin"
        for pattern in SHARED_INFRA_PATTERNS:
            if re.search(pattern, cmd):
                if re.search(r"(>\s*" + pattern + r"|>>\s*" + pattern + r"|rm .*" + pattern + r"|mv .*" + pattern + r"|sed -i.*" + pattern + r")", cmd):
                    return "admin"
        if re.search(r"CREATE TABLE|ALTER TABLE|DROP TABLE|CREATE INDEX", cmd, re.IGNORECASE):
            return "admin"

        # Admin tier: process/daemon management
        if re.search(r"launchctl\s+(unload|load|remove|bootout|disable)", cmd, re.IGNORECASE):
            return "admin"

        # User tier: outbound email (any method)
        if re.search(r"send_email", cmd, re.IGNORECASE):
            return "user"
        if re.search(r"googleapis\.com/gmail.*send|gmail\.users\.messages\.send", cmd, re.IGNORECASE):
            return "user"
        if re.search(r"graph\.microsoft\.com/.*/sendMail", cmd, re.IGNORECASE):
            return "user"

        # User tier: calendar writes (curl -X or Python urllib method=)
        if re.search(r"googleapis\.com/calendar", cmd, re.IGNORECASE):
            if re.search(r"(-X\s*(POST|PUT|PATCH|DELETE)|method\s*=\s*['\"]?(POST|PUT|PATCH|DELETE))", cmd, re.IGNORECASE):
                return "user"
        if re.search(r"graph\.microsoft\.com/.*/events", cmd, re.IGNORECASE):
            if re.search(r"(-X\s*(POST|PUT|PATCH|DELETE)|method\s*=\s*['\"]?(POST|PUT|PATCH|DELETE))", cmd, re.IGNORECASE):
                return "user"
        if re.search(r"caldav\.icloud\.com", cmd, re.IGNORECASE):
            if re.search(r"(-X\s*(PUT|DELETE|PROPPATCH)|method\s*=\s*['\"]?(PUT|DELETE|PROPPATCH))", cmd, re.IGNORECASE):
                return "user"

        # User tier: Notion destructive writes
        if re.search(r"api\.notion\.com", cmd, re.IGNORECASE):
            if re.search(r"-X\s*(DELETE|PATCH)", cmd, re.IGNORECASE):
                return "user"

        # User tier: external service modifications
        if re.search(r"bugherd\.com.*-X\s*(POST|PUT|PATCH|DELETE)", cmd, re.IGNORECASE):
            return "user"
        if re.search(r"api\.track\.toggl\.com.*(POST|PUT|PATCH|DELETE)", cmd, re.IGNORECASE):
            return "user"

        # User tier: destructive file/git operations
        if re.search(r"\brm\s+-r|\brmdir\b|\brm\s+", cmd) and not re.search(r"\.pyc|__pycache__|\.tmp|/tmp/", cmd):
            return "user"
        if re.search(r"git\s+(reset\s+--hard|clean\s+-[fd]|checkout\s+\.)", cmd, re.IGNORECASE):
            return "user"

        # User tier: process killing
        if re.search(r"\bkill\b|\bpkill\b|\bkillall\b", cmd):
            return "user"

        # User tier: Gmail modifications (archive, delete)
        if re.search(r"gmail_inbox\.py.*\b(archive|batch-archive|delete)\b", cmd, re.IGNORECASE):
            return "user"

        return None

    return None


# --- Supabase helpers ---

def supabase_request(method, path, data=None):
    """Make a request to Supabase REST API."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    payload = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=payload, method=method)
    req.add_header("apikey", SUPABASE_KEY)
    req.add_header("Authorization", f"Bearer {SUPABASE_KEY}")
    req.add_header("Content-Type", "application/json")
    if method == "POST":
        req.add_header("Prefer", "return=representation")
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except Exception:
        return None


def check_approval(request_id):
    """Check if this request has been approved in Supabase."""
    result = supabase_request("GET",
        f"approval_requests?request_id=eq.{request_id}&status=eq.approved&select=id")
    return bool(result)


def create_pending_request(request_id, tier, tool_name, tool_summary):
    """Write a pending approval request to Supabase."""
    supabase_request("POST", "approval_requests", {
        "agent_name": AGENT_NAME,
        "request_id": request_id,
        "tier": tier,
        "tool_name": tool_name,
        "tool_summary": tool_summary[:500] if tool_summary else "",
        "status": "pending",
    })


def make_request_id(tool_name, tool_input):
    """Deterministic ID for this request. Excludes volatile fields like 'description'."""
    stable_input = {k: v for k, v in tool_input.items() if k not in ("description",)}
    content = f"{AGENT_NAME}:{tool_name}:{json.dumps(stable_input, sort_keys=True)}"
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def make_summary(tool_name, tool_input):
    """Human-readable summary of what the tool call does."""
    if tool_name == "Bash":
        return tool_input.get("command", "")[:300]
    elif tool_name in ("Edit", "Write"):
        return f"Modify file: {tool_input.get('file_path', '')}"
    elif tool_name == "mcp__plugin_imessage_imessage__reply":
        return f"Send iMessage to chat {tool_input.get('chat_id', '?')}"
    return f"{tool_name}: {json.dumps(tool_input)[:200]}"


# --- Launch Gate ---

def is_agent_ready():
    """Check if the agent has completed startup. Returns True if ready."""
    workspace = AGENT_WORKSPACES.get(AGENT_NAME, "")
    if not workspace:
        return True
    return os.path.exists(os.path.join(workspace, ".ready"))


# --- Main ---

def main():
    # Farlen (primary user session) is above the gate. CLAUDE_CONFIG_DIR is the
    # reliable discriminator — AGENT_NAME can leak from parent shell env. This
    # bypass leaves all 6 fleet agents (admin, derek, dereklm, vera, nate,
    # macgyver) fully gated because each runs with its own CLAUDE_CONFIG_DIR.
    if os.environ.get("CLAUDE_CONFIG_DIR", "").endswith("claude-farlen"):
        sys.exit(0)

    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    # Launch gate: block Telegram replies until agent startup completes
    if not is_agent_ready():
        BLOCKED_DURING_STARTUP = {
            "mcp__plugin_telegram_telegram__reply",
            "mcp__plugin_telegram_telegram__edit_message",
            "mcp__plugin_imessage_imessage__reply",
        }
        if tool_name in BLOCKED_DURING_STARTUP:
            json.dump({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": "STARTUP_IN_PROGRESS — complete all startup-instructions.md steps first, then create the .ready file in your workspace before responding to messages.",
                }
            }, sys.stdout)
            sys.exit(0)

    tier = classify_action(tool_name, tool_input)

    if tier is None:
        # Ungated — explicitly allow so the built-in permission system is skipped
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }, sys.stdout)
        sys.exit(0)

    # Gated action — check Supabase for existing approval, deny if none
    request_id = make_request_id(tool_name, tool_input)

    if check_approval(request_id):
        # Already approved in Supabase — allow
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "allow",
            }
        }, sys.stdout)
        sys.exit(0)

    # Not approved — create pending request and deny with reason
    summary = make_summary(tool_name, tool_input)
    create_pending_request(request_id, tier, tool_name, summary)

    json.dump({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": (
                f"APPROVAL_NEEDED [{tier}] request_id={request_id} | {summary}"
            ),
        }
    }, sys.stdout)
    sys.exit(0)


if __name__ == "__main__":
    main()
