#!/usr/bin/env python3
"""Fleet liveness probe — Phase plan: observability workstream.

Runs every 2 min via launchd (com.agent-liveness-probe.plist). For each agent in
agents.json, runs three cheap local checks:

  1. Claude daemon alive        — pgrep for `claude.*<workspace>.*--channels`
  2. Telegram plugin spawned    — pgrep -P <claude_pid> finds bun.*telegram.*start
  3. Tmux pane semantic match   — capture shows "Listening for channel messages"
                                  and none of the stuck-wizard signatures

Writes one row to Supabase `infra_events` per agent per tick. Level INFO for
healthy, WARNING for any failure class. component="liveness:<agent>" so the
existing dashboard MONITORS / EventsPage surfaces pick it up for free.

Sends a Telegram alert to admin (chat_id YOUR_TELEGRAM_CHAT_ID) on failure, deduped to one
alert per agent per 6h via touch-file at /tmp/agent-liveness-alerts/<agent>.

No arguments. Exit 0 regardless of individual agent outcomes (launchd cadence
should never be disrupted by a single flaky agent).
"""
import json
import os
import pathlib
import plistlib
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import urllib.error

AGENTS_JSON = "/Users/YOUR_MAC_USERNAME/derek/agent-infra/agents.json"
DEDUPE_DIR = pathlib.Path("/tmp/agent-liveness-alerts")
DEDUPE_SECS = 6 * 3600  # 6 hours
ADMIN_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"
ADMIN_BOT_ACCESS = "/Users/YOUR_MAC_USERNAME/.claude/channels/telegram_admin/access.json"

# Stuck-wizard phrases — presence means agent is at an onboarding prompt, not serving
STUCK_WIZARD_PHRASES = [
    "Choose the text style",
    "Paste code here",
    "Select login method",
    "login method:",
    "Claude account with subscription",
]
LISTENING_SIGNATURE = "Listening for channel messages"

# `/rate-limit-options` modal — appears when Claude Code hits its 5h quota.
# It's a blocking dialog; even after the reset time passes, the agent stays
# frozen at the menu until someone sends Escape.
#
# Only the menu's own chrome is a reliable signal. Phrases like "You've hit
# your limit" persist in scrollback after the menu is dismissed, so matching
# on them alone produces false positives (nate, 2026-04-24: menu had already
# been dismissed but the status line remained visible). Require BOTH menu-body
# text ("Stop and wait for limit to reset") AND menu-footer text ("Enter to
# confirm · Esc to cancel") — only the live dialog shows both simultaneously.
RATE_LIMIT_MENU_BODY = "Stop and wait for limit to reset"
RATE_LIMIT_MENU_FOOTER = "Enter to confirm"
# Phrases we use for reset-time parsing (present both in the active menu and
# in scrollback right after a limit hit — fine for time extraction only).
RATE_LIMIT_HIT_PHRASE = "You've hit your limit"
# Anthropic prints these reset-time formats:
#   "resets 1pm (America/Los_Angeles)"           — 5h, no minutes (top of hour)
#   "resets 12:50pm (America/Los_Angeles)"       — 5h, with minutes
#   "resets May 1 at 6am (America/Los_Angeles)"  — weekly, date-prefixed
# The regex makes the date prefix and the :MM both optional.
RATE_LIMIT_RESET_RE = re.compile(
    r"resets\s+(?:(?P<date>[A-Za-z]+\s+\d{1,2})\s+at\s+)?"
    r"(?P<time>\d{1,2}(?::\d{2})?(?:am|pm))\s*\((?P<tz>[^)]+)\)",
    re.IGNORECASE,
)


def load_supabase_env():
    """Load SUPABASE_URL + SUPABASE_SERVICE_KEY from env or vera's plist."""
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        return os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    plist = "/Users/YOUR_MAC_USERNAME/Library/LaunchAgents/com.vera-agent.daemon.plist"
    with open(plist, "rb") as f:
        ev = plistlib.load(f).get("EnvironmentVariables", {})
    return ev["SUPABASE_URL"], ev["SUPABASE_SERVICE_KEY"]


def run(cmd, timeout=5):
    """Run a shell command, return stdout (decoded) or '' on any failure."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except Exception:
        return ""


def probe_daemon_pid(workspace):
    """Return the PID of claude-code daemon for this workspace, or None."""
    out = run(["pgrep", "-f", f"claude.*{workspace}.*--channels"])
    pids = [int(p) for p in out.split() if p.strip()]
    return pids[0] if pids else None


def probe_bun_plugin(claude_pid):
    """Return True if a bun telegram plugin subprocess exists under claude_pid."""
    if not claude_pid:
        return False
    out = run(["pgrep", "-P", str(claude_pid)])
    child_pids = [int(p) for p in out.split() if p.strip()]
    for cpid in child_pids:
        # ps -o command= returns the full command line
        cmd = run(["ps", "-o", "command=", "-p", str(cpid)]).strip()
        if "bun" in cmd and "telegram" in cmd and "start" in cmd:
            return True
    return False


def probe_tmux_pane(tmux_session):
    """Return (has_listening, matched_wizard_phrase_or_None, raw_pane_text_or_None)."""
    out = run(["/opt/homebrew/bin/tmux", "capture-pane", "-t", tmux_session, "-S", "-200", "-p"], timeout=8)
    if not out:
        return None, None, None
    has_listening = LISTENING_SIGNATURE in out
    wizard = next((p for p in STUCK_WIZARD_PHRASES if p in out), None)
    return has_listening, wizard, out


def detect_rate_limit_menu(pane_text):
    """Return dict describing a rate-limit menu state, or None if not present.

    Fields:
      present: bool
      reset_local: str|None  (e.g. '12:50pm')
      reset_tz:    str|None  (e.g. 'America/Los_Angeles')
      reset_passed: bool     True if we can confirm the reset time is in the past
    """
    if not pane_text:
        return None
    # Require BOTH menu-body and menu-footer to be present — that's unique
    # to the live modal. Either alone can linger in scrollback after dismissal.
    if RATE_LIMIT_MENU_BODY not in pane_text or RATE_LIMIT_MENU_FOOTER not in pane_text:
        return None
    # Take the LAST "resets ..." in pane text (older hits may be in scrollback).
    matches = list(RATE_LIMIT_RESET_RE.finditer(pane_text))
    m = matches[-1] if matches else None
    info = {
        "present": True,
        "reset_local": m.group("time") if m else None,
        "reset_tz": m.group("tz") if m else None,
        "reset_date": m.group("date") if m else None,
        "reset_passed": False,
    }
    if m:
        info["reset_passed"] = _reset_is_in_the_past(
            m.group("time"), m.group("tz"), m.group("date")
        )
    return info


def _reset_is_in_the_past(local_hm, tz_name, date_str=None):
    """Return True if the parsed reset wall-clock has already passed in tz.

    Accepts:
      local_hm: '1pm' | '1:30pm' | '12:50am' (minutes optional)
      tz_name:  'America/Los_Angeles'
      date_str: 'May 1' (optional; for weekly resets). None means "today".

    Without a date: assume TODAY in tz; if the computed instant is more than
    12h in the future we rewind a day (probe fires shortly after midnight and
    reset was earlier today).

    With a date: assume current year first; if computed instant is more than
    180 days in the past, advance one year (handles year-wrap on Dec→Jan).
    """
    try:
        from datetime import datetime, timedelta
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tz_name)
        except Exception:
            return False  # unknown tz — err on the side of "don't auto-recover"
        now_tz = datetime.now(tz)
        local_hm = local_hm.lower()
        time_fmt = "%I:%M%p" if ":" in local_hm else "%I%p"
        hm = datetime.strptime(local_hm, time_fmt).time()
        if date_str:
            try:
                md = datetime.strptime(date_str, "%b %d")
            except ValueError:
                md = datetime.strptime(date_str, "%B %d")
            candidate = now_tz.replace(
                year=now_tz.year, month=md.month, day=md.day,
                hour=hm.hour, minute=hm.minute, second=0, microsecond=0,
            )
            if candidate < now_tz - timedelta(days=180):
                candidate = candidate.replace(year=now_tz.year + 1)
        else:
            candidate = now_tz.replace(
                hour=hm.hour, minute=hm.minute, second=0, microsecond=0,
            )
            if candidate > now_tz + timedelta(hours=12):
                candidate -= timedelta(days=1)
        return candidate <= now_tz
    except Exception:
        return False


def attempt_rate_limit_recovery(tmux_session):
    """Send Escape to dismiss a blocking /rate-limit-options menu. Returns True
    if the send-keys command succeeded (doesn't guarantee the menu actually went
    away — the next tick will verify)."""
    try:
        r = subprocess.run(
            ["/opt/homebrew/bin/tmux", "send-keys", "-t", tmux_session, "Escape"],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0
    except Exception:
        return False


def classify(daemon_pid, bun_ok, tmux_listening, tmux_wizard, tmux_read_ok, rate_limit):
    """Return (status, short_reason) from the signals.

    Process tree is authoritative: daemon alive + bun alive = plugin is serving.
    tmux is only used to catch failure modes where process tree looks fine but
    the agent isn't actually working — stuck wizards and rate-limit menus.
    Signals with explicit ground-truth take priority over process checks.
    """
    if not daemon_pid:
        return "down", "no claude --channels process found"
    # Wizard is ground-truth stuck even if daemon+bun look fine
    if tmux_read_ok and tmux_wizard:
        return "stuck", f"tmux shows wizard signature: '{tmux_wizard}'"
    # Rate-limit menu is another blocking-modal case. If the reset time has
    # passed, mark recoverable so the main loop can auto-dismiss. Otherwise
    # mark as waiting — no action, just visibility.
    if tmux_read_ok and rate_limit and rate_limit.get("present"):
        if rate_limit.get("reset_passed"):
            return "rate_limited_recoverable", (
                f"/rate-limit-options menu blocking; reset "
                f"({rate_limit.get('reset_local','?')} {rate_limit.get('reset_tz','?')}) already passed"
            )
        return "rate_limited", (
            f"at /rate-limit-options; resets {rate_limit.get('reset_local','?')} "
            f"{rate_limit.get('reset_tz','?')}"
        )
    if not bun_ok:
        return "plugin_not_spawned", "daemon running but no bun telegram subprocess"
    return "up", "daemon + plugin alive" + (" (tmux banner visible)" if tmux_listening else " (tmux pane quiet)")


def insert_infra_event(base, key, agent, status, reason, signals):
    """Insert one row into Supabase infra_events. Return success bool."""
    level = "INFO" if status == "up" else ("WARNING" if status != "unknown" else "INFO")
    body = {
        "level": level,
        "component": f"liveness:{agent}",
        "message": f"{agent} liveness={status}: {reason}",
        "metadata": {"agent": agent, "status": status, "signals": signals},
    }
    req = urllib.request.Request(
        f"{base}/rest/v1/infra_events",
        data=json.dumps(body).encode(),
        method="POST",
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except urllib.error.HTTPError as e:
        sys.stderr.write(f"infra_events insert failed for {agent}: HTTP {e.code} — {e.read().decode()[:200]}\n")
        return False
    except Exception as e:
        sys.stderr.write(f"infra_events insert failed for {agent}: {e}\n")
        return False


def send_telegram_alert(bot_token, agent, status, reason, signals):
    """Fire one Telegram alert to admin. Return success bool."""
    emoji = {"stuck": "🟡", "plugin_not_spawned": "🔴", "down": "🔴", "unknown": "⚪"}.get(status, "🔴")
    compact_signals = ", ".join(f"{k}={v}" for k, v in signals.items() if v is not None and v != "")
    text = f"{emoji} Liveness alert — {agent} status={status}\n{reason}\nSignals: {compact_signals}"
    try:
        data = urllib.parse.urlencode({"chat_id": ADMIN_CHAT_ID, "text": text}).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        sys.stderr.write(f"telegram alert failed for {agent}: {e}\n")
        return False


def should_alert(agent):
    """Return True if the per-agent dedupe file is missing or older than 6h."""
    DEDUPE_DIR.mkdir(parents=True, exist_ok=True)
    p = DEDUPE_DIR / agent
    if not p.exists():
        return True
    return (time.time() - p.stat().st_mtime) >= DEDUPE_SECS


def mark_alerted(agent):
    (DEDUPE_DIR / agent).touch()


def main():
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(f"[{ts}] liveness probe starting")

    try:
        base, key = load_supabase_env()
    except Exception as e:
        sys.stderr.write(f"env load failed: {e}\n")
        sys.exit(2)

    # Admin bot token for alerts
    bot_token = None
    try:
        access = json.load(open(ADMIN_BOT_ACCESS))
        bot_token = access.get("botToken")
    except Exception as e:
        sys.stderr.write(f"admin bot token not loadable: {e}\n")

    # Read agent registry
    try:
        reg = json.load(open(AGENTS_JSON))["agents"]
    except Exception as e:
        sys.stderr.write(f"agents.json not loadable: {e}\n")
        sys.exit(2)

    results = []
    for agent, info in reg.items():
        workspace = info.get("workspace", f"/Users/YOUR_MAC_USERNAME/{agent}")
        tmux_session = info.get("tmux_session", f"{agent}-agent")

        daemon_pid = probe_daemon_pid(workspace)
        bun_ok = probe_bun_plugin(daemon_pid)
        tmux_listening, tmux_wizard, raw = probe_tmux_pane(tmux_session)
        tmux_read_ok = raw is not None
        rate_limit = detect_rate_limit_menu(raw) if tmux_read_ok else None

        status, reason = classify(daemon_pid, bun_ok, tmux_listening, tmux_wizard, tmux_read_ok, rate_limit)
        signals = {
            "daemon_pid": daemon_pid,
            "bun_running": bun_ok,
            "tmux_listening": tmux_listening,
            "tmux_wizard": tmux_wizard,
            "rate_limit_menu": rate_limit,
        }

        # Auto-recover: reset time has passed but the agent is still sitting
        # on the blocking menu. Send Escape to dismiss — next tick will verify.
        if status == "rate_limited_recoverable":
            sent = attempt_rate_limit_recovery(tmux_session)
            signals["auto_escape_sent"] = sent
            reason += f" — auto-Escape {'sent' if sent else 'FAILED'}"

        # Log to stdout for the LaunchAgent log file
        print(f"  {agent}: {status} — {reason}  signals={signals}")

        # Write every tick to infra_events (INFO when up, WARNING otherwise)
        insert_infra_event(base, key, agent, status, reason, signals)

        # Alert only on non-up + non-unknown, with 6h dedupe
        if status not in ("up", "unknown") and bot_token:
            if should_alert(agent):
                if send_telegram_alert(bot_token, agent, status, reason, signals):
                    mark_alerted(agent)
                    print(f"  {agent}: alert sent")
            else:
                print(f"  {agent}: alert suppressed (deduped)")

        results.append((agent, status))

    summary = {a: s for a, s in results}
    print(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] liveness probe done: {summary}")


if __name__ == "__main__":
    main()
