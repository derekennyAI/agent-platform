#!/usr/bin/env python3
"""CRUD operations on the idea-hunter ideas database (Supabase backend).

Usage:
    python3 manage_ideas.py check --title "..." --problem "..."  (check overlap BEFORE adding)
    python3 manage_ideas.py add --title "..." --problem "..." --solution "..." --evidence '...' --tags "saas,devtools"
    python3 manage_ideas.py score --id idea-001 --demand 8 --timing 7 --moat 4 --fit 6 --simplicity 9
    python3 manage_ideas.py list [--status raw|scored|selected|archived] [--top 10] [--since 7d]
    python3 manage_ideas.py update --id idea-001 --status selected --notes "Worth exploring"
    python3 manage_ideas.py archive --id idea-001 --reason "Already exists"
    python3 manage_ideas.py dedup
    python3 manage_ideas.py digest [--days 1]
    python3 manage_ideas.py export --format md
"""

import argparse
import json
import sys
import os
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path

# Scoring weights
WEIGHTS = {
    "demand": 0.25,
    "simplicity": 0.25,
    "timing": 0.20,
    "moat": 0.15,
    "founder_fit": 0.15,
}

# --- Supabase config ---

def get_supabase_config():
    """Load Supabase URL and key from env vars. Prefers PERSONAL_ prefix for personal-tools project."""
    url = os.environ.get("PERSONAL_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    key = os.environ.get("PERSONAL_SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase credentials not found (set PERSONAL_SUPABASE_URL/PERSONAL_SUPABASE_KEY or SUPABASE_URL/SUPABASE_SERVICE_KEY)")
    return url.rstrip("/"), key


def supa_request(method, path, data=None, params=None):
    """Make a Supabase REST API call. Returns parsed JSON or None."""
    url, key = get_supabase_config()
    endpoint = f"{url}/rest/v1/{path}"
    if params:
        endpoint += "?" + urllib.parse.urlencode(params)

    body = json.dumps(data).encode() if data is not None else None
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if method in ("POST", "PUT", "PATCH"):
        headers["Prefer"] = "return=representation"

    req = urllib.request.Request(endpoint, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            resp_body = resp.read().decode()
            return json.loads(resp_body) if resp_body.strip() else []
    except urllib.error.HTTPError as e:
        err = e.read().decode()
        raise RuntimeError(f"Supabase {method} {path} failed [{e.code}]: {err}")


# --- DB helpers ---

def load_ideas(filters=None):
    """Load ideas from Supabase. filters: dict of {field: value}."""
    params = {"order": "total_score.desc"}
    if filters:
        for k, v in filters.items():
            params[k] = f"eq.{v}"
    rows = supa_request("GET", "ideas", params=params)
    return rows or []


def upsert_idea(idea):
    """Insert or update an idea by id."""
    supa_request("POST", "ideas", data=idea,
                 params={"on_conflict": "id"})


def patch_idea(idea_id, fields):
    """Patch specific fields on an idea."""
    supa_request("PATCH", "ideas", data=fields, params={"id": f"eq.{idea_id}"})


def get_idea(idea_id):
    """Fetch a single idea by id."""
    rows = supa_request("GET", "ideas", params={"id": f"eq.{idea_id}"})
    return rows[0] if rows else None


def generate_id():
    """Generate a new idea ID based on today's date, avoiding collisions."""
    today = datetime.now().strftime("%Y%m%d")
    prefix = f"idea-{today}-"
    rows = supa_request("GET", "ideas", params={"id": f"like.{prefix}%", "select": "id"})
    num = len(rows) + 1
    return f"{prefix}{num:03d}"


# --- Scoring ---

def calc_total(scores):
    total = 0
    for dim, weight in WEIGHTS.items():
        val = scores.get(dim, 0)
        if isinstance(val, (int, float)):
            total += val * weight
    return round(total, 2)


def similarity(a, b):
    return SequenceMatcher(None, a.lower().strip(), b.lower().strip()).ratio()


# --- Commands ---

def cmd_check(args):
    """Check if a candidate idea overlaps with existing ideas before adding."""
    ideas = load_ideas()
    active = [i for i in ideas if i.get("status") != "archived"]

    matches = []
    for idea in active:
        title_sim = similarity(args.title, idea["title"])
        prob_sim = 0
        if args.problem and idea.get("problem"):
            prob_sim = similarity(args.problem, idea["problem"])

        best = max(title_sim, prob_sim)
        if best > 0.5:
            matches.append({
                "id": idea["id"],
                "title": idea["title"],
                "problem": idea.get("problem", "")[:120],
                "title_similarity": round(title_sim, 2),
                "problem_similarity": round(prob_sim, 2),
                "status": idea.get("status", "raw"),
            })

    matches.sort(key=lambda m: max(m["title_similarity"], m["problem_similarity"]), reverse=True)
    overlap = len(matches) > 0 and max(
        max(m["title_similarity"], m["problem_similarity"]) for m in matches
    ) > 0.6

    print(json.dumps({
        "candidate": args.title,
        "overlap": overlap,
        "matches": matches[:5],
    }, indent=2))


def cmd_add(args):
    idea_id = generate_id()
    today = datetime.now().strftime("%Y-%m-%d")

    evidence = []
    if args.evidence:
        try:
            evidence = json.loads(args.evidence)
            if isinstance(evidence, dict):
                evidence = [evidence]
        except json.JSONDecodeError:
            evidence = [{"source": "manual", "quote": args.evidence}]

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []

    idea = {
        "id": idea_id,
        "title": args.title,
        "problem": args.problem or "",
        "solution": args.solution or "",
        "evidence": evidence,
        "scores": {},
        "total_score": 0,
        "status": "raw",
        "tags": tags,
        "created_at": today,
        "notes": args.notes or "",
    }

    upsert_idea(idea)
    print(json.dumps({"action": "added", "id": idea_id, "title": args.title}))


def cmd_score(args):
    idea = get_idea(args.id)
    if not idea:
        print(json.dumps({"error": f"idea not found: {args.id}"}))
        sys.exit(1)

    scores = idea.get("scores") or {}
    if isinstance(scores, str):
        scores = json.loads(scores)

    if args.demand is not None:   scores["demand"] = args.demand
    if args.timing is not None:   scores["timing"] = args.timing
    if args.moat is not None:     scores["moat"] = args.moat
    if args.fit is not None:      scores["founder_fit"] = args.fit
    if args.simplicity is not None: scores["simplicity"] = args.simplicity

    total = calc_total(scores)
    status = "scored" if idea.get("status") == "raw" else idea.get("status")

    patch_idea(args.id, {
        "scores": scores,
        "total_score": total,
        "status": status,
    })
    print(json.dumps({"action": "scored", "id": args.id, "scores": scores, "total_score": total}))


def cmd_list(args):
    params = {"order": "total_score.desc"}
    if args.status:
        params["status"] = f"eq.{args.status}"
    else:
        params["status"] = "neq.archived"
    if args.since:
        days = int(args.since.rstrip("d"))
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        params["created_at"] = f"gte.{cutoff}"
    if args.top:
        params["limit"] = args.top

    rows = supa_request("GET", "ideas", params=params) or []

    output = [{
        "id": r["id"],
        "title": r["title"],
        "total_score": r.get("total_score", 0),
        "status": r.get("status", "raw"),
        "tags": r.get("tags", []),
        "created": r.get("created_at", ""),
    } for r in rows]

    print(json.dumps({"count": len(output), "ideas": output}, indent=2))


def cmd_update(args):
    idea = get_idea(args.id)
    if not idea:
        print(json.dumps({"error": f"idea not found: {args.id}"}))
        sys.exit(1)

    fields = {}
    if args.status: fields["status"] = args.status
    if args.notes:  fields["notes"] = args.notes
    if args.title:  fields["title"] = args.title
    if args.tags:   fields["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]

    patch_idea(args.id, fields)
    print(json.dumps({"action": "updated", "id": args.id}))


def cmd_archive(args):
    idea = get_idea(args.id)
    if not idea:
        print(json.dumps({"error": f"idea not found: {args.id}"}))
        sys.exit(1)

    existing_notes = idea.get("notes", "") or ""
    patch_idea(args.id, {
        "status": "archived",
        "notes": (existing_notes + f"\nArchived: {args.reason or 'no reason given'}").strip(),
    })
    print(json.dumps({"action": "archived", "id": args.id, "reason": args.reason or ""}))


def cmd_dedup(args):
    ideas = load_ideas()
    active = [i for i in ideas if i.get("status") != "archived"]
    dupes = []

    for i, a in enumerate(active):
        for b in active[i + 1:]:
            ratio = similarity(a["title"], b["title"])
            if ratio > 0.6:
                dupes.append({
                    "pair": [a["id"], b["id"]],
                    "titles": [a["title"], b["title"]],
                    "similarity": round(ratio, 2),
                })

    for i, a in enumerate(active):
        for b in active[i + 1:]:
            if not a.get("problem") or not b.get("problem"):
                continue
            if similarity(a["title"], b["title"]) <= 0.6:
                prob_ratio = similarity(a["problem"], b["problem"])
                if prob_ratio > 0.7:
                    dupes.append({
                        "pair": [a["id"], b["id"]],
                        "titles": [a["title"], b["title"]],
                        "similarity": round(prob_ratio, 2),
                        "match_type": "problem",
                    })

    print(json.dumps({"duplicates": dupes, "count": len(dupes)}, indent=2))


def cmd_digest(args):
    days = args.days if args.days else 1
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    rows = supa_request("GET", "ideas", params={
        "created_at": f"gte.{cutoff}",
        "status": "neq.archived",
        "order": "total_score.desc",
    }) or []

    if not rows:
        print(f"No new ideas in the last {days} day(s).")
        return

    lines = [
        f"**Idea Hunt Digest** — {datetime.now().strftime('%Y-%m-%d')}",
        f"_{len(rows)} new idea(s) from the last {days} day(s)_\n",
    ]

    for rank, idea in enumerate(rows, 1):
        score = idea.get("total_score", 0)
        tags = ", ".join(idea.get("tags") or [])
        scores = idea.get("scores") or {}
        if isinstance(scores, str):
            scores = json.loads(scores)
        evidence = idea.get("evidence") or []
        if isinstance(evidence, str):
            evidence = json.loads(evidence)

        lines.append(f"**{rank}. {idea['title']}** — {score}/10")
        lines.append(f"   Problem: {idea.get('problem', 'N/A')}")
        lines.append(f"   Solution: {idea.get('solution', 'N/A')}")
        if scores:
            dims = " | ".join(f"{k}: {v}" for k, v in scores.items())
            lines.append(f"   Scores: {dims}")
        if tags:
            lines.append(f"   Tags: {tags}")
        for e in evidence[:2]:
            parts = [e.get("source", "")]
            if e.get("upvotes"): parts.append(f"{e['upvotes']}↑")
            if e.get("quote"):   parts.append(f'"{e["quote"][:100]}"')
            if e.get("url"):     parts.append(e["url"])
            lines.append(f"   Evidence: {' — '.join(p for p in parts if p)}")
        lines.append("")

    print("\n".join(lines))


def cmd_export(args):
    ideas = load_ideas()
    db_path = Path(__file__).resolve().parent.parent.parent.parent / "memory" / "idea-hunter"
    db_path.mkdir(parents=True, exist_ok=True)
    md_path = db_path / "ideas.md"

    lines = [
        "# Idea Hunter — Ranked Ideas",
        f"_Generated {datetime.now().strftime('%Y-%m-%d %H:%M')}_\n",
    ]

    for status in ["selected", "scored", "raw", "scaffolded", "archived"]:
        group = [i for i in ideas if i.get("status") == status]
        if not group:
            continue
        lines.append(f"## {status.title()} ({len(group)})\n")
        for idea in group:
            score = idea.get("total_score", 0)
            tags = ", ".join(idea.get("tags") or [])
            scores = idea.get("scores") or {}
            if isinstance(scores, str):
                scores = json.loads(scores)
            lines += [
                f"### {idea['title']} — {score}/10",
                f"- **ID**: {idea['id']}",
                f"- **Problem**: {idea.get('problem', 'N/A')}",
                f"- **Solution**: {idea.get('solution', 'N/A')}",
            ]
            if scores:
                lines.append(f"- **Scores**: {', '.join(f'{k}: {v}' for k,v in scores.items())}")
            if tags:
                lines.append(f"- **Tags**: {tags}")
            if idea.get("notes"):
                lines.append(f"- **Notes**: {idea['notes']}")
            lines.append("")

    md_path.write_text("\n".join(lines))
    print(json.dumps({"action": "exported", "path": str(md_path), "count": len(ideas)}))


# --- Main ---

def main():
    parser = argparse.ArgumentParser(description="Manage idea-hunter ideas database (Supabase)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("--title", required=True)
    p_add.add_argument("--problem", default="")
    p_add.add_argument("--solution", default="")
    p_add.add_argument("--evidence", default="")
    p_add.add_argument("--tags", default="")
    p_add.add_argument("--notes", default="")

    p_score = sub.add_parser("score")
    p_score.add_argument("--id", required=True)
    p_score.add_argument("--demand", type=int, default=None)
    p_score.add_argument("--timing", type=int, default=None)
    p_score.add_argument("--moat", type=int, default=None)
    p_score.add_argument("--fit", type=int, default=None)
    p_score.add_argument("--simplicity", type=int, default=None)

    p_list = sub.add_parser("list")
    p_list.add_argument("--status", default=None, choices=["raw", "scored", "selected", "scaffolded", "archived"])
    p_list.add_argument("--top", type=int, default=None)
    p_list.add_argument("--since", default=None)

    p_update = sub.add_parser("update")
    p_update.add_argument("--id", required=True)
    p_update.add_argument("--status", default=None)
    p_update.add_argument("--notes", default=None)
    p_update.add_argument("--title", default=None)
    p_update.add_argument("--tags", default=None)

    p_archive = sub.add_parser("archive")
    p_archive.add_argument("--id", required=True)
    p_archive.add_argument("--reason", default="")

    p_check = sub.add_parser("check")
    p_check.add_argument("--title", required=True)
    p_check.add_argument("--problem", default="")

    sub.add_parser("dedup")

    p_digest = sub.add_parser("digest")
    p_digest.add_argument("--days", type=int, default=1)

    p_export = sub.add_parser("export")
    p_export.add_argument("--format", default="md", choices=["md"])

    args = parser.parse_args()
    {
        "check": cmd_check,
        "add": cmd_add,
        "score": cmd_score,
        "list": cmd_list,
        "update": cmd_update,
        "archive": cmd_archive,
        "dedup": cmd_dedup,
        "digest": cmd_digest,
        "export": cmd_export,
    }[args.command](args)


if __name__ == "__main__":
    main()
