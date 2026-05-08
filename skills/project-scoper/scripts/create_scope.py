#!/usr/bin/env python3
"""Create a Notion scope document from an idea in the ideas database.

Usage:
    python3 create_scope.py --id idea-20260304-001
    python3 create_scope.py --id idea-20260304-001 --problem "..." --solution "..." --features "feat1|feat2|feat3"
    python3 create_scope.py --title "My Project" --problem "..." --solution "..." --features "feat1|feat2"

If --id is given, pulls data from ideas.json. Fields can be overridden with flags.
If --title is given without --id, creates a scope from scratch (no idea database link).
"""

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

import notion_client as notion


def load_idea(idea_id):
    """Load an idea from the idea-hunter database."""
    script_dir = Path(__file__).resolve().parent
    workspace = script_dir.parent.parent.parent
    db_path = workspace / "memory" / "idea-hunter" / "ideas.json"
    if not db_path.exists():
        print(f'{{"error": "ideas.json not found at {db_path}"}}', file=sys.stderr)
        sys.exit(1)
    with open(db_path) as f:
        ideas = json.loads(f.read().strip())
    idea = next((i for i in ideas if i["id"] == idea_id), None)
    if not idea:
        print(f'{{"error": "idea not found: {idea_id}"}}', file=sys.stderr)
        sys.exit(1)
    return idea


def build_scope_blocks(title, problem, target_user, solution, features, tech_stack, wireframe=None, idea_id=None):
    """Build the Notion block structure for a scope document."""
    blocks = []

    # Problem
    blocks.append(notion.heading2("Problem"))
    blocks.append(notion.paragraph(problem or "[ Derek will fill this in after research ]"))

    # Target User
    blocks.append(notion.heading2("Target User"))
    blocks.append(notion.paragraph(target_user or "[ Derek will fill this in after research ]"))

    # MVP Features (checkboxes)
    blocks.append(notion.heading2("MVP Features"))
    if features:
        for feat in features:
            blocks.append(notion.todo(feat, checked=False))
    else:
        blocks.append(notion.todo("[ Derek will add features after research ]", checked=False))

    # Technical Approach
    blocks.append(notion.heading2("Technical Approach"))
    if tech_stack:
        for item in tech_stack:
            blocks.append(notion.bullet(item))
    else:
        blocks.append(notion.bullet("[ Derek will determine stack after scoping ]"))

    # Wireframe (Mermaid diagram)
    blocks.append(notion.heading2("Wireframe"))
    if wireframe:
        blocks.append(notion.code(wireframe, language="mermaid"))
    else:
        blocks.append(notion.paragraph("[ Derek will add a Mermaid diagram here ]"))

    # Instructions
    blocks.append(notion.divider())
    blocks.append(notion.callout(
        "Check the features you want, uncheck what to cut. "
        "Leave comments on anything you want changed. "
        "When ready, set status to Approved."
    ))

    return blocks


def main():
    parser = argparse.ArgumentParser(description="Create a Notion scope document")
    parser.add_argument("--id", default=None, help="Idea ID from ideas.json")
    parser.add_argument("--title", default=None, help="Project title (overrides idea title)")
    parser.add_argument("--problem", default=None, help="Problem statement (overrides idea)")
    parser.add_argument("--target-user", default=None, help="Target user description")
    parser.add_argument("--solution", default=None, help="Solution description (overrides idea)")
    parser.add_argument("--features", default=None, help="Pipe-separated MVP features")
    parser.add_argument("--tech", default=None, help="Pipe-separated tech stack items")
    parser.add_argument("--wireframe", default=None, help="Mermaid diagram string for the wireframe section")
    args = parser.parse_args()

    if not args.id and not args.title:
        print('{"error": "must provide --id or --title"}', file=sys.stderr)
        sys.exit(1)

    # Load from idea database if ID given
    idea = None
    if args.id:
        idea = load_idea(args.id)

    # Resolve fields (flags override idea data)
    title = args.title or (idea["title"] if idea else "Untitled Project")
    problem = args.problem or (idea.get("problem", "") if idea else "")
    solution = args.solution or (idea.get("solution", "") if idea else "")
    target_user = args.target_user or ""
    features = args.features.split("|") if args.features else []
    tech_stack = args.tech.split("|") if args.tech else []
    wireframe = args.wireframe.replace('\\n', '\n') if args.wireframe else None
    score = idea.get("total_score", 0) if idea else 0
    source = ""
    if idea:
        source = idea.get("id", "")
        if idea.get("evidence"):
            first = idea["evidence"][0]
            source = f"{idea['id']} — {first.get('source', '')} {first.get('url', '')}"

    # Create Notion page
    db_id = notion.get_database_id()
    today = datetime.now().strftime("%Y-%m-%d")

    properties = {
        "Name": {"title": [{"text": {"content": title}}]},
        "Status": {"select": {"name": "Scoping"}},
        "Score": {"number": score},
        "Source Idea": {"rich_text": [{"text": {"content": source}}]},
        "Created": {"date": {"start": today}},
    }

    page = notion.create_page(db_id, properties)
    page_id = page["id"]
    page_url = page.get("url", "")

    # Add scope content blocks
    blocks = build_scope_blocks(title, problem, target_user, solution, features, tech_stack, wireframe, args.id)
    notion.append_blocks(page_id, blocks)

    result = {
        "action": "created",
        "page_id": page_id,
        "url": page_url,
        "title": title,
        "status": "Scoping",
    }
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
