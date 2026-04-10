#!/usr/bin/env python3
"""Load default crons into Supabase for a new agent.

Usage:
    python3 load_crons.py --agent vera
    python3 load_crons.py --agent vera --crons ../configs/default-crons.json
    python3 load_crons.py --agent vera --dry-run
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone

PLATFORM_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CRONS = PLATFORM_DIR / "configs" / "default-crons.json"

# Load .env
ENV_FILE = PLATFORM_DIR / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")


def supabase_request(path, method="GET", body=None):
    url = f"{SUPABASE_URL}/rest/v1/{path}"
    headers = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        print(f"  ERROR: {e.code} — {err}")
        return None


def get_existing_crons(agent_name):
    """Get IDs of crons already loaded for this agent."""
    result = supabase_request(
        f"harness_scheduled_tasks?agent_name=eq.{agent_name}&select=id"
    )
    if result:
        return {r["id"] for r in result}
    return set()


def load_crons(agent_name, crons_file, dry_run=False):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_KEY must be set (in .env or environment)")
        sys.exit(1)

    crons = json.loads(crons_file.read_text())
    existing = get_existing_crons(agent_name)

    print(f"\nLoading {len(crons)} default crons for agent '{agent_name}'")
    if existing:
        print(f"  ({len(existing)} crons already exist)")
    print()

    loaded = 0
    skipped = 0

    for cron in crons:
        task_id = f"{agent_name}_{cron['id']}"

        if task_id in existing:
            print(f"  SKIP  {task_id} — already exists")
            skipped += 1
            continue

        row = {
            "id": task_id,
            "agent_name": agent_name,
            "schedule": cron["schedule"],
            "task_description": cron["task_description"],
            "recurring": cron.get("recurring", True),
            "active": cron.get("active", True),
            "created_by": "setup",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }

        if dry_run:
            print(f"  DRY   {task_id}: {cron['schedule']} — {cron['task_description'][:60]}")
        else:
            result = supabase_request("harness_scheduled_tasks", method="POST", body=row)
            if result:
                print(f"  OK    {task_id}: {cron['schedule']} — {cron['task_description'][:60]}")
                loaded += 1
            else:
                print(f"  FAIL  {task_id}")

    print(f"\nDone: {loaded} loaded, {skipped} skipped")


def main():
    parser = argparse.ArgumentParser(description="Load default crons for an agent")
    parser.add_argument("--agent", required=True, help="Agent name")
    parser.add_argument("--crons", default=str(DEFAULT_CRONS), help="Path to crons JSON")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be loaded")
    args = parser.parse_args()

    crons_file = Path(args.crons)
    if not crons_file.exists():
        print(f"ERROR: Crons file not found: {crons_file}")
        sys.exit(1)

    load_crons(args.agent, crons_file, args.dry_run)


if __name__ == "__main__":
    main()
