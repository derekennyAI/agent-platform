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
from datetime import datetime, timezone
from pathlib import Path

MAX_BYTES = 8000
MAX_LOG_SIZE = 100 * 1024 * 1024   # skip if log > 100MB (probably runaway)
HARD_TIMEOUT_SECONDS = 1            # watchdog — abort if hook hangs
MAX_SENT_MESSAGES = 15             # most recent outbound messages to inject

# Workspace name often equals agent name; derek personal is the exception.
AGENT_TO_WORKSPACE = {
    "derek": "derekPersonal",
}

# TELEGRAM_STATE_DIR name often equals agent name; derek uses the legacy "test".
AGENT_TO_TELEGRAM_STATE = {
    "derek": "test",
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


def _sent_messages_path():
    """Path to the telegram plugin's outbound message log (sent-messages.jsonl).
    This captures the verbatim text of every message the bot sent — including
    scheduled-task one-shots, whose transcripts the live session never sees."""
    agent = (os.environ.get("AGENT_NAME") or "").strip()
    if not agent:
        return None
    state_name = AGENT_TO_TELEGRAM_STATE.get(agent, agent)
    return Path.home() / ".claude" / "channels" / f"telegram_{state_name}" / "sent-messages.jsonl"


def _read_recent_sent():
    """Return formatted recent outbound messages, or '' if none."""
    import json as _json
    path = _sent_messages_path()
    if not path or not path.exists():
        return ""
    size = path.stat().st_size
    if size == 0 or size > MAX_LOG_SIZE:
        return ""
    # Read last 32KB (plenty for MAX_SENT_MESSAGES), keep the last N JSON lines.
    with path.open("rb") as f:
        if size > 32000:
            f.seek(size - 32000)
            f.readline()
        lines = f.read().decode(errors="replace").splitlines()
    out = []
    for line in lines[-MAX_SENT_MESSAGES:]:
        line = line.strip()
        if not line:
            continue
        try:
            rec = _json.loads(line)
            ts = rec.get("ts", "")[:19]
            text = (rec.get("text") or "").strip()
            if text:
                out.append(f"[{ts}] {text}")
        except Exception:
            continue
    return "\n\n".join(out)


def main():
    # Watchdog: if anything blocks for more than HARD_TIMEOUT_SECONDS, abort.
    signal.signal(signal.SIGALRM, _silent_exit)
    signal.alarm(HARD_TIMEOUT_SECONDS)

    # Drain stdin (UserPromptSubmit hooks receive a JSON event payload).
    try:
        sys.stdin.read()
    except Exception:
        pass

    tail = ""
    log_path = _resolve_log_path()
    if log_path and log_path.exists():
        try:
            tail = _read_tail(log_path)
        except Exception:
            pass

    now_utc = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

    parts = [f"<current_time>{now_utc}</current_time>"]

    try:
        sent = _read_recent_sent()
    except Exception:
        sent = ""

    if sent:
        parts.append(
            "<recent_messages_you_sent>\n"
            "Verbatim text of recent messages your bot sent to the user — INCLUDING\n"
            "messages sent by scheduled-task one-shots running in separate processes\n"
            "(jokes, reports, reminders). Your live session does NOT contain these in\n"
            "its transcript. If the user references something \"you\" sent that you don't\n"
            "recall (\"the joke,\" \"that report,\" \"what you just sent\"), the actual text\n"
            "is here — use it, don't claim you can't see it.\n"
            "\n"
            f"{sent}\n"
            "</recent_messages_you_sent>"
        )

    if tail:
        parts.append(
            "<scheduled_task_history>\n"
            "Tail of your push_tasks.log — status/summary from scheduled-task one-shots.\n"
            "Shows which tasks ran and their exit status (actual sent content is in\n"
            "<recent_messages_you_sent> above). If the user references work you don't\n"
            "immediately recall, look here for what ran and when.\n"
            "\n"
            f"{tail}\n"
            "</scheduled_task_history>"
        )

    payload = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }
    sys.stdout.write(json.dumps(payload))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Belt-and-suspenders: any uncaught exception → silent exit 0.
        sys.exit(0)
