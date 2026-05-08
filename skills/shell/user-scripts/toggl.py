#!/usr/bin/env python3
"""Toggl Track API client — enny.ai time tracking.

Usage:
    python3 toggl.py current                          # currently running timer
    python3 toggl.py start --description "Task name" [--project-id ID]
    python3 toggl.py stop                             # stop current timer
    python3 toggl.py list [--days 7]                  # recent time entries
    python3 toggl.py summary [--days 7]               # hours by project/description
    python3 toggl.py add --description "Task" --start "2026-03-25T10:00:00" --duration 3600
    python3 toggl.py projects                         # list workspace projects
"""

import argparse
import base64
import json
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

TOGGL_API = "https://api.track.toggl.com/api/v9"


def _auth_header(token):
    creds = base64.b64encode(f"{token}:api_token".encode()).decode()
    return f"Basic {creds}"


def toggl_request(method, path, body=None, params=None, token=None):
    url = f"{TOGGL_API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header(token))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        print(json.dumps({"error": f"HTTP {e.code}", "detail": detail}), file=sys.stderr)
        sys.exit(1)


def cmd_current(args, secrets):
    result = toggl_request("GET", "/me/time_entries/current", token=secrets["toggl_api_token"])
    if not result:
        print(json.dumps({"running": False}))
        return
    print(json.dumps({
        "running": True,
        "id": result.get("id"),
        "description": result.get("description"),
        "project_id": result.get("project_id"),
        "start": result.get("start"),
        "duration": result.get("duration"),
    }, indent=2))


def cmd_start(args, secrets):
    wid = int(secrets["toggl_workspace_id"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "description": args.description,
        "workspace_id": wid,
        "start": now,
        "duration": -1,
        "created_with": "derek-agent",
    }
    if args.project_id:
        body["project_id"] = int(args.project_id)
    result = toggl_request("POST", f"/workspaces/{wid}/time_entries", body=body, token=secrets["toggl_api_token"])
    print(json.dumps({"started": True, "id": result.get("id"), "description": result.get("description")}, indent=2))


def cmd_stop(args, secrets):
    current = toggl_request("GET", "/me/time_entries/current", token=secrets["toggl_api_token"])
    if not current:
        print(json.dumps({"stopped": False, "reason": "no running timer"}))
        return
    wid = int(secrets["toggl_workspace_id"])
    tid = current["id"]
    result = toggl_request("PATCH", f"/workspaces/{wid}/time_entries/{tid}/stop", token=secrets["toggl_api_token"])
    print(json.dumps({"stopped": True, "id": tid, "description": result.get("description")}, indent=2))


def cmd_list(args, secrets):
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = toggl_request("GET", "/me/time_entries", params={"start_date": since, "end_date": until}, token=secrets["toggl_api_token"])
    output = []
    for e in (entries or []):
        dur = e.get("duration", 0)
        output.append({
            "id": e.get("id"),
            "description": e.get("description"),
            "project_id": e.get("project_id"),
            "start": e.get("start"),
            "duration_sec": dur if dur >= 0 else None,
            "duration_h": round(dur / 3600, 2) if dur >= 0 else None,
        })
    print(json.dumps({"count": len(output), "entries": output}, indent=2))


def cmd_summary(args, secrets):
    since = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    until = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    entries = toggl_request("GET", "/me/time_entries", params={"start_date": since, "end_date": until}, token=secrets["toggl_api_token"])
    by_desc = {}
    total_sec = 0
    for e in (entries or []):
        dur = e.get("duration", 0)
        if dur < 0:
            continue
        desc = e.get("description") or "(no description)"
        by_desc[desc] = by_desc.get(desc, 0) + dur
        total_sec += dur
    breakdown = sorted([{"description": k, "hours": round(v / 3600, 2)} for k, v in by_desc.items()], key=lambda x: -x["hours"])
    print(json.dumps({
        "days": args.days,
        "total_hours": round(total_sec / 3600, 2),
        "breakdown": breakdown,
    }, indent=2))


def cmd_add(args, secrets):
    wid = int(secrets["toggl_workspace_id"])
    body = {
        "description": args.description,
        "workspace_id": wid,
        "start": args.start,
        "duration": args.duration,
        "created_with": "derek-agent",
    }
    result = toggl_request("POST", f"/workspaces/{wid}/time_entries", body=body, token=secrets["toggl_api_token"])
    print(json.dumps({"added": True, "id": result.get("id")}, indent=2))


def cmd_projects(args, secrets):
    wid = int(secrets["toggl_workspace_id"])
    projects = toggl_request("GET", f"/workspaces/{wid}/projects", token=secrets["toggl_api_token"])
    output = [{"id": p["id"], "name": p["name"], "active": p.get("active")} for p in (projects or [])]
    print(json.dumps({"count": len(output), "projects": output}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Toggl Track time management (enny.ai)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("current")

    p_start = sub.add_parser("start")
    p_start.add_argument("--description", required=True)
    p_start.add_argument("--project-id", default=None)

    sub.add_parser("stop")

    p_list = sub.add_parser("list")
    p_list.add_argument("--days", type=int, default=7)

    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--days", type=int, default=7)

    p_add = sub.add_parser("add")
    p_add.add_argument("--description", required=True)
    p_add.add_argument("--start", required=True, help="ISO8601, e.g. 2026-03-25T10:00:00Z")
    p_add.add_argument("--duration", type=int, required=True, help="Duration in seconds")

    sub.add_parser("projects")

    args = parser.parse_args()
    secrets = load_secrets()

    {
        "current": cmd_current,
        "start": cmd_start,
        "stop": cmd_stop,
        "list": cmd_list,
        "summary": cmd_summary,
        "add": cmd_add,
        "projects": cmd_projects,
    }[args.command](args, secrets)


if __name__ == "__main__":
    main()
