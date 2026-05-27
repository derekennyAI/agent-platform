#!/usr/bin/env python3
"""Phase 2.4 — PreToolUse hook: block direct Write/Edit on memory/*.md files.

Agents that have been migrated to DB-backed memory (Phase 2.5) should use the
mcp__admin-control__store_memory tool instead — it validates the row, writes
to the agent_memories Supabase table, and the file appears via the projector
cron within 15 min. Direct Write/Edit would just be overwritten on the next
projection, causing silent data loss.

Allowed exceptions:
  - Farlen's session (CLAUDE_CONFIG_DIR ends in claude-farlen) — above the gate.
  - NotebookEdit on non-.md paths.

Failure mode: hook fails open (sys.exit(0)) on any unexpected input. Missing
frontmatter / bad paths / parse errors should never block real work.

Install per-agent (only for agents that have been migrated) via
~/.claude-<agent>/settings.json PreToolUse entry.
"""
import json
import os
import re
import sys


BLOCKED_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

# Match any agent's workspace memory dir — this is a fleet-wide convention.
MEMORY_PATH_RE = re.compile(
    r"^/Users/YOUR_MAC_USERNAME/(?:admin|derekPersonal|dereklm|vera|nate|macgyver)/memory/[^/]+\.md$"
)

REMEDIATION = (
    "BLOCKED: memory/*.md files are projected from the agent_memories "
    "Supabase table. A direct Write/Edit will be overwritten within 15 min "
    "when the projector runs. Use the mcp__admin-control__store_memory MCP "
    "tool instead — it validates, writes to DB, and the file appears via "
    "projection. For MEMORY.md specifically: never edit — it's auto-"
    "regenerated from the index. To rename/revise an existing memory, use "
    "mcp__admin-control__supersede_memory after store_memory."
)


def main():
    # Farlen's interactive session is above this gate (matches the approval-gate
    # pattern). Fleet agents each run with their own CLAUDE_CONFIG_DIR.
    if os.environ.get("CLAUDE_CONFIG_DIR", "").endswith("claude-farlen"):
        sys.exit(0)

    try:
        data = json.loads(sys.stdin.read())
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = data.get("tool_name", "")
    if tool_name not in BLOCKED_TOOLS:
        sys.exit(0)

    tool_input = data.get("tool_input", {}) or {}
    fp = tool_input.get("file_path") or tool_input.get("notebook_path") or ""

    if MEMORY_PATH_RE.match(fp):
        json.dump({
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": REMEDIATION,
            }
        }, sys.stdout)

    sys.exit(0)


if __name__ == "__main__":
    main()
