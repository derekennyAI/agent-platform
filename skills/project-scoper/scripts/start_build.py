#!/usr/bin/env python3
"""Initialize a build from an approved dev doc.

Run once per project after create_dev_doc.py has been executed.
Creates local state, initializes the code directory, and sets Phase 0 as IN PROGRESS in Notion.

Usage:
    python3 start_build.py \
        --scope-id <notion-page-id> \
        --dev-doc-id <notion-page-id> \
        --block-map-file memory/builds/contractor-invoicing/block-map.json \
        --slug contractor-invoicing \
        --phases '[{"name":"Phase 0: Landing Page","task_count":2},{"name":"Phase 1: Auth","task_count":6}]'
"""

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

import notion_client as notion


def _workspace():
    return Path(__file__).resolve().parent.parent.parent.parent


def main():
    parser = argparse.ArgumentParser(description="Initialize a project build")
    parser.add_argument("--scope-id", required=True, help="Notion page ID of the scope doc")
    parser.add_argument("--dev-doc-id", required=True, help="Notion page ID of the dev doc page")
    parser.add_argument("--block-map-file", required=True, help="Path to block-map.json from create_dev_doc.py")
    parser.add_argument("--slug", required=True, help="Project slug (e.g. contractor-invoicing)")
    parser.add_argument("--phases", required=True, help='JSON: [{"name":"Phase 0: Landing Page","task_count":2},...]')
    args = parser.parse_args()

    ws = _workspace()

    # Validate slug to prevent path traversal
    if not re.match(r'^[a-z0-9][a-z0-9-]*$', args.slug):
        print("Error: --slug must be lowercase alphanumeric with hyphens only", file=sys.stderr)
        sys.exit(1)

    # Load block map
    bm_path = Path(args.block_map_file)
    if not bm_path.exists():
        bm_path = ws / args.block_map_file
    if not bm_path.exists():
        print(f"Error: block-map file not found: {args.block_map_file}", file=sys.stderr)
        sys.exit(1)

    with open(bm_path) as f:
        block_map = json.load(f)

    phases = json.loads(args.phases)
    if not phases:
        print("Error: --phases must be a non-empty JSON array", file=sys.stderr)
        sys.exit(1)

    # 1. Create build memory directory
    build_dir = ws / "memory" / "builds" / args.slug
    build_dir.mkdir(parents=True, exist_ok=True)

    # 2. Create project code directory + git init
    code_dir = ws / "projects" / args.slug
    code_dir.mkdir(parents=True, exist_ok=True)
    if not (code_dir / ".git").exists():
        result = subprocess.run(
            ["git", "init"],
            cwd=str(code_dir),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"Warning: git init failed: {result.stderr}", file=sys.stderr)
        else:
            print(f"Git repo initialized at {code_dir}", file=sys.stderr)

    # 3. Build initial state
    phase_statuses = {
        str(i): ("in_progress" if i == 0 else "not_started")
        for i in range(len(phases))
    }

    state = {
        "project": args.slug,
        "scope_page_id": args.scope_id,
        "dev_doc_page_id": args.dev_doc_id,
        "code_dir": f"projects/{args.slug}",
        "block_map": block_map,
        "phases": phases,
        "current_phase": 0,
        "current_task": 0,
        "phase_statuses": phase_statuses,
        "blocked_on": None,
        "waiting_for_farlen": False,
        "last_commit": None,
    }

    state_file = build_dir / "state.json"
    with open(state_file, "w") as f:
        json.dump(state, f, indent=2)

    # 4. Update Notion
    phase_0_name = phases[0]["name"]
    task_count_0 = phases[0]["task_count"]
    banner_text = f"{phase_0_name} — Task 1 of {task_count_0}"

    if "phase_0_status" in block_map:
        notion.update_callout(block_map["phase_0_status"], "IN PROGRESS", "🟡")
    if "status_banner" in block_map:
        notion.update_callout(block_map["status_banner"], banner_text, "🔄")

    result = {
        "action": "initialized",
        "slug": args.slug,
        "state_file": str(state_file),
        "code_dir": str(code_dir),
        "phase_count": len(phases),
        "banner": banner_text,
    }
    print(json.dumps(result, indent=2))
    print("\nNext: check memory/dev-docs/{}.json for required env vars to ask Farlen for.".format(args.slug), file=sys.stderr)


if __name__ == "__main__":
    main()
