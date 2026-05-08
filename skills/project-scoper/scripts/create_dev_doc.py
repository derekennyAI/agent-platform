#!/usr/bin/env python3
"""Create a comprehensive phased development document in Notion.

Reads a dev doc JSON spec and creates a structured child page under the
approved scope document. See SKILL.md for the full JSON schema.

Usage:
    python3 create_dev_doc.py --scope-id <notion-page-id> --from-file memory/dev-docs/project.json [--slug project-slug]
"""

import argparse
import json
import sys
from pathlib import Path

import notion_client as notion


def _workspace():
    return Path(__file__).resolve().parent.parent.parent.parent


def render_env_vars(env_vars):
    blocks, labels = [], []
    blocks.append(notion.heading2("Environment Variables"))
    labels.append(None)
    for v in env_vars:
        line = v["name"]
        if v.get("description"):
            line += f" — {v['description']}"
        if v.get("example"):
            line += f" (e.g. {v['example']})"
        blocks.append(notion.bullet(line))
        labels.append(None)
    return blocks, labels


def render_database_schema(schema):
    blocks, labels = [], []
    blocks.append(notion.heading2("Database Schema"))
    labels.append(None)
    for table in schema:
        blocks.append(notion.heading3(f"Table: {table['table']}"))
        labels.append(None)
        lines = [f"-- {table['table']}"]
        for col in table.get("columns", []):
            note = f"  -- {col['notes']}" if col.get("notes") else ""
            lines.append(f"  {col['name']}  {col['type']}{note}")
        blocks.append(notion.code("\n".join(lines), language="sql"))
        labels.append(None)
    return blocks, labels


def render_api_routes(routes):
    blocks, labels = [], []
    blocks.append(notion.heading2("API Routes"))
    labels.append(None)
    for route in routes:
        method = route.get("method", "GET")
        path = route.get("path", "")
        auth = " (auth)" if route.get("auth") else ""
        blocks.append(notion.heading3(f"{method} {path}{auth}"))
        labels.append(None)
        if route.get("description"):
            blocks.append(notion.paragraph(route["description"]))
            labels.append(None)
        body_parts = {}
        if route.get("body"):
            body_parts["request"] = route["body"]
        if route.get("returns"):
            body_parts["returns"] = route["returns"]
        if body_parts:
            blocks.append(notion.code(json.dumps(body_parts, indent=2), language="json"))
            labels.append(None)
    return blocks, labels


def render_phases_with_labels(phases):
    blocks, labels = [], []
    for n, phase in enumerate(phases):
        blocks.append(notion.heading2(phase["name"]))
        labels.append(None)

        blocks.append(notion.callout("NOT STARTED", emoji="🔴"))
        labels.append(f"phase_{n}_status")

        for m, task in enumerate(phase.get("tasks", [])):
            children = []
            if task.get("details"):
                children.append(notion.paragraph(task["details"]))
            if task.get("commands"):
                children.append(notion.code("\n".join(task["commands"]), language="bash"))
            for f in task.get("files_created", []):
                children.append(notion.bullet(f))
            if task.get("test_command"):
                children.append(notion.callout(f"Test: {task['test_command']}", emoji="🧪"))
            if task.get("notes"):
                children.append(notion.callout(task["notes"], emoji="💡"))

            if children:
                blocks.append(notion.todo_with_children(task["title"], children))
            else:
                blocks.append(notion.todo(task["title"]))
            labels.append(f"phase_{n}_task_{m}")

        if phase.get("done_when"):
            blocks.append(notion.callout(f"Done when: {phase['done_when']}", emoji="✅"))
            labels.append(None)

        blocks.append(notion.divider())
        labels.append(None)

    return blocks, labels


def main():
    parser = argparse.ArgumentParser(description="Create a dev doc Notion page")
    parser.add_argument("--scope-id", required=True, help="Notion page ID of the approved scope")
    parser.add_argument("--from-file", required=True, help="Path to dev doc JSON spec file")
    parser.add_argument("--slug", default="", help="Project slug — if set, writes block-map to memory/builds/{slug}/block-map.json")
    args = parser.parse_args()

    ws = _workspace()
    spec_path = Path(args.from_file)
    if not spec_path.exists():
        spec_path = ws / args.from_file
    if not spec_path.exists():
        print(f'{{"error": "file not found: {args.from_file}"}}', file=sys.stderr)
        sys.exit(1)

    with open(spec_path) as f:
        spec = json.load(f)

    title = spec.get("title", "Dev Plan")
    page_title = f"Dev Plan — {title}" if not title.startswith("Dev Plan") else title

    page = notion.create_child_page(args.scope_id, page_title)
    page_id = page["id"]
    page_url = page.get("url", "")

    all_blocks = []
    all_labels = []

    # Status banner at top
    all_blocks.append(notion.callout("Build not started — run start_build.py to initialize", emoji="🔄"))
    all_labels.append("status_banner")

    if spec.get("overview"):
        all_blocks.append(notion.heading2("Overview"))
        all_labels.append(None)
        all_blocks.append(notion.paragraph(spec["overview"]))
        all_labels.append(None)

    if spec.get("stack"):
        all_blocks.append(notion.heading2("Tech Stack"))
        all_labels.append(None)
        for item in spec["stack"]:
            all_blocks.append(notion.bullet(item))
            all_labels.append(None)

    if spec.get("environment_vars"):
        b, l = render_env_vars(spec["environment_vars"])
        all_blocks.extend(b)
        all_labels.extend(l)

    if spec.get("database_schema"):
        b, l = render_database_schema(spec["database_schema"])
        all_blocks.extend(b)
        all_labels.extend(l)

    if spec.get("api_routes"):
        b, l = render_api_routes(spec["api_routes"])
        all_blocks.extend(b)
        all_labels.extend(l)

    phases = spec.get("phases", [])
    if phases:
        b, l = render_phases_with_labels(phases)
        all_blocks.extend(b)
        all_labels.extend(l)

    created = notion.append_blocks_with_ids(page_id, all_blocks)

    block_map = {}
    for label, block in zip(all_labels, created):
        if label:
            block_map[label] = block["id"]

    phase_task_counts = {
        str(n): len(phase.get("tasks", []))
        for n, phase in enumerate(phases)
    }

    if args.slug:
        build_dir = ws / "memory" / "builds" / args.slug
        build_dir.mkdir(parents=True, exist_ok=True)
        map_file = build_dir / "block-map.json"
        with open(map_file, "w") as f:
            json.dump(block_map, f, indent=2)
        print(f"Block map written to {map_file}", file=sys.stderr)

    result = {
        "action": "created",
        "page_id": page_id,
        "url": page_url,
        "title": page_title,
        "phase_count": len(phases),
        "block_count": len(all_blocks),
        "block_map": block_map,
        "phase_task_counts": phase_task_counts,
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
