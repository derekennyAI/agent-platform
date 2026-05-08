#!/usr/bin/env python3
"""BugHerd API client — Change Influence project bug/task management.

Usage:
    python3 bugherd.py list [--project-id ID]
    python3 bugherd.py get <task_id>
    python3 bugherd.py create --title "Bug title" [--description "..."] [--priority low|normal|high|critical]
    python3 bugherd.py update <task_id> --status backlog|todo|doing|done|closed [--priority ...]
    python3 bugherd.py comment <task_id> --text "Comment text"
"""

import argparse
import base64
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

BUGHERD_API = "https://www.bugherd.com/api_v2"


def _auth_header(api_key):
    token = base64.b64encode(f"{api_key}:x".encode()).decode()
    return f"Basic {token}"


def bugherd_request(method, path, body=None, api_key=None, project_id=None):
    url = f"{BUGHERD_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", _auth_header(api_key))
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        err_body = e.read().decode()
        print(json.dumps({"error": f"HTTP {e.code}", "detail": err_body}), file=sys.stderr)
        sys.exit(1)


def cmd_list(args, secrets):
    pid = args.project_id or secrets["bugherd_project_id"]
    result = bugherd_request("GET", f"/projects/{pid}/tasks.json", api_key=secrets["bugherd_api_key"])
    tasks = result.get("tasks", [])
    output = []
    for t in tasks:
        output.append({
            "id": t["id"],
            "title": t.get("description", ""),
            "status": t.get("status", ""),
            "priority": t.get("priority", ""),
            "requester": t.get("requester_email", ""),
            "created": t.get("created_at", ""),
        })
    print(json.dumps({"count": len(output), "tasks": output}, indent=2))


def cmd_get(args, secrets):
    pid = secrets["bugherd_project_id"]
    result = bugherd_request("GET", f"/projects/{pid}/tasks/{args.task_id}.json", api_key=secrets["bugherd_api_key"])
    t = result.get("task", result)
    print(json.dumps(t, indent=2))


def cmd_create(args, secrets):
    pid = secrets["bugherd_project_id"]
    body = {"task": {"description": args.title}}
    if args.description:
        body["task"]["annotation"] = {"description": args.description}
    if args.priority:
        body["task"]["priority"] = args.priority
    result = bugherd_request("POST", f"/projects/{pid}/tasks.json", body=body, api_key=secrets["bugherd_api_key"])
    t = result.get("task", result)
    print(json.dumps({"created": True, "id": t.get("id"), "title": t.get("description")}, indent=2))


def cmd_update(args, secrets):
    pid = secrets["bugherd_project_id"]
    body = {"task": {}}
    if args.status:
        body["task"]["status"] = args.status
    if args.priority:
        body["task"]["priority"] = args.priority
    result = bugherd_request("PUT", f"/projects/{pid}/tasks/{args.task_id}.json", body=body, api_key=secrets["bugherd_api_key"])
    t = result.get("task", result)
    print(json.dumps({"updated": True, "id": t.get("id"), "status": t.get("status")}, indent=2))


def cmd_comment(args, secrets):
    pid = secrets["bugherd_project_id"]
    body = {"comment": {"text": args.text}}
    result = bugherd_request("POST", f"/projects/{pid}/tasks/{args.task_id}/comments.json", body=body, api_key=secrets["bugherd_api_key"])
    print(json.dumps({"commented": True, "task_id": args.task_id}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="BugHerd task management (Change Influence)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--project-id", default=None)

    p_get = sub.add_parser("get")
    p_get.add_argument("task_id")

    p_create = sub.add_parser("create")
    p_create.add_argument("--title", required=True)
    p_create.add_argument("--description", default=None)
    p_create.add_argument("--priority", choices=["low", "normal", "high", "critical"], default=None)

    p_update = sub.add_parser("update")
    p_update.add_argument("task_id")
    p_update.add_argument("--status", choices=["backlog", "todo", "doing", "done", "closed"], default=None)
    p_update.add_argument("--priority", choices=["low", "normal", "high", "critical"], default=None)

    p_comment = sub.add_parser("comment")
    p_comment.add_argument("task_id")
    p_comment.add_argument("--text", required=True)

    args = parser.parse_args()
    secrets = load_secrets()

    {
        "list": cmd_list,
        "get": cmd_get,
        "create": cmd_create,
        "update": cmd_update,
        "comment": cmd_comment,
    }[args.command](args, secrets)


if __name__ == "__main__":
    main()
