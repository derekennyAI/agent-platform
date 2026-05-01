#!/usr/bin/env python3
"""Phase 2.5 — Migrate an agent's file-based memory into agent_memories + agent_memory_sessions.

Walks ~/<workspace>/memory/*.md (and memory/sessions/*.md), parses frontmatter
or infers type from filename prefix, inserts rows with ON CONFLICT DO NOTHING
so re-runs are safe. Leaves local files untouched.

Report written to /tmp/migration_report_<agent>.json with per-file disposition:
  imported     — conforming frontmatter + valid type
  inferred     — no frontmatter; type guessed from filename prefix
  session      — went to agent_memory_sessions
  quarantined  — skipped; reason logged
  error        — import attempted but DB rejected

Usage:
  migrate_memories_to_db.py --agent derek
  migrate_memories_to_db.py --agent derek --dry-run    # parse + report, no inserts
"""
import argparse
import json
import os
import pathlib
import plistlib
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

AGENT_WORKSPACE = {
    "admin": "/Users/YOUR_MAC_USERNAME/admin",
    "derek": "/Users/YOUR_MAC_USERNAME/derekPersonal",
    "dereklm": "/Users/YOUR_MAC_USERNAME/dereklm",
    "vera": "/Users/YOUR_MAC_USERNAME/vera",
    "nate": "/Users/YOUR_MAC_USERNAME/nate",
    "macgyver": "/Users/YOUR_MAC_USERNAME/macgyver",
}

VALID_TYPES = {"soul", "user", "feedback", "project", "reference", "failed", "session"}
# Filename prefix → inferred type
PREFIX_TYPE = {
    "soul": "soul",
    "user_": "user",
    "feedback_": "feedback",
    "project_": "project",
    "reference_": "reference",
    "failed_": "failed",
}


def load_env():
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        return os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    plist = "/Users/YOUR_MAC_USERNAME/Library/LaunchAgents/com.vera-agent.daemon.plist"
    with open(plist, "rb") as f:
        ev = plistlib.load(f).get("EnvironmentVariables", {})
    return ev["SUPABASE_URL"], ev["SUPABASE_SERVICE_KEY"]


def slugify(s):
    """Underscore convention — matches existing ~/<agent>/memory/ filenames.
    Keep in sync with project_memories_to_files.py:slugify and server.js:slugify."""
    s = re.sub(r"[^a-z0-9]+", "_", str(s or "").lower()).strip("_")
    return s[:80]


def parse_frontmatter(text):
    """Return (meta_dict, body) or (None, full_text) if no frontmatter."""
    if not text.startswith("---\n"):
        return None, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return None, text
    raw = text[4:end]
    meta = {}
    for line in raw.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip()
    return meta, text[end + 5:]


def infer_from_filename(fname):
    """Return (type, synthesized_name) or (None, None)."""
    stem = fname[:-3] if fname.endswith(".md") else fname
    # soul is the full filename (e.g. "derek_soul.md" → soul)
    if stem.endswith("_soul") or stem == "soul":
        return "soul", stem.replace("_soul", "") or "soul"
    for prefix, t in PREFIX_TYPE.items():
        if prefix == "soul":
            continue
        if stem.startswith(prefix):
            return t, stem[len(prefix):] or stem
    # failed_approaches.md (no underscore after failed)
    if stem == "failed_approaches":
        return "failed", "approaches"
    return None, None


def walk_top_level(memory_dir):
    """Yield (path, relpath) for .md files at memory/ top level."""
    for p in sorted(memory_dir.glob("*.md")):
        if p.name == "MEMORY.md":
            continue
        yield p


def walk_sessions(memory_dir):
    sess = memory_dir / "sessions"
    if not sess.is_dir():
        return
    for p in sorted(sess.glob("*.md")):
        yield p


def classify_session(fname):
    """Parse filename into (session_date, hour, kind, id_suffix). Returns None if unparseable.

    id_suffix is the trailing token used to make the row id unique when multiple
    session notes share the same date (e.g. 2026-04-22_admin-poll.md,
    2026-04-22_admin-poll-2.md)."""
    stem = fname[:-3]
    # YYYY-MM-DD_HH.md → hourly
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(\d{2})$", stem)
    if m:
        return m.group(1), int(m.group(2)), "hourly", f"{int(m.group(2)):02d}"
    # YYYY-MM-DD_weekly.md
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_weekly$", stem)
    if m:
        return m.group(1), None, "weekly", "weekly"
    # YYYY-MM-DD_daily.md or YYYY-MM-DD.md → daily
    m = re.match(r"^(\d{4}-\d{2}-\d{2})(_daily)?$", stem)
    if m:
        return m.group(1), None, "daily", "daily"
    # YYYY-MM-DD_<anything>.md — treat as daily with custom suffix for id uniqueness
    m = re.match(r"^(\d{4}-\d{2}-\d{2})_(.+)$", stem)
    if m:
        return m.group(1), None, "daily", slugify(m.group(2))
    return None


def insert_memory(base, key, row, dry):
    if dry:
        return {"ok": True, "dry": True}
    req = urllib.request.Request(
        f"{base}/rest/v1/agent_memories",
        data=json.dumps(row).encode(), method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "return=minimal,resolution=ignore-duplicates"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return {"ok": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:300]}"}


def insert_session(base, key, row, dry):
    if dry:
        return {"ok": True, "dry": True}
    req = urllib.request.Request(
        f"{base}/rest/v1/agent_memory_sessions",
        data=json.dumps(row).encode(), method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}",
                 "Content-Type": "application/json",
                 "Prefer": "return=minimal,resolution=ignore-duplicates"})
    try:
        urllib.request.urlopen(req, timeout=15)
        return {"ok": True}
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": f"HTTP {e.code}: {e.read().decode()[:300]}"}


def process_top_level(p, agent, base, key, dry):
    text = p.read_text(errors="replace")
    meta, body = parse_frontmatter(text)

    if meta and meta.get("type") in VALID_TYPES and meta.get("name"):
        # Conforming
        row = {
            "id": f"{agent}/{meta['type']}/{slugify(meta['name'])}",
            "agent_name": agent,
            "type": meta["type"],
            "name": meta["name"],
            "description": (meta.get("description") or meta["name"])[:500],
            "content": body.strip(),
            "source": meta.get("source") or None,
            "tags": [],
            "origin_session_id": meta.get("originSessionId") or None,
        }
        r = insert_memory(base, key, row, dry)
        return ("imported" if r["ok"] else "error", {"file": p.name, "id": row["id"], **({"error": r["error"]} if not r["ok"] else {})})

    # Try inferring from filename
    t, name = infer_from_filename(p.name)
    if t:
        row = {
            "id": f"{agent}/{t}/{slugify(name)}",
            "agent_name": agent,
            "type": t,
            "name": name,
            "description": f"[migrated from {p.name}; frontmatter was missing/invalid]"[:500],
            "content": body.strip() if meta is None else text.strip(),
            "source": None,
            "tags": ["migrated-inferred"],
        }
        # reference type requires a source; if we're inferring a reference with no source, quarantine instead
        if t == "reference":
            return ("quarantined", {"file": p.name, "reason": "reference type inferred but no source; needs manual review"})
        r = insert_memory(base, key, row, dry)
        return ("inferred" if r["ok"] else "error", {"file": p.name, "id": row["id"], **({"error": r["error"]} if not r["ok"] else {})})

    return ("quarantined", {"file": p.name, "reason": "no frontmatter and unrecognized filename prefix"})


def process_session(p, agent, base, key, dry):
    cls = classify_session(p.name)
    if cls is None:
        return ("quarantined", {"file": f"sessions/{p.name}", "reason": "unparseable session filename"})
    date, hour, kind, id_suffix = cls
    content = p.read_text(errors="replace").strip()
    sess_id = f"{agent}/sessions/{date}_{id_suffix}"
    row = {
        "id": sess_id,
        "agent_name": agent,
        "session_date": date,
        "hour": hour,
        "kind": kind,
        "content": content,
    }
    r = insert_session(base, key, row, dry)
    return ("session" if r["ok"] else "error", {"file": f"sessions/{p.name}", "id": sess_id, **({"error": r["error"]} if not r["ok"] else {})})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--agent", required=True, help="Registry name of the agent to migrate.")
    ap.add_argument("--dry-run", action="store_true", help="Parse + classify; do not insert.")
    args = ap.parse_args()

    if args.agent not in AGENT_WORKSPACE:
        print(f"unknown agent '{args.agent}'; valid: {list(AGENT_WORKSPACE)}", file=sys.stderr)
        sys.exit(2)
    workspace = pathlib.Path(AGENT_WORKSPACE[args.agent])
    memory_dir = workspace / "memory"
    if not memory_dir.is_dir():
        print(f"no memory dir at {memory_dir}", file=sys.stderr)
        sys.exit(2)

    base, key = load_env()

    report = {
        "agent": args.agent,
        "workspace": str(workspace),
        "dry_run": args.dry_run,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "imported": [],
        "inferred": [],
        "session": [],
        "quarantined": [],
        "error": [],
        "ignored": [],  # subdirs + non-.md files we skipped
    }

    # Top-level .md
    for p in walk_top_level(memory_dir):
        kind, info = process_top_level(p, args.agent, base, key, args.dry_run)
        report[kind].append(info)

    # Sessions
    for p in walk_sessions(memory_dir):
        kind, info = process_session(p, args.agent, base, key, args.dry_run)
        report[kind].append(info)

    # Also log what we *didn't* touch
    for p in sorted(memory_dir.iterdir()):
        if p.name == "MEMORY.md":
            report["ignored"].append({"file": p.name, "reason": "projector-regenerated index"})
        elif p.is_dir() and p.name != "sessions":
            report["ignored"].append({"file": f"{p.name}/", "reason": "non-memory subdir (skill data / docs / binaries)"})
        elif p.is_file() and not p.name.endswith(".md"):
            report["ignored"].append({"file": p.name, "reason": "non-.md operational file (log/json/state)"})

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    report["summary"] = {k: len(report[k]) for k in ("imported", "inferred", "session", "quarantined", "error", "ignored")}

    out = pathlib.Path(f"/tmp/migration_report_{args.agent}.json")
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report["summary"], indent=2))
    print(f"\nfull report: {out}")


if __name__ == "__main__":
    main()
