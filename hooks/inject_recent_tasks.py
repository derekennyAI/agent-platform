#!/usr/bin/env python3
"""UserPromptSubmit hook: inject recent push_tasks.log tail as context.

Why: scheduled tasks fire as `claude -p` one-shots in separate processes.
Their full transcript and output go to ~/<workspace>/push_tasks.log, but the
live `--channels` session never sees them. Result: when a user says "the
four topics you sent this morning," the live agent has no record of having
sent them.

This hook runs on every UserPromptSubmit and prepends the last ~8KB of
push_tasks.log as a `<scheduled_task_history>` block in additionalContext.
The agent then has deterministic visibility into recent autonomous work
without any prompt-level instruction to look.

Failure mode by design: any error → silent exit 0 → no context injected →
the agent proceeds as it does today (without scheduled-task awareness for
that turn). The hook strictly cannot make things worse.
"""
import json
import os
import signal
import sys
from pathlib import Path

MAX_BYTES = 8000
MAX_LOG_SIZE = 100 * 1024 * 1024   # skip if log > 100MB (probably runaway)
HARD_TIMEOUT_SECONDS = 1            # watchdog — abort if hook hangs

# Workspace name often equals agent name; derek personal is the exception.
AGENT_TO_WORKSPACE = {
    "derek": "derekPersonal",
}


def _silent_exit(*_args):
    sys.exit(0)


def _resolve_log_path():
    agent = (os.environ.get("AGENT_NAME") or "").strip()
    if not agent:
        return None
    workspace = AGENT_TO_WORKSPACE.get(agent, agent)
    return Path.home() / workspace / "push_tasks.log"


def _read_tail(log_path):
    """Read the last MAX_BYTES of the file, discarding any partial leading
    line. Returns text decoded with replacement on bad bytes."""
    size = log_path.stat().st_size
    if size == 0 or size > MAX_LOG_SIZE:
        return ""
    with log_path.open("rb") as f:
        if size > MAX_BYTES:
            f.seek(size - MAX_BYTES)
            f.readline()  # discard the partial first line
        data = f.read()
    return data.decode(errors="replace").strip()


def main():
    # Watchdog: if anything blocks for more than HARD_TIMEOUT_SECONDS, abort.
    signal.signal(signal.SIGALRM, _silent_exit)
    signal.alarm(HARD_TIMEOUT_SECONDS)

    # Drain stdin (UserPromptSubmit hooks receive a JSON event payload).
    try:
        sys.stdin.read()
    except Exception:
        pass

    log_path = _resolve_log_path()
    if not log_path or not log_path.exists():
        return  # nothing to inject; silent no-op

    try:
        tail = _read_tail(log_path)
    except Exception:
        return

    if not tail:
        return

    additional = (
        "<scheduled_task_history>\n"
        "Tail of your push_tasks.log — output from scheduled-task one-shots.\n"
        "Your live session does NOT contain transcripts of these one-shots; this\n"
        "block does. If the user references work you don't immediately recall\n"
        "(\"the four topics,\" \"this morning's report,\" \"the items you sent\"),\n"
        "look here first before responding.\n"
        "\n"
        f"{tail}\n"
        "</scheduled_task_history>"
    )

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional,
        }
    }
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Belt-and-suspenders: any uncaught exception → silent exit 0.
        sys.exit(0)
