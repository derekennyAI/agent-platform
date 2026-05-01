#!/usr/bin/env python3
"""
Agent Health Check — monitors all sub-agent tmux sessions, logs to Supabase infra_events,
applies intervention rules (auto-restart, escalation alerts).

Usage:
    python3 agent_health_check.py              # check all agents
    python3 agent_health_check.py --json       # output JSON summary
    python3 agent_health_check.py --dashboard  # generate dashboard HTML
"""

import subprocess, json, os, sys, time, urllib.request, random, string
from datetime import datetime, timezone

sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/agent-infra/shared")
import registry  # type: ignore
import task_actions as ta  # type: ignore

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TMUX = "/opt/homebrew/bin/tmux"

# ──────────────────────────────────────────────────────────────
# Flood-fix guardrails (plan glowing-rolling-plum.md 2026-04-24)
# ──────────────────────────────────────────────────────────────
# The 544-task flood happened because:
#   - check_cron_staleness considered ALL active tasks (including pull); pull
#     tasks go stale when agents are idle, which is working-as-designed.
#   - No dedupe on admin_task creation — every 15-min cron run created a new
#     fix_mcp_scheduler row, even for the same agent already flagged.
#   - No global cap on pending rows.
# All three are fixed below.
HEALTH_ALERT_DIR = "/Users/YOUR_MAC_USERNAME/derek/logs/health_alert_state"
HEALTH_ALERT_DEDUPE_SECS = 6 * 3600   # 6 hours between fix_mcp_scheduler alerts per agent
HEALTH_ALERT_CAP = 20                 # max pending fix_mcp_scheduler rows fleet-wide
SCHEDULER_LIVENESS_WINDOW_SEC = 300   # scheduler must have logged within 5 min
INFRA_LOG = "/Users/YOUR_MAC_USERNAME/derek/logs/infra.log"
os.makedirs(HEALTH_ALERT_DIR, exist_ok=True)


def _build_agents():
    out = []
    for name, cfg in registry.load_agents().items():
        ws = cfg.get("workspace", f"/Users/YOUR_MAC_USERNAME/{name}")
        # Derek's daemon log lives in ~/.claude/, not ~/derek/
        daemon_log = (
            "/Users/YOUR_MAC_USERNAME/.claude/daemon.log" if name == "derek"
            else f"{ws}/daemon.log"
        )
        creds = (
            f"{cfg['config_dir']}/.credentials.json"
            if cfg.get("has_pro_token") and cfg.get("config_dir")
            else None
        )
        out.append({
            "name": name,
            "tmux": cfg.get("tmux_session", f"{name}-agent"),
            "workspace": ws,
            "daemon_log": daemon_log,
            "credentials": creds,
            "auto_restart": cfg.get("auto_restart", True),
            "max_consecutive_down": cfg.get("max_consecutive_down", 2),
        })
    return out


AGENTS = _build_agents()

TELEGRAM_CHAT_ID = "YOUR_TELEGRAM_CHAT_ID"
TELEGRAM_BOT_TOKEN_FILE = os.path.expanduser("~/.claude/channels/telegram/.env")

# Intervention rules (per-agent overrides via registry fields)
MAX_CONSECUTIVE_DOWN = 2  # default; can be overridden per-agent via registry max_consecutive_down
AUTO_RESTART_AGENTS = [
    a["name"] for a in AGENTS if a["auto_restart"] and a["name"] != "derek"
]  # derek cannot self-restart
MAX_CONSECUTIVE_RESTARTS = 3  # after this many restarts without recovery, stop and task Derek
STATE_FILE = os.path.expanduser("~/derek/logs/agent_health_state.json")


def get_telegram_token():
    """Read bot token from .env file."""
    try:
        with open(TELEGRAM_BOT_TOKEN_FILE) as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return None


def send_telegram(message):
    """Send a Telegram message to Farlen."""
    token = get_telegram_token()
    if not token:
        print("[warn] No Telegram bot token found", file=sys.stderr)
        return
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": message}).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[warn] Telegram send failed: {e}", file=sys.stderr)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def check_tmux_session(session_name):
    """Check if a tmux session is running and capture recent output."""
    try:
        subprocess.run(
            [TMUX, "has-session", "-t", session_name],
            capture_output=True, timeout=5
        )
        # Session exists — capture last 5 lines for error detection
        result = subprocess.run(
            [TMUX, "capture-pane", "-t", session_name, "-p", "-l", "10"],
            capture_output=True, text=True, timeout=5
        )
        return {"running": True, "output": result.stdout.strip()}
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return {"running": False, "output": ""}


def detect_errors(output):
    """Scan tmux output for error patterns."""
    errors = []
    error_patterns = [
        "Error:", "ERROR", "FATAL", "panic:", "Traceback",
        "SIGTERM", "SIGKILL", "killed", "crashed",
        "rate limit", "429", "quota exceeded",
        "connection refused", "ECONNREFUSED",
        "authentication failed", "401 Unauthorized",
        "MCP server disconnected", "spawn error",
    ]
    for line in output.split("\n"):
        line_lower = line.lower()
        for pattern in error_patterns:
            if pattern.lower() in line_lower:
                errors.append(line.strip()[:200])
                break
    return errors


def check_ready_file(agent):
    """
    Check if agent completed startup (.ready file exists).
    Returns (is_ready, minutes_since_start).
    Startup stuck = running >10min with no .ready file.
    """
    ready_path = os.path.join(agent["workspace"], ".ready")
    is_ready = os.path.exists(ready_path)
    if is_ready:
        return True, None

    # Not ready — find out how long the daemon has been running
    daemon_log = agent.get("daemon_log")
    if not daemon_log or not os.path.exists(daemon_log):
        return False, None
    try:
        with open(daemon_log) as f:
            lines = f.readlines()
        for line in reversed(lines):
            if "Session started successfully" in line:
                ts_str = line.split(" | ")[0].strip()
                started = datetime.fromisoformat(ts_str)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=timezone.utc)
                minutes = (datetime.now(timezone.utc) - started).total_seconds() / 60
                return False, minutes
    except Exception:
        pass
    return False, None


def check_input_flood(agent, threshold=6):
    """
    Check if SCHEDULED TASK / HEARTBEAT messages are flooding the tmux input buffer.
    When an agent is busy, tasks pile up as queued text and push the prompt marker
    off-screen — the scheduler then thinks the agent is perpetually busy and stalls.
    Returns the count of consecutive task injection lines at the END of the pane.
    """
    try:
        result = subprocess.run(
            [TMUX, "capture-pane", "-t", agent["tmux"], "-p", "-S", "-30"],
            capture_output=True, text=True, timeout=5
        )
        lines = [l for l in result.stdout.split("\n") if l.strip()]
        consecutive = 0
        for line in reversed(lines[-20:]):
            if "SCHEDULED TASK" in line or "HEARTBEAT" in line:
                consecutive += 1
            else:
                break
        return consecutive
    except Exception:
        return 0


def _cron_field_match(field, value, min_v, max_v):
    """Match a single cron-field component (handles '*', 'a-b', '*/n', 'a-b/n', 'a,b,c')."""
    for part in field.split(','):
        if part == '*':
            return True
        if '/' in part:
            range_part, step_str = part.split('/', 1)
            step = int(step_str)
            if range_part == '*':
                lo, hi = min_v, max_v
            elif '-' in range_part:
                lo, hi = map(int, range_part.split('-', 1))
            else:
                lo, hi = int(range_part), max_v
            if lo <= value <= hi and (value - lo) % step == 0:
                return True
        elif '-' in part:
            lo, hi = map(int, part.split('-', 1))
            if lo <= value <= hi:
                return True
        else:
            if int(part) == value:
                return True
    return False


def cron_match(schedule, dt):
    """Return True if standard 5-field cron schedule matches datetime dt."""
    parts = schedule.split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    if not _cron_field_match(minute, dt.minute, 0, 59): return False
    if not _cron_field_match(hour, dt.hour, 0, 23): return False
    if not _cron_field_match(month, dt.month, 1, 12): return False
    # cron weekday convention: 0=Sun..6=Sat. Python: 0=Mon..6=Sun.
    cron_dow = (dt.weekday() + 1) % 7
    dow_match = _cron_field_match(dow, cron_dow, 0, 6)
    dom_match = _cron_field_match(dom, dt.day, 1, 31)
    # Cron quirk: if BOTH dom and dow are restricted (neither is '*'), the day
    # matches if EITHER does (OR). Otherwise, the restricted one decides.
    if dom == '*' and dow == '*':
        return True
    if dom != '*' and dow != '*':
        return dom_match or dow_match
    return dom_match if dom != '*' else dow_match


def cron_prev_fire(schedule, now_utc, max_lookback_minutes=14 * 24 * 60):
    """Walk back minute-by-minute to find the most recent scheduled fire time.
    The cron schedule is interpreted in LOCAL TIME (matches scheduler_executor.sh
    behavior — system cron runs jobs in TZ=local). The returned datetime is in
    UTC for direct comparison against last_fired_at (which is stored as UTC).
    Returns None if no match within max_lookback_minutes (default 14 days)."""
    from datetime import timedelta
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Los_Angeles")
    except Exception:
        # Fallback: assume local == UTC (shouldn't happen on this fleet)
        tz = timezone.utc
    candidate = now_utc.astimezone(tz).replace(second=0, microsecond=0)
    for _ in range(max_lookback_minutes):
        if cron_match(schedule, candidate):
            return candidate.astimezone(timezone.utc)
        candidate -= timedelta(minutes=1)
    return None


def check_cron_staleness(agent_name, stale_minutes=20):
    """
    Check Supabase for active PUSH scheduled tasks. For each, compute the most
    recent expected fire time from the cron schedule, then determine whether
    last_fired_at met that expectation. Returns the worst overdue across the
    agent's push tasks.

    Returns (overdue_minutes, task_id, check_ok).
      overdue_minutes = 0 if every task fired on time,
                        else minutes since the most-recently-missed expected fire.
      check_ok=False means the check itself failed (network error etc.) —
      callers must not create tasks on a failed check (avoid false-positive
      floods, see 2026-04-24 incident).

    IMPORTANT (2026-04-24): filtered to trigger=push. Pull tasks go stale when
    agents are idle — that's working-as-designed.
    IMPORTANT (2026-04-30): now schedule-aware. Old logic treated any push
    last_fired_at older than 20min as stale, which loop-restarted agents whose
    freshest push was hourly (`0 * * * *`) — between fires it always reads as
    21-60min "stale". Compare to the most recent scheduled fire instead, with
    a 2-min grace period for fire timing.
    """
    if not SUPABASE_URL or not SUPABASE_KEY:
        return None, None, True
    try:
        url = (
            f"{SUPABASE_URL}/rest/v1/scheduled_tasks"
            f"?agent_name=eq.{agent_name}&active=eq.true&trigger=eq.push"
            f"&order=last_fired_at.desc&limit=20"
        )
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        resp = urllib.request.urlopen(req, timeout=10)
        tasks = json.loads(resp.read())
        fired = [t for t in tasks if t.get("last_fired_at")]
        if not fired:
            return None, None, True  # no push tasks ever fired — new agent

        from datetime import timedelta
        GRACE = timedelta(minutes=2)
        # Only frequent tasks (prev_fire within HORIZON) suit "MCP-disconnected"
        # detection — agent restart fixes a broken MCP loop. Daily/weekly tasks
        # falling behind are a scheduler-level or system-level problem; restarting
        # the agent doesn't help and would be a noisy false positive (e.g.,
        # sched_vera_weekly_memory_review last firing 2 weeks ago shouldn't
        # restart vera every 15 min).
        HORIZON = timedelta(hours=4)
        now = datetime.now(timezone.utc)
        worst_overdue = 0.0
        worst_id = fired[0]["id"]
        for t in fired:
            sched = t.get("schedule") or ""
            last_fired = datetime.fromisoformat(t["last_fired_at"].replace("Z", "+00:00"))
            prev_fire = cron_prev_fire(sched, now)
            if prev_fire is None:
                continue  # couldn't parse / no recent fire window — skip silently
            if (now - prev_fire) > HORIZON:
                continue  # daily/weekly etc. — out of scope for MCP-disconnect detection
            if last_fired >= prev_fire - GRACE:
                continue  # fired on time
            overdue_min = (now - prev_fire).total_seconds() / 60
            if overdue_min > worst_overdue:
                worst_overdue = overdue_min
                worst_id = t["id"]
        return worst_overdue, worst_id, True
    except Exception as e:
        print(f"[warn] cron staleness check failed for {agent_name}: {e}", file=sys.stderr)
        return None, None, False  # do not create tasks on a failed check


def scheduler_is_alive(window_sec=SCHEDULER_LIVENESS_WINDOW_SEC):
    """
    Check whether scheduler_executor.sh has logged anything to infra.log within
    the last `window_sec` seconds. This is the only real "scheduler is dead"
    signal — per-task staleness can mean many other things (agent down, plugin
    unspawned, agent stuck). Returns True if scheduler appears alive, False if
    no recent activity, None if the check itself failed (log unreadable etc.).
    """
    if not os.path.exists(INFRA_LOG):
        return None
    try:
        # Tail the last ~500 lines, grep for scheduler entries, find newest.
        result = subprocess.run(
            ["tail", "-n", "500", INFRA_LOG],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return None
        now = datetime.now(timezone.utc)
        for line in reversed(result.stdout.splitlines()):
            if "| scheduler |" not in line:
                continue
            # format: "2026-04-24 09:11:00 | INFO | scheduler | ..."
            try:
                ts_raw = line.split("|", 1)[0].strip()
                ts = datetime.strptime(ts_raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                age_sec = (now - ts).total_seconds()
                # Account for timezone — infra.log uses local time. Allow +/- 8h skew.
                if abs(age_sec) < window_sec + 8 * 3600:
                    return True
                return False
            except Exception:
                continue
        # No scheduler lines found in tail window
        return False
    except Exception:
        return None


def fix_mcp_scheduler_recently_alerted(agent_name):
    """Dedupe: return True if we've already created a fix_mcp_scheduler task
    for this agent within HEALTH_ALERT_DEDUPE_SECS (6h). Prevents the 544-flood
    scenario where every cron tick creates a new admin_task for the same stale
    agent."""
    p = os.path.join(HEALTH_ALERT_DIR, f"{agent_name}.fix_mcp_scheduler")
    if not os.path.exists(p):
        return False
    try:
        age = time.time() - os.path.getmtime(p)
        return age < HEALTH_ALERT_DEDUPE_SECS
    except Exception:
        return False


def mark_fix_mcp_scheduler_alerted(agent_name):
    p = os.path.join(HEALTH_ALERT_DIR, f"{agent_name}.fix_mcp_scheduler")
    try:
        open(p, "w").close()
        os.utime(p, None)
    except Exception:
        pass


def fix_mcp_scheduler_backlog_size():
    """Count pending admin_tasks with action=fix_mcp_scheduler. If over the cap,
    the caller should skip creating more rows until admin drains some."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return 0
    try:
        url = f"{SUPABASE_URL}/rest/v1/admin_tasks?action=eq.fix_mcp_scheduler&status=eq.pending&select=task_id"
        req = urllib.request.Request(url, headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
        })
        rows = json.loads(urllib.request.urlopen(req, timeout=5).read())
        return len(rows)
    except Exception:
        return 0


def check_oauth_mode(agent):
    """Read daemon log to find whether the last launch used Pro OAuth or API key fallback."""
    log_path = agent.get("daemon_log")
    if not log_path or not os.path.exists(log_path):
        return None
    try:
        with open(log_path) as f:
            lines = f.readlines()
        for line in reversed(lines):
            if "Using Pro OAuth token" in line:
                return "pro"
            if "falling back to API key" in line:
                return "api_key"
    except Exception:
        pass
    return None


def check_token_validity(agent):
    """Check if agent has a valid OAuth token in its credentials file."""
    creds_path = agent.get("credentials")
    if not creds_path or not os.path.exists(creds_path):
        return None  # no credentials file — API key is correct for this agent
    try:
        with open(creds_path) as f:
            d = json.load(f)
        oauth = d.get("claudeAiOauth", {})
        token = oauth.get("accessToken", "")
        expires = oauth.get("expiresAt", 0)
        if not token:
            return "missing"
        remaining = expires / 1000 - time.time() if expires else float("inf")
        if remaining > 1800:
            return "valid"
        elif remaining > 0:
            return "expiring_soon"
        else:
            return "expired"
    except Exception:
        return None


def create_admin_task(action, agent_name, reason, instructions):
    """Create a task in Supabase admin_tasks table for Admin to handle."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return False
    suffix = ''.join(random.choices(string.ascii_lowercase + string.digits, k=6))
    task_id = f"hc_{agent_name}_{action[:12]}_{suffix}"
    payload = json.dumps({
        "task_id": task_id,
        "source_agent": "health_check",
        "target_agent": "admin",
        "action": action,
        "status": "pending",
        "params": json.dumps({
            "agent": agent_name,
            "reason": reason,
            "instructions": instructions,
        }),
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/admin_tasks",
        data=payload,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
        return True
    except Exception as e:
        print(f"[warn] Failed to create admin task: {e}", file=sys.stderr)
        return False


def log_to_supabase(level, component, message, metadata=None):
    """Log event to infra_events table."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        return
    payload = json.dumps({
        "level": level,
        "component": component,
        "message": message[:500],
        "metadata": metadata or {},
    }).encode()
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/infra_events",
        data=payload,
        headers={
            "apikey": SUPABASE_KEY,
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as e:
        print(f"[warn] Failed to log to Supabase: {e}", file=sys.stderr)


RESTART_COOLDOWN_MINUTES = 30

def recently_restarted(agent_state):
    """Return True if this agent was restarted within the cooldown window."""
    last = agent_state.get("last_restart")
    if not last:
        return False
    try:
        last_dt = datetime.fromisoformat(last)
        minutes_ago = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
        return minutes_ago < RESTART_COOLDOWN_MINUTES
    except Exception:
        return False


def restart_blocked_by_limit(agent_state):
    """Return True if we've hit the consecutive restart limit without recovery."""
    return agent_state.get("consecutive_restarts_without_recovery", 0) >= MAX_CONSECUTIVE_RESTARTS


def record_restart(agent_state, now):
    agent_state["total_restarts"] = agent_state.get("total_restarts", 0) + 1
    agent_state["last_restart"] = now
    agent_state["consecutive_restarts_without_recovery"] = agent_state.get("consecutive_restarts_without_recovery", 0) + 1


def record_healthy(agent_state):
    """Reset consecutive restart counter when agent is confirmed healthy."""
    agent_state["consecutive_restarts_without_recovery"] = 0


def restart_agent(agent):
    """Attempt to restart a sub-agent's daemon via launchctl."""
    name = agent["name"]
    if name == "derek":
        return False  # never restart self

    plist_candidates = [
        os.path.expanduser(f"~/Library/LaunchAgents/com.{name}.daemon.plist"),
        os.path.expanduser(f"~/Library/LaunchAgents/com.{name}-agent.daemon.plist"),
        os.path.expanduser(f"~/Library/LaunchAgents/com.claude-{name}.daemon.plist"),
    ]
    # Also check for dereklm-specific naming
    if name == "dereklm":
        plist_candidates.insert(0, os.path.expanduser("~/Library/LaunchAgents/com.dereklm.daemon.plist"))

    plist = None
    for p in plist_candidates:
        if os.path.exists(p):
            plist = p
            break

    if not plist:
        return False

    try:
        subprocess.run(["launchctl", "unload", plist], capture_output=True, timeout=10)
        time.sleep(2)
        subprocess.run(["launchctl", "load", plist], capture_output=True, timeout=10)
        return True
    except Exception:
        return False


def run_health_check():
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    results = []

    # Scheduler-liveness pre-check (2026-04-24). If scheduler_executor.sh hasn't
    # logged anything in 5 min, the scheduler itself is dead — that's the one
    # genuine "scheduler down" signal. Fires a single scheduler_down admin_task
    # (deduped per 1h) instead of spamming per-agent fix_mcp_scheduler rows.
    sched_alive = scheduler_is_alive()
    if sched_alive is False:
        dedupe_path = os.path.join(HEALTH_ALERT_DIR, "scheduler_down")
        dedupe_age = time.time() - os.path.getmtime(dedupe_path) if os.path.exists(dedupe_path) else 1e9
        if dedupe_age >= 3600:  # 1h cooldown on scheduler_down alerts
            log_to_supabase("CRITICAL", "scheduler",
                "Scheduler appears down — no component=scheduler entries in infra.log for 5+ min",
                {"check": "scheduler_is_alive"})
            # Single admin_task for the scheduler itself, not per-agent
            create_admin_task(
                action=ta.FIX_MCP_SCHEDULER,
                agent_name="admin",
                reason="scheduler_executor.sh appears DOWN — no component=scheduler entries in infra.log for 5+ min",
                instructions=(
                    "Check crontab entry `* * * * * /Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp/scheduler_executor.sh`. "
                    "Tail ~/derek/logs/infra.log for component=scheduler entries. "
                    "If macOS TCC is blocking cron, consider migrating to launchd (pattern: com.memory-projector.plist)."
                ),
            )
            open(dedupe_path, "w").close()

    for agent in AGENTS:
        name = agent["name"]
        status = check_tmux_session(agent["tmux"])
        errors = detect_errors(status["output"]) if status["running"] else []

        agent_state = state.get(name, {"consecutive_down": 0, "last_restart": None, "total_restarts": 0})

        oauth_mode = check_oauth_mode(agent)
        token_status = check_token_validity(agent)
        cron_minutes_ago, cron_task_id, cron_check_ok = check_cron_staleness(name, stale_minutes=20)
        is_ready, minutes_since_start = check_ready_file(agent)
        flood_count = check_input_flood(agent) if status["running"] else 0

        result = {
            "agent": name,
            "running": status["running"],
            "errors": errors,
            "checked_at": now,
            "action_taken": None,
            "oauth_mode": oauth_mode,
            "token_status": token_status,
            "cron_minutes_ago": cron_minutes_ago,
            "ready": is_ready,
            "flood_count": flood_count,
        }

        if not status["running"]:
            agent_state["consecutive_down"] = agent_state.get("consecutive_down", 0) + 1

            log_to_supabase("ERROR", f"agent:{name}", f"Agent {name} tmux session DOWN", {
                "consecutive_down": agent_state["consecutive_down"],
                "tmux_session": agent["tmux"],
            })

            # Intervention: create admin task for Derek to investigate and fix
            if name in AUTO_RESTART_AGENTS:
                consecutive = agent_state["consecutive_down"]
                if consecutive == 1:
                    # First down: ask Derek to investigate and restart
                    create_admin_task(
                        action=ta.FIX_AGENT_DOWN,
                        agent_name=name,
                        reason=f"tmux session '{agent['tmux']}' is down (check 1)",
                        instructions=(
                            f"Agent {name} tmux session is down. "
                            f"1) Check ~/Library/LaunchAgents for its plist and reload it. "
                            f"2) Check the agent's daemon log for errors. "
                            f"3) If the token is expired, run refresh_agent_token.sh first. "
                            f"4) Confirm the session comes back up. "
                            f"5) Alert Farlen only if you cannot fix it."
                        ),
                    )
                    result["action_taken"] = "task_created_for_derek"
                    log_to_supabase("WARNING", f"agent:{name}", f"Agent {name} DOWN — tasked Derek to fix", {
                        "consecutive_down": consecutive,
                    })
                elif consecutive >= MAX_CONSECUTIVE_DOWN:
                    # Still down after Derek was tasked — escalate to Farlen
                    result["escalate"] = True
                    send_telegram(
                        f"⚠️ {name}: still DOWN after {consecutive} checks. "
                        f"Derek was tasked but it hasn't recovered. Manual intervention needed."
                    )
                    log_to_supabase("CRITICAL", f"agent:{name}",
                        f"Agent {name} still DOWN after {consecutive} checks — escalated to Farlen")
        else:
            agent_state["consecutive_down"] = 0
            # Reset restart counter if agent is healthy (ready + crons firing)
            if is_ready and (cron_minutes_ago is None or cron_minutes_ago < 20):
                record_healthy(agent_state)

            if errors:
                log_to_supabase("WARNING", f"agent:{name}", f"Errors detected in {name}: {errors[0]}", {
                    "error_count": len(errors),
                    "errors": errors[:5],
                })
                result["action_taken"] = "errors_logged"

            # Agent is "onboarded" if it has ever had a cron fire — proxy for real usage
            agent_is_onboarded = cron_minutes_ago is not None

            # Cron staleness: MCP scheduler likely disconnected — restart to reconnect
            # Only act when check_ok=True; a failed check (network error etc.) must not
            # trigger task creation — that was the source of 1727 false-positive tasks.
            STALE_THRESHOLD = 20  # minutes — */5 cron should fire within 20 min
            # Staleness alerting guardrails (2026-04-24 post-544-flood):
            #   A. Confirm the scheduler itself is alive before blaming per-agent staleness.
            #      If scheduler is dead, the right action is to alert ONCE about the
            #      scheduler, not create N fix_mcp_scheduler rows for each agent.
            #   B. Dedupe per-agent with 6h cooldown.
            #   C. Respect global cap on pending fix_mcp_scheduler rows.
            if (cron_check_ok and cron_minutes_ago is not None and cron_minutes_ago > STALE_THRESHOLD
                    and name in AUTO_RESTART_AGENTS
                    and agent_is_onboarded
                    and not recently_restarted(agent_state)):
                sched_alive = scheduler_is_alive()
                if sched_alive is False:
                    # Scheduler itself is dead — don't blame agents. Let the
                    # scheduler-level alert (handled once per run below) cover it.
                    log_to_supabase("WARNING", f"agent:{name}",
                        f"Agent {name} crons stale ({cron_minutes_ago:.0f}min) but scheduler appears down — suppressing per-agent alert",
                        {"last_task": cron_task_id, "minutes_ago": cron_minutes_ago})
                    result["action_taken"] = "suppressed_scheduler_down"
                elif restart_blocked_by_limit(agent_state):
                    # Agent has been auto-restarted too many times and still stale —
                    # create admin_task, but only if we haven't alerted for this
                    # agent in the last 6h AND the global backlog isn't already full.
                    if fix_mcp_scheduler_recently_alerted(name):
                        result["action_taken"] = "dedupe_suppressed"
                        log_to_supabase("INFO", f"agent:{name}",
                            f"Agent {name} crons stale but dedupe active — no new admin_task",
                            {"minutes_ago": cron_minutes_ago})
                    elif fix_mcp_scheduler_backlog_size() >= HEALTH_ALERT_CAP:
                        result["action_taken"] = "backlog_cap_reached"
                        log_to_supabase("WARNING", "health_check",
                            f"fix_mcp_scheduler backlog >= {HEALTH_ALERT_CAP} — admin draining before more alerts fire",
                            {"agent": name, "minutes_ago": cron_minutes_ago})
                    else:
                        result["action_taken"] = "restart_limit_reached"
                        create_admin_task(
                            action=ta.FIX_MCP_SCHEDULER,
                            agent_name=name,
                            reason=f"Crons stale {cron_minutes_ago:.0f}min — restarted {agent_state.get('consecutive_restarts_without_recovery',0)}x without recovery",
                            instructions=(
                                f"Agent {name} MCP scheduler keeps disconnecting. Auto-restart has been attempted "
                                f"{agent_state.get('consecutive_restarts_without_recovery',0)} times without recovery. "
                                f"Investigate root cause — check MCP server logs, .ready file, startup instructions."
                            ),
                        )
                        mark_fix_mcp_scheduler_alerted(name)
                else:
                    log_to_supabase("WARNING", f"agent:{name}",
                        f"Agent {name} crons stale ({cron_minutes_ago:.0f}min) — MCP likely disconnected", {
                            "last_task": cron_task_id, "minutes_ago": cron_minutes_ago,
                        })
                    restarted = restart_agent(agent)
                    if restarted:
                        record_restart(agent_state, now)
                        result["action_taken"] = "mcp_reconnect_restart"
                    else:
                        result["action_taken"] = "mcp_reconnect_failed"
                        if fix_mcp_scheduler_recently_alerted(name):
                            result["action_taken"] += "_dedupe_suppressed"
                        elif fix_mcp_scheduler_backlog_size() >= HEALTH_ALERT_CAP:
                            result["action_taken"] += "_backlog_cap"
                        else:
                            create_admin_task(ta.FIX_MCP_SCHEDULER, name,
                                f"Crons stale {cron_minutes_ago:.0f}min, auto-restart failed",
                                f"Agent {name} MCP scheduler disconnected, auto-restart failed. Manually reload plist.")
                            mark_fix_mcp_scheduler_alerted(name)

            # Startup stuck: running >10min but no .ready file — startup never completed
            # Skip if agent has never had a cron fire — it's not yet onboarded, just idle.
            # Also skip if the staleness check failed — we can't confirm onboarding status.
            STARTUP_TIMEOUT = 10  # minutes
            if (cron_check_ok and not is_ready and minutes_since_start is not None
                    and minutes_since_start > STARTUP_TIMEOUT
                    and name in AUTO_RESTART_AGENTS
                    and agent_is_onboarded
                    and not recently_restarted(agent_state)):
                if restart_blocked_by_limit(agent_state):
                    result["action_taken"] = "restart_limit_reached"
                    create_admin_task(
                        action=ta.FIX_STARTUP_STUCK,
                        agent_name=name,
                        reason=f"Startup stuck {minutes_since_start:.0f}min — restarted {agent_state.get('consecutive_restarts_without_recovery',0)}x without recovery",
                        instructions=(
                            f"Agent {name} keeps failing to complete startup (no .ready file). "
                            f"Auto-restart attempted {agent_state.get('consecutive_restarts_without_recovery',0)} times. "
                            f"Check startup-instructions.md for failing steps, memory file errors, or MCP issues."
                        ),
                    )
                else:
                    log_to_supabase("WARNING", f"agent:{name}",
                        f"Agent {name} stuck at startup ({minutes_since_start:.0f}min, no .ready file)", {
                            "minutes_since_start": minutes_since_start,
                        })
                    restarted = restart_agent(agent)
                    if restarted:
                        record_restart(agent_state, now)
                        result["action_taken"] = "startup_stuck_restart"
                    else:
                        result["action_taken"] = "startup_stuck_restart_failed"
                        create_admin_task(ta.FIX_STARTUP_STUCK, name,
                            f"Startup stuck {minutes_since_start:.0f}min, auto-restart failed",
                            f"Agent {name} startup stuck, auto-restart failed. Manually reload plist.")

            # Input flood: SCHEDULED TASK messages piling up, pushing prompt off-screen
            # Scheduler then thinks agent is perpetually busy and stalls all crons
            FLOOD_THRESHOLD = 6  # consecutive task lines at end of pane
            if (flood_count >= FLOOD_THRESHOLD and name in AUTO_RESTART_AGENTS
                    and not recently_restarted(agent_state)):
                log_to_supabase("WARNING", f"agent:{name}",
                    f"Agent {name} input buffer flooded ({flood_count} stacked task messages) — restarting", {
                        "flood_count": flood_count,
                    })
                restarted = restart_agent(agent)
                if restarted:
                    agent_state["total_restarts"] = agent_state.get("total_restarts", 0) + 1
                    agent_state["last_restart"] = now
                    result["action_taken"] = "flood_restart"
                    log_to_supabase("INFO", f"agent:{name}",
                        f"Restarted {name} to clear input flood ({flood_count} stacked messages)")
                else:
                    result["action_taken"] = "flood_restart_failed"
                    create_admin_task(
                        action=ta.FIX_INPUT_FLOOD,
                        agent_name=name,
                        reason=f"{flood_count} stacked SCHEDULED TASK messages flooding input buffer",
                        instructions=(
                            f"Agent {name} has {flood_count} SCHEDULED TASK messages piled up in its "
                            f"tmux input buffer. The scheduler skips it thinking it's perpetually busy. "
                            f"Restart the daemon to clear the queue."
                        ),
                    )

            # OAuth fallback correction: agent has a valid token but launched on API key
            if (oauth_mode == "api_key" and token_status in ("valid", "expiring_soon")
                    and name in AUTO_RESTART_AGENTS
                    and not recently_restarted(agent_state)):
                log_to_supabase("WARNING", f"agent:{name}",
                    f"Agent {name} running on API key fallback but has valid Pro token", {
                        "token_status": token_status,
                    })
                # Try immediate auto-restart (fast path — no need to wait for Derek)
                restarted = restart_agent(agent)
                if restarted:
                    agent_state["total_restarts"] = agent_state.get("total_restarts", 0) + 1
                    agent_state["last_restart"] = now
                    result["action_taken"] = "oauth_corrected"
                    log_to_supabase("INFO", f"agent:{name}",
                        f"Auto-restarted {name} to restore Pro OAuth token")
                else:
                    # Restart failed — hand off to Derek
                    result["action_taken"] = "oauth_correction_failed"
                    create_admin_task(
                        action=ta.FIX_TOKEN,
                        agent_name=name,
                        reason=f"Running on API key fallback but has valid Pro token (status: {token_status}). Auto-restart failed.",
                        instructions=(
                            f"Agent {name} has a valid Pro OAuth token on disk but its session launched "
                            f"in API key fallback mode, and the auto-restart failed. "
                            f"Manually reload the plist: launchctl unload/load "
                            f"~/Library/LaunchAgents/com.{name}-agent.daemon.plist. "
                            f"Confirm the daemon log says 'Using Pro OAuth token' after restart. "
                            f"Alert Farlen if you cannot fix it."
                        ),
                    )
                    log_to_supabase("WARNING", f"agent:{name}",
                        f"OAuth correction failed for {name} — tasked Derek")

        state[name] = agent_state
        results.append(result)

    save_state(state)
    return results


def generate_dashboard_html(results=None):
    """Generate an HTML dashboard page showing agent health."""
    if results is None:
        results = run_health_check()

    # Also pull recent infra_events from Supabase
    recent_events = []
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            req = urllib.request.Request(
                f"{SUPABASE_URL}/rest/v1/infra_events?order=timestamp.desc&limit=50",
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                },
            )
            resp = urllib.request.urlopen(req, timeout=10)
            recent_events = json.loads(resp.read())
        except Exception:
            pass

    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    agents_html = ""
    for r in results:
        status_color = "#22c55e" if r["running"] else "#ef4444"
        status_text = "RUNNING" if r["running"] else "DOWN"
        error_count = len(r.get("errors", []))
        error_badge = f'<span style="background:#fbbf24;color:#000;padding:2px 8px;border-radius:4px;font-size:12px;">{error_count} errors</span>' if error_count > 0 else ""
        action = r.get("action_taken", "")
        action_html = f'<span style="background:#3b82f6;color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;">{action}</span>' if action else ""

        agents_html += f"""
        <div style="background:#1e1e2e;border-radius:12px;padding:20px;display:flex;align-items:center;justify-content:space-between;margin-bottom:12px;">
            <div style="display:flex;align-items:center;gap:12px;">
                <div style="width:12px;height:12px;border-radius:50%;background:{status_color};box-shadow:0 0 8px {status_color};"></div>
                <div>
                    <div style="font-size:18px;font-weight:600;">{r['agent']}</div>
                    <div style="font-size:13px;color:#888;">{status_text}</div>
                </div>
            </div>
            <div style="display:flex;gap:8px;align-items:center;">
                {error_badge}
                {action_html}
            </div>
        </div>"""

    events_html = ""
    level_colors = {"INFO": "#3b82f6", "WARNING": "#fbbf24", "ERROR": "#ef4444", "CRITICAL": "#dc2626"}
    for evt in recent_events[:30]:
        color = level_colors.get(evt.get("level", "INFO"), "#888")
        ts = evt.get("timestamp", "")[:19].replace("T", " ")
        events_html += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #2a2a3e;font-size:13px;color:#888;">{ts}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #2a2a3e;"><span style="background:{color};color:#fff;padding:2px 8px;border-radius:4px;font-size:12px;">{evt.get('level','')}</span></td>
            <td style="padding:8px 12px;border-bottom:1px solid #2a2a3e;font-size:13px;">{evt.get('component','')}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #2a2a3e;font-size:13px;">{evt.get('message','')[:100]}</td>
        </tr>"""

    running = sum(1 for r in results if r["running"])
    total = len(results)
    errors_total = sum(len(r.get("errors", [])) for r in results)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="300">
<title>Agent Health Dashboard</title>
<style>
    body {{ background: #0f0f1a; color: #e0e0e0; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 24px; }}
    .header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 32px; }}
    .stat-cards {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 16px; margin-bottom: 32px; }}
    .stat-card {{ background: #1e1e2e; border-radius: 12px; padding: 20px; text-align: center; }}
    .stat-value {{ font-size: 36px; font-weight: 700; }}
    .stat-label {{ font-size: 13px; color: #888; margin-top: 4px; }}
    table {{ width: 100%; border-collapse: collapse; background: #1e1e2e; border-radius: 12px; overflow: hidden; }}
    th {{ padding: 12px; text-align: left; font-size: 13px; color: #888; border-bottom: 2px solid #2a2a3e; }}
</style>
</head>
<body>
<div style="max-width: 900px; margin: 0 auto;">
    <div class="header">
        <h1 style="font-size: 24px; margin: 0;">Agent Health Dashboard</h1>
        <div style="font-size: 13px; color: #888;">Last updated: {now_str}</div>
    </div>

    <div class="stat-cards">
        <div class="stat-card">
            <div class="stat-value" style="color:{'#22c55e' if running == total else '#ef4444'}">{running}/{total}</div>
            <div class="stat-label">Agents Running</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="color:{'#22c55e' if errors_total == 0 else '#fbbf24'}">{errors_total}</div>
            <div class="stat-label">Active Errors</div>
        </div>
        <div class="stat-card">
            <div class="stat-value" style="color:#3b82f6">{len(recent_events)}</div>
            <div class="stat-label">Events (24h)</div>
        </div>
    </div>

    <h2 style="font-size:18px;margin-bottom:16px;">Agent Status</h2>
    {agents_html}

    <h2 style="font-size:18px;margin:32px 0 16px;">Recent Events</h2>
    <table>
        <thead>
            <tr>
                <th>Time</th>
                <th>Level</th>
                <th>Component</th>
                <th>Message</th>
            </tr>
        </thead>
        <tbody>
            {events_html if events_html else '<tr><td colspan="4" style="padding:20px;text-align:center;color:#888;">No events recorded yet</td></tr>'}
        </tbody>
    </table>

    <div style="margin-top:32px;text-align:center;font-size:12px;color:#555;">
        Auto-refreshes every 5 minutes &middot; Derek Agent Infrastructure
    </div>
</div>
</body>
</html>"""
    return html


if __name__ == "__main__":
    if "--dashboard" in sys.argv:
        html = generate_dashboard_html()
        out_path = os.path.expanduser("~/derek/reports/agent-health.html")
        os.makedirs(os.path.dirname(out_path), exist_ok=True)
        with open(out_path, "w") as f:
            f.write(html)
        print(json.dumps({"dashboard": out_path}))
    elif "--json" in sys.argv:
        results = run_health_check()
        print(json.dumps(results, indent=2))
    else:
        results = run_health_check()
        escalations = [r for r in results if r.get("escalate")]
        down = [r for r in results if not r["running"]]
        errors = [r for r in results if r.get("errors")]

        running_count = sum(1 for r in results if r["running"])
        total_count = len(results)
        print(f"Agents: {running_count}/{total_count} running")

        if down:
            print(f"DOWN: {', '.join(r['agent'] for r in down)}")
        if errors:
            for r in errors:
                print(f"ERRORS in {r['agent']}: {len(r['errors'])} detected")
        if escalations:
            print(f"ESCALATE: {', '.join(r['agent'] for r in escalations)}")
        if not down and not errors:
            print("All clear.")

        # Only alert Farlen directly for true escalations (Derek couldn't fix it)
        # Routine issues (agent down once, oauth fallback) go to Derek via admin_tasks
        if escalations:
            names = ", ".join(r["agent"] for r in escalations)
            send_telegram(
                f"⚠️ Agent health escalation — needs your attention:\n{names}\n"
                f"Derek has been unable to resolve. Check ~/derek/logs/agent-health-check.log for details."
            )
