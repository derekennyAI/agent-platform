#!/usr/bin/env python3
"""Scaffold a project directory from a selected idea.

Creates a standard project structure with README, PRD, and validation plan
pre-populated from the idea's data in Supabase.

Usage:
    python3 scaffold_project.py --id idea-20260304-001
    python3 scaffold_project.py --id idea-20260304-001 --output ~/projects/my-saas
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime
from pathlib import Path


def get_supabase_config():
    """Load Supabase URL and key from env vars. Prefers PERSONAL_ prefix for personal-tools project."""
    url = os.environ.get("PERSONAL_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    key = os.environ.get("PERSONAL_SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase credentials not found (set PERSONAL_SUPABASE_URL/PERSONAL_SUPABASE_KEY or SUPABASE_URL/SUPABASE_SERVICE_KEY)")
    return url.rstrip("/"), key


def load_idea(idea_id):
    """Load a specific idea from Supabase."""
    url, key = get_supabase_config()
    endpoint = f"{url}/rest/v1/ideas?id=eq.{urllib.parse.quote(idea_id)}"
    req = urllib.request.Request(endpoint, headers={
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            rows = json.loads(resp.read().decode())
            if not rows:
                print(f"[error] idea '{idea_id}' not found in Supabase", file=sys.stderr)
                sys.exit(1)
            return rows[0]
    except urllib.error.HTTPError as e:
        print(f"[error] Supabase query failed [{e.code}]: {e.read().decode()}", file=sys.stderr)
        sys.exit(1)


def slugify(text):
    """Convert text to a filesystem-safe slug."""
    return "-".join(
        "".join(c if c.isalnum() else " " for c in text.lower()).split()
    )[:50]


def scaffold(idea, output_dir):
    """Create project directory structure from idea data."""
    project_name = slugify(idea["title"])
    project_dir = output_dir / project_name

    if project_dir.exists():
        print(f"[error] directory already exists: {project_dir}", file=sys.stderr)
        sys.exit(1)

    # Create structure
    (project_dir / "docs").mkdir(parents=True)
    (project_dir / "src").mkdir()
    (project_dir / "tests").mkdir()

    # Normalize evidence/scores (may be strings from Supabase JSON columns)
    evidence = idea.get("evidence", [])
    if isinstance(evidence, str):
        evidence = json.loads(evidence)
    scores = idea.get("scores", {})
    if isinstance(scores, str):
        scores = json.loads(scores)

    # README.md
    tags = ", ".join(idea.get("tags", []))
    evidence_lines = []
    for e in evidence:
        evidence_lines.append(f"- [{e.get('source', 'unknown')}]({e.get('url', '#')}): \"{e.get('quote', '')}\" ({e.get('upvotes', 0)} upvotes)")

    readme = f"""# {idea['title']}

{idea.get('solution', 'TBD')}

## Problem

{idea.get('problem', 'TBD')}

## Evidence

{chr(10).join(evidence_lines) if evidence_lines else '- TBD'}

## Tags

{tags if tags else 'TBD'}

## Status

Scaffolded from idea-hunter on {datetime.now().strftime('%Y-%m-%d')}.
Idea score: {idea.get('total_score', 'unscored')}

## Getting Started

```bash
# TODO: Add setup instructions
```
"""
    (project_dir / "README.md").write_text(readme)

    # .gitignore
    gitignore = """node_modules/
__pycache__/
*.pyc
.env
.env.local
dist/
build/
.DS_Store
"""
    (project_dir / ".gitignore").write_text(gitignore)

    # docs/PRD.md
    score_table = "\n".join(
        f"| {dim.replace('_', ' ').title()} | {val}/10 |"
        for dim, val in scores.items()
    )

    prd = f"""# PRD: {idea['title']}

## Problem Statement

{idea.get('problem', 'TBD')}

## Proposed Solution

{idea.get('solution', 'TBD')}

## Scoring

| Dimension | Score |
|-----------|-------|
{score_table if score_table else '| TBD | TBD |'}
| **Total** | **{idea.get('total_score', 'N/A')}** |

## Target Users

TBD

## Key Features (MVP)

1. TBD
2. TBD
3. TBD

## Tech Stack

TBD

## Success Metrics

- TBD

## Timeline

- Week 1: TBD
- Week 2: TBD

## Notes

{idea.get('notes', '')}
"""
    (project_dir / "docs" / "PRD.md").write_text(prd)

    # docs/VALIDATION.md
    validation = f"""# Validation Plan: {idea['title']}

## Evidence Collected

{chr(10).join(evidence_lines) if evidence_lines else '- TBD'}

## Validation Steps

### 1. Problem Validation
- [ ] Talk to 5 potential users
- [ ] Confirm pain point severity
- [ ] Understand current workarounds

### 2. Solution Validation
- [ ] Build landing page
- [ ] Collect email signups
- [ ] Target: 50+ signups in first week

### 3. Willingness to Pay
- [ ] Price sensitivity survey
- [ ] Target price point: $__/mo
- [ ] Compare to alternatives

### 4. MVP Test
- [ ] Ship MVP to first 10 users
- [ ] Track activation rate
- [ ] Collect feedback

## Competition

| Competitor | Pricing | Gap |
|-----------|---------|-----|
| TBD | TBD | TBD |

## Go/No-Go Criteria

- [ ] 5+ people confirm the pain point
- [ ] 50+ landing page signups
- [ ] At least 3 people willing to pay $X/mo
"""
    (project_dir / "docs" / "VALIDATION.md").write_text(validation)

    print(f"[scaffold] created project at {project_dir}", file=sys.stderr)
    print(json.dumps({
        "project_dir": str(project_dir),
        "idea_id": idea["id"],
        "files": [
            str(p.relative_to(project_dir))
            for p in sorted(project_dir.rglob("*"))
            if p.is_file()
        ]
    }, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Scaffold a project from a selected idea")
    parser.add_argument("--id", required=True, help="Idea ID from Supabase")
    parser.add_argument("--output", "-o", default=None, help="Output directory (default: workspace root)")
    args = parser.parse_args()

    workspace = Path(__file__).resolve().parent.parent.parent.parent

    if args.output:
        output_dir = Path(args.output)
    else:
        output_dir = workspace

    idea = load_idea(args.id)
    scaffold(idea, output_dir)


if __name__ == "__main__":
    main()
