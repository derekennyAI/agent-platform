#!/usr/bin/env python3
"""
PreToolUse hook: Two-tier approval gate.

ADMIN TIER (routes to platform admin):
  - Modifying shared infra (MCP server code, scheduler executor, infra scripts)
  - Cross-workspace file access (touching another agent's directory)
  - Git push to platform/infra repos
  - Shared Supabase schema changes

USER TIER (routes to the agent's own user):
  - Sending emails
  - Sending messages (iMessage)
  - External API posts
  - Creating PRs/issues

NO GATE (just happens):
  - Agent editing its own workspace files (CLAUDE.md, scheduled tasks, credentials)
  - Reading files, searching, normal tool use
  - Telegram replies (would break the agent)

Config:
  Set AGENT_NAME env var to identify the agent.
  Set APPROVAL_GATE_ADMIN_CHAT to override admin chat ID.
  Bot token is read from the agent's Telegram channel .env file.
"""

import json, sys, os, time, urllib.request, urllib.parse, hashlib, re

# --- Config ---
AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
HOME = os.path.expanduser("~")
ADMIN_CHAT_ID = os.environ.get("APPROVAL_GATE_ADMIN_CHAT", "")  # Set in .env or plist
APPROVALS_DIR = os.path.join(HOME, ".claude", "hooks", "approvals")
TIMEOUT_SECONDS = 120

# Agent workspace directories
AGENT_WORKSPACES = {
    "derek": os.path.join(HOME, "derek"),
    "dereklm": os.path.join(HOME, "dereklm"),
    "vera": os.path.join(HOME, "vera"),
    "nate": os.path.join(HOME, "nate"),
    "blake": os.path.join(HOME, "blake"),
    "julie": os.path.join(HOME, "julie"),
    "macgyver": os.path.join(HOME, "macgyver"),
    "test": os.path.join(HOME, "test"),
}

# Bot token file per agent
def get_bot_token_file(agent):
    if agent == "derek":
        return os.path.join(HOME, ".claude", "channels", "telegram", ".env")
    return os.path.join(HOME, ".claude", "channels", f"telegram_{agent}", ".env")

# User chat IDs per agent (their Telegram user)
# These get populated as users onboard — fall back to admin if unknown
USER_CHAT_IDS = {
    # Map agent names to their user's Telegram chat ID
    # Fill these in as users onboard. Falls back to ADMIN_CHAT_ID if None.
    # Example: "my_agent": "123456789",
}

# --- Shared infra paths (admin gate if touched) ---
SHARED_INFRA_PATTERNS = [
    r"/derek/skills/admin-mcp/",          # MCP server, scheduler
    r"/derek/scripts/",                    # infra scripts
    r"mcp-server/server\.js",             # MCP server code
    r"scheduler_executor",                 # scheduler engine
    r"infra_lib\.sh",                      # shared infra lib
    r"refresh_all_agents\.sh",             # token refresh
    r"security_watch\.py",                 # security scanner
    r"agent_health_check\.py",             # health monitoring
]

# --- Classification ---

def classify_action(tool_name, tool_input):
    """
    Returns: "admin", "user", or None (no gate).
    """
    # Never gate these tools
    if tool_name in {"Read", "Glob", "Grep", "Agent", "TaskCreate", "TaskUpdate",
                     "TaskGet", "TaskList", "TaskOutput", "TaskStop",
                     "mcp__plugin_telegram_telegram__react",
                     "mcp__plugin_telegram_telegram__reply",
                     "mcp__plugin_telegram_telegram__edit_message",
                     "ToolSearch", "Skill"}:
        return None

    # Derek manages infra — never gate his own infra operations
    # Admin gate only fires for SUB-AGENTS touching shared infra
    if AGENT_NAME == "derek":
        # Derek only gets user-tier gates (email sends, iMessage)
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")
            if re.search(r"send_email", cmd, re.IGNORECASE):
                return "user"
            return None
        if tool_name == "mcp__plugin_imessage_imessage__reply":
            return "user"
        return None

    my_workspace = AGENT_WORKSPACES.get(AGENT_NAME, "")

    # --- Check Edit/Write for what they're touching ---
    if tool_name in ("Edit", "Write"):
        file_path = tool_input.get("file_path", "")

        # Own workspace = no gate
        if my_workspace and file_path.startswith(my_workspace):
            return None

        # Shared infra = admin gate
        for pattern in SHARED_INFRA_PATTERNS:
            if re.search(pattern, file_path):
                return "admin"

        # Another agent's workspace = admin gate
        for agent, ws in AGENT_WORKSPACES.items():
            if agent != AGENT_NAME and ws and file_path.startswith(ws):
                return "admin"

        # Other files (e.g., own .claude config) = no gate
        return None

    # --- Check Bash commands ---
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")

        # Git push to shared repos = admin
        if re.search(r"git push", cmd, re.IGNORECASE):
            # Push to own repo is fine, push to platform/infra = admin
            if re.search(r"agent-platform|agent-infra", cmd, re.IGNORECASE):
                return "admin"

        # Modifying shared infra via bash
        # Only gate destructive writes — not reads or copies FROM infra
        for pattern in SHARED_INFRA_PATTERNS:
            if re.search(pattern, cmd):
                # Writes: redirect into infra, delete, move, or edit in-place
                if re.search(r"(>\s*" + pattern + r"|>>\s*" + pattern + r"|rm .*" + pattern + r"|mv .*" + pattern + r"|sed -i.*" + pattern + r")", cmd):
                    return "admin"

        # Supabase schema changes = admin
        if re.search(r"CREATE TABLE|ALTER TABLE|DROP TABLE|CREATE INDEX", cmd, re.IGNORECASE):
            return "admin"

        # Email sends = user gate
        if re.search(r"send_email", cmd, re.IGNORECASE):
            return "user"

        # No gate for other bash commands
        return None

    # --- iMessage sends = user gate ---
    if tool_name == "mcp__plugin_imessage_imessage__reply":
        return "user"

    # --- Everything else = no gate ---
    return None


# --- Telegram helpers ---

def get_bot_token():
    token_file = get_bot_token_file(AGENT_NAME)
    try:
        with open(token_file) as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.strip().split("=", 1)[1]
    except FileNotFoundError:
        pass
    return None


def send_telegram(token, chat_id, text):
    data = urllib.parse.urlencode({
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "HTML",
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    return resp.get("result", {}).get("message_id")


def get_updates(token, offset=0):
    data = urllib.parse.urlencode({
        "offset": offset,
        "timeout": 5,
        "allowed_updates": json.dumps(["message"]),
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/getUpdates",
        data=data,
    )
    resp = json.loads(urllib.request.urlopen(req, timeout=15).read())
    return resp.get("result", [])


def check_approval_cache(request_id):
    cache_file = os.path.join(APPROVALS_DIR, f"{request_id}.approved")
    if os.path.exists(cache_file):
        age = time.time() - os.path.getmtime(cache_file)
        if age < 300:
            os.remove(cache_file)
            return True
    return False


def save_approval(request_id):
    os.makedirs(APPROVALS_DIR, exist_ok=True)
    with open(os.path.join(APPROVALS_DIR, f"{request_id}.approved"), "w") as f:
        f.write("approved")


def request_approval(token, chat_id, tier, tool_name, tool_input):
    """Send approval request and poll for response. Returns True (approved) or False (denied)."""
    request_id = hashlib.sha256(
        f"{tool_name}:{json.dumps(tool_input, sort_keys=True)}".encode()
    ).hexdigest()[:12]

    if check_approval_cache(request_id):
        return True

    # Format summary
    if tool_name == "Bash":
        cmd = tool_input.get("command", "")[:200]
        summary = f"<b>Command:</b> <code>{cmd}</code>"
    elif tool_name in ("Edit", "Write"):
        path = tool_input.get("file_path", "")
        summary = f"<b>File:</b> <code>{path}</code>"
    else:
        summary = f"<b>Tool:</b> <code>{tool_name}</code>"

    tier_label = "🔴 ADMIN" if tier == "admin" else "🟡 USER"
    agent_label = f" [{AGENT_NAME}]" if AGENT_NAME != "derek" else ""

    msg = (
        f"{tier_label} <b>Approval Required</b>{agent_label}\n\n"
        f"{summary}\n\n"
        f"Reply <b>yes</b> or <b>no</b>. Auto-denies in {TIMEOUT_SECONDS}s."
    )

    try:
        send_telegram(token, chat_id, msg)
    except Exception:
        return True  # fail open if can't notify

    # Poll for response
    try:
        updates = get_updates(token, offset=0)
        last_update_id = (updates[-1]["update_id"] + 1) if updates else 0
    except Exception:
        last_update_id = 0

    start = time.time()
    while time.time() - start < TIMEOUT_SECONDS:
        try:
            updates = get_updates(token, offset=last_update_id)
            for update in updates:
                last_update_id = update["update_id"] + 1
                msg_obj = update.get("message", {})
                text = msg_obj.get("text", "").strip().lower()
                from_id = str(msg_obj.get("from", {}).get("id", ""))

                if from_id != chat_id:
                    continue

                if text in ("yes", "y", "approve", "ok", "go"):
                    save_approval(request_id)
                    send_telegram(token, chat_id, "✅ Approved.")
                    return True
                elif text in ("no", "n", "deny", "stop", "nope"):
                    send_telegram(token, chat_id, "❌ Denied.")
                    return False
        except Exception:
            pass
        time.sleep(3)

    # Timeout
    try:
        send_telegram(token, chat_id, "⏰ Timed out — denied.")
    except Exception:
        pass
    return False


# --- Main ---

def main():
    raw = sys.stdin.read()
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input", {})

    tier = classify_action(tool_name, tool_input)

    if tier is None:
        sys.exit(0)  # no gate, allow

    token = get_bot_token()
    if not token:
        sys.exit(0)  # fail open

    # Route to the right person
    if tier == "admin":
        chat_id = ADMIN_CHAT_ID
    else:
        chat_id = USER_CHAT_IDS.get(AGENT_NAME) or ADMIN_CHAT_ID  # fall back to admin

    approved = request_approval(token, chat_id, tier, tool_name, tool_input)

    if approved:
        sys.exit(0)  # allow
    else:
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": f"Denied via Telegram ({tier} tier)",
            }
        }, sys.stdout)
        sys.exit(0)


if __name__ == "__main__":
    main()
