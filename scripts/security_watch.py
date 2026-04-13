#!/usr/bin/env python3
"""
Security tripwire monitor for OpenClaw agent.
Usage: python3 security_watch.py [--integrity] [--exec-audit] [--sessions] [--all]
Output: human-readable findings for the agent to report.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

AGENT_NAME = os.environ.get("AGENT_NAME", "agent")
WORKSPACE = Path.home() / AGENT_NAME
SESSIONS_FILE = Path.home() / ".claude" / "sessions" / "sessions.json"
LOG_FILE = Path.home() / "logs" / f"agent-{datetime.now().strftime('%Y-%m-%d')}.log"
EXPECTED_TELEGRAM_USER = os.environ.get("EXPECTED_TELEGRAM_USER", "")
CANARY = "SECURITY-CANARY:7f3a9b2e-4c1d-8f6a-b5e7-2d3c9a1f4e8b"
CRITICAL_FILES = ["AGENTS.md", "SOUL.md", "IDENTITY.md", "USER.md", "MEMORY.md"]

# Exec patterns that are suspicious
SUSPICIOUS_EXEC = [
    r"rm\s+-[rf]",
    r"chmod\s+[0-7]*7[0-7]*",          # world-writable
    r"curl.*(\.onion|pastebin|ngrok)",
    r"wget.*(\.onion|pastebin|ngrok)",
    r"base64.*\|.*sh",
    r"eval\s*\(",
    r"python.*-c.*exec",
    r">\s*/etc/",
    r">\s*/root/",
    r"ssh-keygen",
    r"authorized_keys",
    r"\.ssh/",
    r"exfil",
    r"/proc/",
]

alerts = []
clean = []


def run(cmd, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, timeout=10)
        return r.stdout.strip(), r.stderr.strip(), r.returncode
    except Exception as e:
        return "", str(e), 1


def check_integrity():
    """Verify workspace critical files haven't been tampered with."""

    # 1. Canary check
    agents_md = WORKSPACE / "AGENTS.md"
    if agents_md.exists():
        content = agents_md.read_text()
        if CANARY not in content:
            alerts.append("🚨 CANARY MISSING from AGENTS.md — file may have been tampered with")
        else:
            clean.append("✓ Canary present in AGENTS.md")
    else:
        alerts.append("🚨 AGENTS.md is missing entirely")

    # 2. Unstaged changes to critical files
    stdout, _, rc = run(["git", "status", "--porcelain", "--", *CRITICAL_FILES], cwd=WORKSPACE)
    if stdout:
        lines = [l for l in stdout.splitlines() if l.strip()]
        if lines:
            alerts.append(f"⚠️  Uncommitted changes to critical files:\n" + "\n".join(f"  {l}" for l in lines))
        else:
            clean.append("✓ No uncommitted changes to critical files")
    else:
        clean.append("✓ No uncommitted changes to critical files")

    # 3. Recent commits to critical files (last 2 hours)
    since = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S")
    stdout, _, rc = run(
        ["git", "log", f"--since={since}", "--name-only", "--pretty=format:%h %s", "--", *CRITICAL_FILES],
        cwd=WORKSPACE
    )
    if stdout.strip():
        alerts.append(f"⚠️  Critical files changed in last 2h (verify these were intentional):\n{stdout[:500]}")
    else:
        clean.append("✓ No critical file commits in last 2h")

    # 4. Unknown files in workspace root
    stdout, _, _ = run(["git", "ls-files", "--others", "--exclude-standard"], cwd=WORKSPACE)
    unknown = [f for f in stdout.splitlines() if f.strip() and not f.startswith("memory/")]
    if unknown:
        alerts.append(f"⚠️  Unknown untracked files in workspace:\n" + "\n".join(f"  {f}" for f in unknown[:10]))
    else:
        clean.append("✓ No unexpected untracked files")


def check_exec_audit():
    """Scan recent logs for suspicious exec/tool calls."""
    if not LOG_FILE.exists():
        clean.append("✓ No log file found for today (nothing to audit)")
        return

    suspicious_found = []
    content = LOG_FILE.read_text(errors="replace")

    # Look for tool_use exec blocks in JSONL entries
    for line in content.splitlines():
        if "tool_use" not in line and "exec" not in line.lower():
            continue
        try:
            d = json.loads(line)
            msg = str(d.get("1", ""))
            if "exec" in msg.lower():
                for pattern in SUSPICIOUS_EXEC:
                    if re.search(pattern, msg, re.IGNORECASE):
                        suspicious_found.append(f"  Pattern '{pattern}' in: {msg[:200]}")
        except Exception:
            # Raw text fallback
            for pattern in SUSPICIOUS_EXEC:
                if re.search(pattern, line, re.IGNORECASE):
                    suspicious_found.append(f"  Pattern '{pattern}' in log line")
                    break

    if suspicious_found:
        alerts.append("🚨 Suspicious exec patterns detected in logs:\n" + "\n".join(suspicious_found[:10]))
    else:
        clean.append("✓ No suspicious exec patterns in today's logs")


def check_sessions():
    """Check for sessions from unexpected origins."""
    if not SESSIONS_FILE.exists():
        clean.append("✓ No sessions file found")
        return

    with open(SESSIONS_FILE) as f:
        sessions = json.load(f)

    unexpected = []
    for key, val in sessions.items():
        if key.startswith("telegram:slash:"): continue  # slash commands are fine
        origin = val.get("origin", {})
        provider = origin.get("provider", "")
        from_id = origin.get("from", "")

        # Flag non-telegram sessions or unexpected telegram users
        if provider == "telegram":
            uid = from_id.replace("telegram:", "")
            if uid and uid != EXPECTED_TELEGRAM_USER:
                unexpected.append(f"  Unexpected Telegram user: {uid} in session '{key}'")
        elif provider in ("webchat", "heartbeat", "cron", "cron-event"):
            pass  # webchat/local, heartbeat, and cron are fine
        elif provider:
            unexpected.append(f"  Unexpected provider '{provider}' in session '{key}'")

    if unexpected:
        alerts.append("🚨 Unexpected session origins:\n" + "\n".join(unexpected))
    else:
        clean.append("✓ All sessions from expected origins")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--integrity", action="store_true")
    parser.add_argument("--exec-audit", action="store_true")
    parser.add_argument("--sessions", action="store_true")
    parser.add_argument("--all", action="store_true")
    args = parser.parse_args()

    run_all = args.all or not any([args.integrity, args.exec_audit, args.sessions])

    if args.integrity or run_all:
        check_integrity()
    if args.exec_audit or run_all:
        check_exec_audit()
    if args.sessions or run_all:
        check_sessions()

    ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    if alerts:
        print(f"🔴 SECURITY ALERT [{ts}]")
        for a in alerts:
            print(a)
        if clean:
            print("\nChecks passed:")
            for c in clean:
                print(f"  {c}")
        sys.exit(1)
    else:
        print(f"🟢 SECURITY CLEAN [{ts}]")
        for c in clean:
            print(f"  {c}")
        sys.exit(0)


if __name__ == "__main__":
    main()
