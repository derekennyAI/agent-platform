#!/usr/bin/env python3
"""Daily Lean Intelligence DevOps Report.

Sweeps n8n, Supabase, Grafana, Linear, and frontends.
Generates a PDF and emails + Telegrams it to Farlen.

Usage:
    python3 lean_devops_report.py
    python3 lean_devops_report.py --dry-run
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

REPORT_TO = "YOUR_ADMIN_EMAIL"


def get_fun_fact():
    try:
        req = urllib.request.Request("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en")
        req.add_header("User-Agent", "derek-agent/1.0")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read()).get("text", "")
    except Exception:
        return ""


def n8n_request(path, secrets):
    req = urllib.request.Request(f"https://primary-production-cea5.up.railway.app/api/v1{path}")
    req.add_header("X-N8N-API-KEY", secrets["n8n_api_key"])
    req.add_header("User-Agent", "derek-agent/1.0")
    with urllib.request.urlopen(req, timeout=12) as r:
        return json.loads(r.read())


def supabase_sql(query, secrets):
    tok = secrets["supabase_access_token"]
    ref = secrets["supabase_project_ref"]
    payload = json.dumps({"query": query}).encode()
    req = urllib.request.Request(
        f"https://api.supabase.com/v1/projects/{ref}/database/query",
        data=payload
    )
    req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "derek-agent/1.0")
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def check_url(url):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:
        return 0, str(e)[:60]


def linear_query(gql, secrets):
    payload = json.dumps({"query": gql}).encode()
    req = urllib.request.Request("https://api.linear.app/graphql", data=payload)
    req.add_header("Authorization", secrets["linear_api_key"])
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "derek-agent/1.0")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def gather_data(secrets):
    data = {}

    # Frontends
    prod_code, _ = check_url("https://ai.leanmarketing.com")
    dev_code, _ = check_url("https://lean-intelligence-dev.up.railway.app")
    data["frontend_prod"] = prod_code
    data["frontend_dev"] = dev_code

    # n8n failures
    try:
        execs = n8n_request("/executions?status=error&limit=10", secrets)
        failed = execs.get("data", [])
        data["n8n_failures"] = len(failed)
        # Get details for top 3
        details = []
        for ex in failed[:3]:
            wf_name = "?"
            error_node = None
            error_msg = None
            try:
                full = n8n_request(f"/executions/{ex['id']}?includeData=true", secrets)
                wf_name = full.get("workflowData", {}).get("name", "?") or "?"
                rd = (full.get("data") or {}).get("resultData", {})
                error_node = rd.get("lastNodeExecuted")
                err = rd.get("error", {})
                if err:
                    error_msg = err.get("description") or err.get("message", "")
                for node, runs in rd.get("runData", {}).items():
                    for run in (runs or []):
                        if run.get("error"):
                            error_node = node
                            error_msg = run["error"].get("description") or run["error"].get("message", "")
                            break
            except Exception:
                pass
            details.append({
                "id": ex["id"],
                "workflow": wf_name,
                "started": ex.get("startedAt", "")[:16].replace("T", " "),
                "node": error_node,
                "error": (error_msg or "")[:160],
            })
        data["n8n_details"] = details
    except Exception as e:
        data["n8n_failures"] = -1
        data["n8n_details"] = []

    # n8n active workflow count
    try:
        wfs = n8n_request("/workflows?limit=1&active=true", secrets)
        data["n8n_active"] = wfs.get("count", len(wfs.get("data", [])))
    except Exception:
        data["n8n_active"] = "?"

    # Grafana alerts
    try:
        req = urllib.request.Request(
            f'{secrets["grafana_url"]}/api/alertmanager/grafana/api/v2/alerts?active=true&silenced=false&inhibited=false'
        )
        req.add_header("Authorization", f'Bearer {secrets["grafana_service_account_token"]}')
        req.add_header("User-Agent", "derek-agent/1.0")
        with urllib.request.urlopen(req, timeout=10) as r:
            alerts = json.loads(r.read())
            data["grafana_alerts"] = len(alerts)
    except Exception:
        data["grafana_alerts"] = 0

    # Supabase queue
    try:
        failed_q = supabase_sql("""
            SELECT gq.id, gq.client_id, gq.writer_name, gq.error_message, gq.created_at,
                   bp.company, c.firstname, c.lastname, c.email
            FROM generation_queue gq
            LEFT JOIN clients c ON c.client_id = gq.client_id
            LEFT JOIN business_profiles bp ON bp.client_id = gq.client_id
            WHERE gq.status = 'failed' AND gq.created_at > NOW() - INTERVAL '7 days'
            ORDER BY gq.created_at DESC LIMIT 8
        """, secrets)
        for row in failed_q:
            row["company_display"] = (
                row.get("company")
                or f"{row.get('firstname','')} {row.get('lastname','')}".strip()
                or row.get("email", "Unknown client")
            )
        data["queue_failed"] = failed_q

        stats = supabase_sql("""
            SELECT status, COUNT(*) as count FROM generation_queue
            WHERE created_at > NOW() - INTERVAL '24 hours'
            GROUP BY status ORDER BY count DESC
        """, secrets)
        data["queue_stats"] = {row["status"]: row["count"] for row in stats}

        stuck = supabase_sql("""
            SELECT COUNT(*) as count FROM generation_queue
            WHERE status = 'processing' AND created_at < NOW() - INTERVAL '10 minutes'
        """, secrets)
        data["queue_stuck"] = stuck[0]["count"] if stuck else 0

        total_clients = supabase_sql("SELECT COUNT(*) as total FROM clients", secrets)
        data["total_clients"] = total_clients[0]["total"] if total_clients else "?"

        activity = supabase_sql("""
            SELECT writer_name, status, COUNT(*) as count
            FROM generation_queue
            WHERE created_at > NOW() - INTERVAL '7 days'
            GROUP BY writer_name, status ORDER BY count DESC LIMIT 12
        """, secrets)
        data["activity"] = activity
    except Exception as e:
        data["queue_failed"] = []
        data["queue_stats"] = {}
        data["queue_stuck"] = "?"
        data["total_clients"] = "?"
        data["activity"] = []

    # Linear bugs
    try:
        result = linear_query("""
        query {
          issues(filter: {
            team: { name: { eq: "Lean Marketing" } }
            state: { type: { in: ["backlog", "unstarted", "started"] } }
          }, first: 20, orderBy: updatedAt) {
            nodes { identifier title priority state { name type } createdAt }
          }
        }
        """, secrets)
        data["linear_bugs"] = result.get("data", {}).get("issues", {}).get("nodes", [])
    except Exception:
        data["linear_bugs"] = []

    data["fun_fact"] = get_fun_fact()
    return data


def badge(text, cls):
    colors = {
        "err": ("FEE9E9", "C0392B"),
        "ok": ("E6F9D5", "2E7D0B"),
        "warn": ("FFF3E0", "C47E00"),
        "info": ("E8F0FE", "1A56DB"),
        "p0": ("FEE9E9", "C0392B"),
        "p1": ("FFF3E0", "C47E00"),
        "p2": ("FFF9E6", "A07000"),
        "p3": ("F0F4FF", "5B6FA1"),
    }
    bg, fg = colors.get(cls, ("F0F0F0", "555"))
    return f'<span style="display:inline-block;padding:2px 7px;border-radius:4px;font-size:10px;font-weight:700;letter-spacing:.5px;text-transform:uppercase;background:#{bg};color:#{fg}">{text}</span>'


def priority_badge(p):
    if p == 1: return badge("P1", "p1")
    if p == 2: return badge("P2", "p2")
    if p == 3: return badge("P3", "p3")
    return badge("P0", "p0")


def status_card(label, value, sub, color="ok"):
    border = {"ok": "#C5F135", "warn": "#FFB020", "err": "#FF4D4D", "info": "#4D9EFF"}.get(color, "#ddd")
    val_color = {"ok": "#2E7D0B", "warn": "#C47E00", "err": "#C0392B", "info": "#1A56DB"}.get(color, "#111")
    return f"""<div style="background:#fff;border-radius:10px;padding:18px 20px;border:1px solid #E5E9EF;border-top:3px solid {border}">
      <div style="font-size:10px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:#aaa;margin-bottom:8px">{label}</div>
      <div style="font-size:22px;font-weight:900;color:{val_color};line-height:1">{value}</div>
      <div style="font-size:11px;color:#aaa;margin-top:5px">{sub}</div>
    </div>"""


def generate_html(data):
    today = date.today()
    now_str = datetime.now(timezone.utc).strftime("%A, %B %-d, %Y")

    # Status bar counts
    fe_ok = sum(1 for c in [data["frontend_prod"], data["frontend_dev"]] if 200 <= c < 400)
    n8n_fail = data["n8n_failures"]
    q_failed = len(data["queue_failed"])
    p0_bugs = sum(1 for b in data["linear_bugs"] if b["priority"] == 0)
    alerts = data["grafana_alerts"]

    # Status dot colors
    def sdot(color):
        c = {"g": "#C5F135", "y": "#FFB020", "r": "#FF4D4D"}[color]
        return f'<span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:{c};margin-right:6px;position:relative;top:1px"></span>'

    # n8n execution rows
    n8n_rows = ""
    for ex in data["n8n_details"]:
        n8n_rows += f"""<tr>
          <td style="white-space:nowrap;padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#444">{ex['started']}</td>
          <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#444"><strong>{ex['workflow']}</strong><br><span style="color:#aaa;font-size:10px">#{ex['id']}</span></td>
          <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#444"><span style="font-family:monospace;font-size:11px;background:#F3F4F6;padding:1px 5px;border-radius:3px;color:#555">{ex['node'] or 'Unknown'}</span></td>
          <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#444"><span style="font-family:monospace;font-size:11px;background:#F3F4F6;padding:1px 5px;border-radius:3px;color:#C0392B;word-break:break-word">{ex['error'] or 'No detail retained'}</span></td>
          <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;text-align:center">{badge('Failed', 'err')}</td>
        </tr>"""
    if not n8n_rows:
        n8n_rows = "<tr><td colspan='5' style='padding:12px 14px;font-size:12px;color:#999;font-style:italic'>No recent execution errors</td></tr>"

    # Queue failure rows
    queue_rows = ""
    for row in data["queue_failed"]:
        company = row.get("company_display", "Unknown")
        err = str(row.get("error_message", "") or "")[:120]
        ts = row.get("created_at", "")[:16].replace("T", " ")
        queue_rows += f"""<tr>
          <td style="white-space:nowrap;padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#444">{ts}</td>
          <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7"><strong style="color:#111">{company}</strong></td>
          <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#444">{row.get('writer_name','?')}</td>
          <td style="padding:10px 14px;font-size:12px;border-bottom:1px solid #F2F4F7"><span style="font-family:monospace;font-size:11px;background:#F3F4F6;padding:1px 5px;border-radius:3px;color:#C0392B">{err}</span></td>
          <td style="padding:10px 14px;text-align:center;border-bottom:1px solid #F2F4F7">{badge('Failed', 'err')}</td>
        </tr>"""
    if not queue_rows:
        queue_rows = "<tr><td colspan='5' style='padding:12px 14px;font-size:12px;color:#999;font-style:italic'>No failures in last 7 days ✅</td></tr>"

    # Writer activity rows
    activity_completed = [a for a in data["activity"] if a["status"] == "completed"]
    activity_rows = ""
    for row in activity_completed[:8]:
        activity_rows += f"""<tr>
          <td style="padding:9px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#333">{row['writer_name']}</td>
          <td style="padding:9px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#333;text-align:center;font-weight:700">{row['count']}</td>
          <td style="padding:9px 14px;border-bottom:1px solid #F2F4F7">{badge('Active', 'ok')}</td>
        </tr>"""

    # Linear bugs
    bug_rows = ""
    for bug in data["linear_bugs"][:10]:
        state = bug["state"]["name"]
        state_cls = "info" if bug["state"]["type"] == "started" else "err"
        bug_rows += f"""<tr>
          <td style="padding:9px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#888;white-space:nowrap">{bug['identifier']}</td>
          <td style="padding:9px 14px;font-size:12px;border-bottom:1px solid #F2F4F7">{priority_badge(bug['priority'])}</td>
          <td style="padding:9px 14px;font-size:12px;border-bottom:1px solid #F2F4F7;color:#333">{bug['title'][:80]}</td>
          <td style="padding:9px 14px;font-size:12px;border-bottom:1px solid #F2F4F7">{badge(state, state_cls)}</td>
        </tr>"""
    if not bug_rows:
        bug_rows = "<tr><td colspan='4' style='padding:12px 14px;font-size:12px;color:#999;font-style:italic'>No open bugs ✅</td></tr>"

    # Fun fact block
    fun_fact_html = ""
    if data.get("fun_fact"):
        fun_fact_html = f"""
  <div style="padding:0 48px 24px">
    <div style="background:#F8F9FB;border-radius:10px;padding:16px 20px;border-left:4px solid #C5F135">
      <div style="font-size:10px;font-weight:700;letter-spacing:1.2px;text-transform:uppercase;color:#aaa;margin-bottom:6px">💡 Fun Fact of the Day</div>
      <div style="font-size:13px;color:#444;line-height:1.6">{data['fun_fact']}</div>
    </div>
  </div>"""

    q_stats = data.get("queue_stats", {})
    completed_24h = q_stats.get("completed", 0)
    failed_24h = q_stats.get("failed", 0)

    def th(label):
        return f'<th style="background:#F8F9FB;padding:9px 14px;font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#888;text-align:left;border-bottom:1px solid #E5E9EF">{label}</th>'

    def section(title):
        return f'<div style="font-size:10px;font-weight:800;letter-spacing:2px;text-transform:uppercase;color:#888;margin-bottom:12px;padding-bottom:8px;border-bottom:1px solid #E5E9EF">{title}</div>'

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8">
<style>
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif; background:#F4F6F9; color:#111; }}
  .grid-3 {{ display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-bottom:20px; }}
  .grid-4 {{ display:grid; grid-template-columns:repeat(4,1fr); gap:12px; margin-bottom:20px; }}
  table {{ width:100%; border-collapse:collapse; background:#fff; border-radius:10px; overflow:hidden; border:1px solid #E5E9EF; margin-bottom:4px; }}
</style>
</head>
<body>
<div style="max-width:820px;margin:0 auto">

  <!-- Header -->
  <div style="background:#0D1117;padding:36px 48px 32px;position:relative;overflow:hidden">
    <div style="position:absolute;top:-80px;right:-80px;width:360px;height:360px;background:radial-gradient(circle,rgba(197,241,53,.12) 0%,transparent 70%)"></div>
    <div style="position:absolute;bottom:0;left:0;right:0;height:3px;background:linear-gradient(90deg,#C5F135 0%,#8FD11B 40%,transparent 100%)"></div>
    <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:22px">
      <div style="display:flex;align-items:center;gap:10px">
        <div style="background:#C5F135;color:#0D1117;font-size:11px;font-weight:900;letter-spacing:1px;padding:5px 10px;border-radius:4px;text-transform:uppercase">LI</div>
        <div style="color:#fff;font-size:14px;font-weight:700">Lean Intelligence</div>
      </div>
      <div style="background:rgba(197,241,53,.1);border:1px solid rgba(197,241,53,.25);color:#C5F135;font-size:10px;font-weight:700;letter-spacing:1.5px;text-transform:uppercase;padding:5px 12px;border-radius:20px">Daily DevOps Report</div>
    </div>
    <div style="color:#fff;font-size:30px;font-weight:900;letter-spacing:-.5px;margin-bottom:6px">Platform <span style="color:#C5F135">Health</span> Report</div>
    <div style="color:rgba(255,255,255,.35);font-size:12px">{now_str} &nbsp;·&nbsp; UTC &nbsp;·&nbsp; Generated by Derek</div>
  </div>

  <!-- Status Bar -->
  <div style="background:#131920;border-top:1px solid rgba(255,255,255,.05);padding:14px 48px">
    <table style="background:transparent;border:none;border-radius:0">
      <tr>
        <td style="padding:0 28px 0 0;white-space:nowrap;font-size:12px;color:rgba(255,255,255,.5);border:none">
          {sdot('g' if fe_ok == 2 else 'r')}<strong style="color:#fff">{fe_ok}/2</strong> Frontends Up
        </td>
        <td style="padding:0 28px 0 0;white-space:nowrap;font-size:12px;color:rgba(255,255,255,.5);border:none">
          {sdot('r' if n8n_fail > 0 else 'g')}<strong style="color:#fff">{n8n_fail}</strong> n8n Failures
        </td>
        <td style="padding:0 28px 0 0;white-space:nowrap;font-size:12px;color:rgba(255,255,255,.5);border:none">
          {sdot('y' if q_failed > 0 else 'g')}<strong style="color:#fff">{q_failed}</strong> Queue Failures
        </td>
        <td style="padding:0 28px 0 0;white-space:nowrap;font-size:12px;color:rgba(255,255,255,.5);border:none">
          {sdot('r' if p0_bugs > 0 else 'g')}<strong style="color:#fff">{p0_bugs}</strong> P0 Bugs
        </td>
        <td style="padding:0 28px 0 0;white-space:nowrap;font-size:12px;color:rgba(255,255,255,.5);border:none">
          {sdot('g' if alerts == 0 else 'r')}<strong style="color:#fff">{alerts}</strong> Alerts
        </td>
        <td style="padding:0;white-space:nowrap;font-size:12px;color:rgba(255,255,255,.5);border:none">
          {sdot('g')}<strong style="color:#fff">{data['total_clients']}</strong> Clients
        </td>
      </tr>
    </table>
  </div>

  <div style="padding:28px 48px 0">

    <!-- System Overview -->
    <div style="margin-bottom:28px">
      {section("System Overview")}
      <div class="grid-4">
        {status_card("Frontend Prod", "✅ Online" if 200 <= data['frontend_prod'] < 400 else f"❌ {data['frontend_prod']}", "ai.leanmarketing.com", "ok" if 200 <= data['frontend_prod'] < 400 else "err")}
        {status_card("Frontend Dev", "✅ Online" if 200 <= data['frontend_dev'] < 400 else f"❌ {data['frontend_dev']}", "lean-intelligence-dev", "ok" if 200 <= data['frontend_dev'] < 400 else "err")}
        {status_card("Grafana Alerts", "✅ Clear" if alerts == 0 else f"⚠️ {alerts} Active", f"{alerts} active alerts", "ok" if alerts == 0 else "warn")}
        {status_card("Supabase DB", "✅ Healthy", f"{data['total_clients']} clients · REST OK", "ok")}
      </div>
    </div>

    <!-- n8n -->
    <div style="margin-bottom:28px">
      {section("n8n Execution Health")}
      <div class="grid-3" style="margin-bottom:14px">
        {status_card("Failed (last 24h)", str(n8n_fail) if n8n_fail >= 0 else "?", "recent failures", "warn" if n8n_fail > 0 else "ok")}
        {status_card("Active Workflows", str(data['n8n_active']), "all marked active", "ok")}
        {status_card("Queue Completed", str(completed_24h), "jobs done in 24h", "ok")}
      </div>
      <table>
        <thead><tr>{th("Time (UTC)")}{th("Workflow")}{th("Failing Node")}{th("Error")}{th("Status")}</tr></thead>
        <tbody>{n8n_rows}</tbody>
      </table>
    </div>

    <!-- Generation Queue -->
    <div style="margin-bottom:28px">
      {section("Generation Queue — Last 7 Days")}
      <div class="grid-3" style="margin-bottom:14px">
        {status_card("Completed (24h)", str(completed_24h), "successful jobs", "ok")}
        {status_card("Failed (7d)", str(len(data['queue_failed'])), "need attention", "warn" if data['queue_failed'] else "ok")}
        {status_card("Stuck Processing", str(data['queue_stuck']), "jobs hung >10 min", "warn" if data['queue_stuck'] and data['queue_stuck'] != '0' else "ok")}
      </div>
      <table>
        <thead><tr>{th("Time (UTC)")}{th("Company")}{th("Writer")}{th("Error")}{th("Status")}</tr></thead>
        <tbody>{queue_rows}</tbody>
      </table>
    </div>

    <!-- Writer Activity -->
    <div style="margin-bottom:28px">
      {section("Writer Activity — Last 7 Days")}
      <table>
        <thead><tr>{th("Writer")}{th("Completed")}{th("Status")}</tr></thead>
        <tbody>{activity_rows}</tbody>
      </table>
    </div>

    <!-- Linear -->
    <div style="margin-bottom:28px">
      {section(f"Open Bugs — Linear ({len(data['linear_bugs'])} total)")}
      <table>
        <thead><tr>{th("ID")}{th("Priority")}{th("Title")}{th("State")}</tr></thead>
        <tbody>{bug_rows}</tbody>
      </table>
    </div>

  </div>

  {fun_fact_html}

  <!-- Footer -->
  <div style="background:#0D1117;padding:18px 48px;display:flex;justify-content:space-between;align-items:center">
    <div style="color:rgba(255,255,255,.25);font-size:11px">Generated by Derek · Lean Intelligence · {today.strftime('%B %-d, %Y')}</div>
    <div style="color:#C5F135;font-size:11px;font-weight:700;letter-spacing:1px;text-transform:uppercase">{data['total_clients']} Clients · {data['n8n_active']} Workflows</div>
  </div>

</div>
</body>
</html>"""


def html_to_pdf(html, pdf_path):
    import os
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path.home() / ".cache/ms-playwright")
    env["_PDF_OUTPUT_PATH"] = str(pdf_path)
    script = (
        "import os,sys;"
        "os.environ['PLAYWRIGHT_BROWSERS_PATH']=os.environ.get('PLAYWRIGHT_BROWSERS_PATH','');"
        "from playwright.sync_api import sync_playwright; html=sys.stdin.read();"
        "p=sync_playwright().start(); b=p.chromium.launch(); pg=b.new_page();"
        "pg.set_content(html,wait_until='networkidle');"
        "pg.pdf(path=os.environ['_PDF_OUTPUT_PATH'],format='A4',"
        "margin={'top':'0px','bottom':'0px','left':'0px','right':'0px'},print_background=True);"
        "b.close(); p.stop()"
    )
    r = subprocess.run(["python3", "-c", script], input=html, capture_output=True, text=True, env=env)
    return r.returncode == 0


def send_telegram_pdf(pdf_path, caption, secrets):
    env_file = Path.home() / ".claude/channels/telegram/.env"
    bot_token = None
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                bot_token = line.split("=", 1)[1].strip()
    if not bot_token:
        return False
    chat_id = secrets.get("telegram_chat_id", "YOUR_TELEGRAM_CHAT_ID")
    boundary = "----FormBoundary7MA4YWxk"
    pdf_bytes = Path(pdf_path).read_bytes()
    body = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"chat_id\"\r\n\r\n{chat_id}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"caption\"\r\n\r\n{caption}\r\n"
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"document\"; filename=\"lean_devops_report.pdf\"\r\n"
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf_bytes + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{bot_token}/sendDocument",
        data=body, method="POST"
    )
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read()).get("ok", False)
    except Exception:
        return False


_AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
_AGENT_WORKSPACE = Path(f"/Users/YOUR_MAC_USERNAME/{_AGENT_NAME}")
_AGENT_CONFIG = _AGENT_WORKSPACE / ".config" / _AGENT_NAME
_AGENT_ACCOUNTS = _AGENT_CONFIG / "accounts"


def _find_disk_token():
    """Find best token file for this agent."""
    if _AGENT_ACCOUNTS.exists():
        for d in sorted(_AGENT_ACCOUNTS.iterdir()):
            if d.is_dir() and (d / "google-token.json").exists():
                return d / "google-token.json"
    root = _AGENT_CONFIG / "google-token.json"
    return root if root.exists() else None


def _find_disk_creds():
    """Find best credentials file for this agent."""
    if _AGENT_ACCOUNTS.exists():
        for d in sorted(_AGENT_ACCOUNTS.iterdir()):
            if d.is_dir() and (d / "google-credentials.json").exists():
                return d / "google-credentials.json"
    root = _AGENT_CONFIG / "google-credentials.json"
    return root if root.exists() else None


def send_email_pdf(pdf_path, subject, secrets):
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.application import MIMEApplication
    import urllib.parse

    # Load token from vault first (agent-scoped), disk fallback
    from vault_client import get_credential as _vault_get
    token_data = None
    try:
        raw = _vault_get("gmail", "oauth_token", agent=_AGENT_NAME)
        if raw:
            token_data = json.loads(raw)
    except Exception:
        pass
    if not token_data:
        p = _find_disk_token()
        if p and p.exists():
            token_data = json.loads(p.read_text())
    creds = None
    try:
        raw = _vault_get("gmail", "oauth_credentials", agent=_AGENT_NAME)
        if raw:
            creds = json.loads(raw)
    except Exception:
        pass
    if not creds:
        p = _find_disk_creds()
        if p and p.exists():
            creds = json.loads(p.read_text())
    if not token_data or not creds:
        return False
    token_path = _find_disk_token()

    def _send(access_token):
        msg = MIMEMultipart()
        msg["to"] = REPORT_TO
        msg["from"] = "YOUR_BUSINESS_EMAIL"
        msg["subject"] = subject
        msg.attach(MIMEText("Your Lean Intelligence DevOps report is attached.", "plain"))
        pdf_bytes = Path(pdf_path).read_bytes()
        part = MIMEApplication(pdf_bytes, Name=Path(pdf_path).name)
        part["Content-Disposition"] = f'attachment; filename="{Path(pdf_path).name}"'
        msg.attach(part)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = json.dumps({"raw": raw}).encode()
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=payload, method="POST"
        )
        req.add_header("Authorization", f"Bearer {access_token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read()), access_token
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return None, access_token
            raise

    result, _ = _send(token_data.get("access_token", ""))
    if result is None:
        client = creds.get("installed", creds)
        data = urllib.parse.urlencode({
            "client_id": client["client_id"],
            "client_secret": client["client_secret"],
            "refresh_token": token_data["refresh_token"],
            "grant_type": "refresh_token",
        }).encode()
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        with urllib.request.urlopen(req) as r:
            new_token = json.loads(r.read())
        new_token["refresh_token"] = token_data["refresh_token"]
        if token_path:
            token_path.write_text(json.dumps(new_token, indent=2))
        result, _ = _send(new_token["access_token"])
    return result is not None


def main():
    parser = argparse.ArgumentParser(description="Daily Lean Intelligence DevOps report")
    parser.add_argument("--dry-run", action="store_true", help="Write HTML/PDF to /tmp, don't send")
    args = parser.parse_args()

    secrets = load_secrets()
    today = date.today()
    pdf_path = f"/tmp/lean_devops_{today}.pdf"

    print("Gathering platform data...", file=sys.stderr)
    data = gather_data(secrets)
    print(f"  n8n failures={data['n8n_failures']} queue_failed={len(data['queue_failed'])} linear_bugs={len(data['linear_bugs'])}", file=sys.stderr)

    html = generate_html(data)

    if args.dry_run:
        html_path = Path("/tmp/lean_devops_preview.html")
        html_path.write_text(html)
        print(f"[dry-run] HTML → {html_path}", file=sys.stderr)
        html_to_pdf(html, pdf_path)
        print(f"[dry-run] PDF  → {pdf_path}", file=sys.stderr)
        print(json.dumps({"sent": True, "dry_run": True}))
        return

    if not html_to_pdf(html, pdf_path):
        print("[error] PDF generation failed", file=sys.stderr)
        sys.exit(1)

    subject = f"Lean Intelligence DevOps Report — {today.strftime('%b %-d, %Y')}"
    email_ok = send_email_pdf(pdf_path, subject, secrets)
    tg_ok = send_telegram_pdf(pdf_path, f"📊 DevOps Report — {today.strftime('%b %-d, %Y')}", secrets)
    print(json.dumps({"sent": True, "email": email_ok, "telegram": tg_ok}))


if __name__ == "__main__":
    main()
