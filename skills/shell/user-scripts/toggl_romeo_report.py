#!/usr/bin/env python3
"""Generate and email bi-weekly Toggl time report for Romeo (romeo@enny.ai).

Pulls Romeo's time entries from the Toggl Reports API and emails an HTML
breakdown to YOUR_ADMIN_EMAIL.

Usage:
    python3 toggl_romeo_report.py --start 2026-03-15 --end 2026-03-28
    python3 toggl_romeo_report.py --historical       # generate all past 14-day periods
    python3 toggl_romeo_report.py --next-period      # auto-calculate current period ending today's Saturday
"""

import argparse
import base64
import json
import subprocess
import sys
import urllib.request
import urllib.error
from datetime import date, timedelta
from pathlib import Path

_WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent
import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

TOGGL_API = "https://api.track.toggl.com/api/v9"
REPORTS_API = "https://api.track.toggl.com/reports/api/v3"
REPORT_TO = "YOUR_ADMIN_EMAIL"

# Anchor date: first report Saturday
ANCHOR = date(2026, 3, 28)
# Don't generate reports before this date
HISTORY_START = date(2025, 9, 27)  # ~6 months back


def _auth(token):
    return "Basic " + base64.b64encode(f"{token}:api_token".encode()).decode()


def toggl_get(path, token):
    req = urllib.request.Request(f"{TOGGL_API}{path}")
    req.add_header("Authorization", _auth(token))
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read())


def reports_search(wid, start, end, token):
    """Search time entries via Reports API v3."""
    url = f"{REPORTS_API}/workspace/{wid}/search/time_entries"
    body = json.dumps({
        "start_date": str(start),
        "end_date": str(end),
        "user_ids": [load_secrets().get("romeo_toggl_user_id", 12406328)],
        "order_by": "date",
        "order_dir": "asc",
    }).encode()
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Authorization", _auth(token))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        print(f"[error] Reports API {e.code}: {e.read().decode()}", file=sys.stderr)
        return []


def get_projects(wid, token):
    projects = toggl_get(f"/workspaces/{wid}/projects?active=both&per_page=200", token)
    return {p["id"]: p["name"] for p in (projects or [])}


def sec_to_h(seconds):
    h = seconds // 3600
    m = (seconds % 3600) // 60
    return f"{h}h {m:02d}m"


def generate_html(start, end, entries, project_map):
    """Build an HTML report from time entries."""
    # Group by project
    by_project = {}
    for entry in entries:
        pid = entry.get("project_id")
        pname = project_map.get(pid, "No Project") if pid else "No Project"
        dur = entry.get("time_entries", [{}])[0].get("seconds", 0) if entry.get("time_entries") else 0
        # Some API versions return duration differently
        if not dur and entry.get("seconds"):
            dur = entry["seconds"]
        if pname not in by_project:
            by_project[pname] = {"seconds": 0, "entries": []}
        by_project[pname]["seconds"] += dur
        desc = entry.get("description") or "(no description)"
        entry_date = (entry.get("time_entries") or [{}])[0].get("start", "")[:10] if entry.get("time_entries") else ""
        by_project[pname]["entries"].append({"date": entry_date, "desc": desc, "seconds": dur})

    total_sec = sum(v["seconds"] for v in by_project.values())
    total_hours = total_sec / 3600
    secrets = load_secrets()
    romeo_rate = secrets.get("romeo_rate_usd", 60)
    romeo_wise_url = secrets.get("romeo_wise_url", "")
    amount_due = total_hours * romeo_rate

    # Sort projects by hours desc
    sorted_projects = sorted(by_project.items(), key=lambda x: -x[1]["seconds"])

    # Build project rows HTML
    project_rows = ""
    for pname, data in sorted_projects:
        entry_rows = "".join(
            f"<tr><td style='padding:4px 8px;color:#666;font-size:13px'>{e['date']}</td>"
            f"<td style='padding:4px 8px;color:#555;font-size:13px'>{e['desc']}</td>"
            f"<td style='padding:4px 8px;text-align:right;color:#555;font-size:13px'>{sec_to_h(e['seconds'])}</td></tr>"
            for e in data["entries"]
        )
        project_rows += f"""
        <tr>
            <td colspan='3' style='padding:10px 8px 4px;font-weight:600;color:#333;border-top:1px solid #eee'>
                {pname}
                <span style='font-weight:400;color:#888;font-size:13px;margin-left:8px'>{sec_to_h(data["seconds"])}</span>
            </td>
        </tr>
        {entry_rows}
        """

    period_label = f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"

    return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;max-width:600px;margin:0 auto;padding:24px;color:#222">
  <div style="border-left:4px solid #4a90d9;padding-left:16px;margin-bottom:24px">
    <h2 style="margin:0 0 4px;font-size:20px">Romeo — Time Report</h2>
    <p style="margin:0;color:#666;font-size:14px">{period_label}</p>
  </div>

  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;border-radius:8px;margin-bottom:20px">
    <tr>
      <td width="50%" style="padding:20px 24px;border-right:1px solid #e8eaed">
        <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Total Hours</div>
        <div style="font-size:26px;font-weight:700;color:#2c3e50;line-height:1">{sec_to_h(total_sec)}</div>
      </td>
      <td width="50%" style="padding:20px 24px">
        <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Amount Due</div>
        <div style="font-size:26px;font-weight:700;color:#27ae60;line-height:1">${amount_due:,.2f}</div>
        <div style="font-size:12px;color:#bbb;margin-top:4px">@ ${romeo_rate}/hr</div>
      </td>
    </tr>
  </table>

  <table cellpadding="0" cellspacing="0" style="margin-bottom:28px">
    <tr>
      <td style="background:#37b5f0;border-radius:6px">
        <a href="{romeo_wise_url}" style="display:block;color:#fff;text-decoration:none;padding:11px 22px;font-weight:600;font-size:14px;white-space:nowrap">Pay Romeo on Wise &rarr;</a>
      </td>
    </tr>
  </table>

  {"<p style='color:#888;font-style:italic'>No time entries logged this period.</p>" if not entries else f"""
  <table style="width:100%;border-collapse:collapse">
    {project_rows}
  </table>
  """}

  <p style="margin-top:32px;font-size:12px;color:#aaa;border-top:1px solid #eee;padding-top:12px">
    Generated by Derek · enny.ai
  </p>
</body>
</html>"""


def send_report(start, end, html, dry_run=False):
    subject = f"Romeo Time Report — {start.strftime('%b %-d')}–{end.strftime('%-d, %Y')}"
    send_script = _WORKSPACE / "skills" / "idea-hunter" / "scripts" / "send_email.py"
    if dry_run:
        print(f"[dry-run] Would send: {subject}")
        return True
    result = subprocess.run(
        ["python3", str(send_script), "--to", REPORT_TO, "--subject", subject, "--stdin", "--html"],
        input=html,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        print(f"[error] send_email failed: {result.stderr}", file=sys.stderr)
        return False
    print(f"Sent: {subject}")
    return True


def periods_before(anchor, stop):
    """Generate 14-day periods going backwards from anchor."""
    end = anchor
    while end > stop:
        start = end - timedelta(days=13)
        if start < stop:
            start = stop
        yield start, end
        end = end - timedelta(days=14)


def run_period(start, end, secrets, project_map, dry_run=False):
    wid = secrets["toggl_workspace_id"]
    token = secrets["toggl_api_token"]
    print(f"Fetching {start} to {end}...", file=sys.stderr)
    entries = reports_search(wid, start, end, token)
    html = generate_html(start, end, entries, project_map)
    if not entries:
        print(f"  No entries for {start}–{end}, skipping email.", file=sys.stderr)
        return False
    return send_report(start, end, html, dry_run=dry_run)


def main():
    parser = argparse.ArgumentParser(description="Bi-weekly Toggl report for Romeo")
    parser.add_argument("--start", help="Period start date (YYYY-MM-DD)")
    parser.add_argument("--end", help="Period end date (YYYY-MM-DD)")
    parser.add_argument("--historical", action="store_true", help="Generate all past periods back to HISTORY_START")
    parser.add_argument("--next-period", action="store_true", help="Auto-calculate period ending on most recent anchor Saturday")
    parser.add_argument("--dry-run", action="store_true", help="Print report, don't email")
    args = parser.parse_args()

    secrets = load_secrets()
    wid = secrets["toggl_workspace_id"]
    token = secrets["toggl_api_token"]
    project_map = get_projects(wid, token)

    if args.historical:
        sent = 0
        for start, end in periods_before(ANCHOR, HISTORY_START):
            ok = run_period(start, end, secrets, project_map, dry_run=args.dry_run)
            if ok:
                sent += 1
        print(json.dumps({"sent": sent}))

    elif args.next_period:
        # Find the most recent anchor Saturday on or before today
        today = date.today()
        days_since = (today - ANCHOR).days
        periods_elapsed = days_since // 14
        end = ANCHOR + timedelta(days=periods_elapsed * 14)
        if end > today:
            end = end - timedelta(days=14)
        start = end - timedelta(days=13)
        ok = run_period(start, end, secrets, project_map, dry_run=args.dry_run)
        print(json.dumps({"sent": ok, "period": f"{start}/{end}"}))

    elif args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
        ok = run_period(start, end, secrets, project_map, dry_run=args.dry_run)
        print(json.dumps({"sent": ok, "period": f"{start}/{end}"}))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
