#!/usr/bin/env python3
"""
Agent Health Check — monitors all sub-agent tmux sessions, logs to Supabase infra_events,
applies intervention rules (auto-restart, escalation alerts).

Usage:
    python3 agent_health_check.py              # check all agents
    python3 agent_health_check.py --json       # output JSON summary
    python3 agent_health_check.py --dashboard  # generate dashboard HTML
"""

import subprocess, json, os, sys, time, urllib.request
from datetime import datetime, timezone
from pathlib import Path

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TMUX = "/opt/homebrew/bin/tmux"

PLATFORM_DIR = Path(__file__).resolve().parent.parent
AGENTS_REGISTRY = PLATFORM_DIR / "configs" / "agents.json"

def load_agents():
    """Load agents from registry, or use defaults."""
    if AGENTS_REGISTRY.exists():
        with open(AGENTS_REGISTRY) as f:
            registry = json.load(f)
        return [
            {"name": a["name"], "tmux": f"{a['name']}-agent", "workspace": os.path.expanduser(f"~/{a['name']}")}
            for a in registry
        ]
    # Fallback: scan for agent tmux sessions
    return []

AGENTS = load_agents()

# Intervention rules
MAX_CONSECUTIVE_DOWN = 2  # alert after 2 consecutive down checks
ADMIN_AGENT = os.environ.get("AGENT_NAME", "admin")  # don't auto-restart self
AUTO_RESTART_AGENTS = [a["name"] for a in AGENTS if a["name"] != ADMIN_AGENT]
STATE_FILE = os.path.expanduser(f"~/{ADMIN_AGENT}/logs/agent_health_state.json")


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

    for agent in AGENTS:
        name = agent["name"]
        status = check_tmux_session(agent["tmux"])
        errors = detect_errors(status["output"]) if status["running"] else []

        agent_state = state.get(name, {"consecutive_down": 0, "last_restart": None, "total_restarts": 0})

        result = {
            "agent": name,
            "running": status["running"],
            "errors": errors,
            "checked_at": now,
            "action_taken": None,
        }

        if not status["running"]:
            agent_state["consecutive_down"] = agent_state.get("consecutive_down", 0) + 1

            log_to_supabase("ERROR", f"agent:{name}", f"Agent {name} tmux session DOWN", {
                "consecutive_down": agent_state["consecutive_down"],
                "tmux_session": agent["tmux"],
            })

            # Intervention: auto-restart after 1 down check (if eligible)
            if name in AUTO_RESTART_AGENTS:
                restarted = restart_agent(agent)
                if restarted:
                    agent_state["total_restarts"] = agent_state.get("total_restarts", 0) + 1
                    agent_state["last_restart"] = now
                    result["action_taken"] = "auto_restarted"
                    log_to_supabase("WARN", f"agent:{name}", f"Auto-restarted agent {name}", {
                        "total_restarts": agent_state["total_restarts"],
                    })
                else:
                    result["action_taken"] = "restart_failed"
                    log_to_supabase("CRITICAL", f"agent:{name}", f"Failed to restart agent {name} — no plist found")

            # Escalation: alert after MAX_CONSECUTIVE_DOWN
            if agent_state["consecutive_down"] >= MAX_CONSECUTIVE_DOWN:
                result["escalate"] = True
        else:
            agent_state["consecutive_down"] = 0

            if errors:
                log_to_supabase("WARN", f"agent:{name}", f"Errors detected in {name}: {errors[0]}", {
                    "error_count": len(errors),
                    "errors": errors[:5],
                })
                result["action_taken"] = "errors_logged"

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
    level_colors = {"INFO": "#3b82f6", "WARN": "#fbbf24", "ERROR": "#ef4444", "CRITICAL": "#dc2626"}
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

        print(f"Agents: {sum(1 for r in results if r['running'])}/{len(results)} running")
        if down:
            print(f"DOWN: {', '.join(r['agent'] for r in down)}")
        if errors:
            for r in errors:
                print(f"ERRORS in {r['agent']}: {len(r['errors'])} detected")
        if escalations:
            print(f"ESCALATE: {', '.join(r['agent'] for r in escalations)}")
        if not down and not errors:
            print("All clear.")
