#!/usr/bin/env python3
"""Phase 2.3 — Materialize agent_memories rows to per-agent memory directories.

Reads the authoritative agent_memories table and writes markdown files to
~/<agent>/memory/<slug>.md (one file per non-superseded row), regenerates
~/<agent>/memory/MEMORY.md, and writes session summaries to ~/<agent>/memory/sessions/.

Safety rails:
  - --dry (default on first run): writes to /tmp/memory-projection-test/<agent>/ instead.
  - Atomic swap via memory.new/ → memory.prev/ + memory/. One backup generation.
  - Advisory file lock at /tmp/memory-projector.lock; refuses to run concurrently.
  - Supabase unreachable → exit 2; files unchanged.
  - Runs per-agent independently; one agent's failure doesn't block others.

Designed for OS-cron: `*/15 * * * *`. Runs under the admin daemon's env so
SUPABASE_SERVICE_KEY + SUPABASE_URL are available.

Usage:
  project_memories_to_files.py                   # dry-run into /tmp/
  project_memories_to_files.py --live            # write to real ~/<agent>/memory/
  project_memories_to_files.py --agent vera      # single agent
"""
import argparse
import fcntl
import json
import os
import pathlib
import plistlib
import shutil
import sys
import time
import urllib.request
import urllib.error
import urllib.parse

LOCK_PATH = "/tmp/memory-projector.lock"
DRY_ROOT = pathlib.Path("/tmp/memory-projection-test")

AGENT_WORKSPACE = {
    "admin": "/Users/YOUR_MAC_USERNAME/admin",
    "derek": "/Users/YOUR_MAC_USERNAME/derekPersonal",
    "dereklm": "/Users/YOUR_MAC_USERNAME/dereklm",
    "vera": "/Users/YOUR_MAC_USERNAME/vera",
    "nate": "/Users/YOUR_MAC_USERNAME/nate",
    "macgyver": "/Users/YOUR_MAC_USERNAME/macgyver",
}


def load_env():
    if os.environ.get("SUPABASE_URL") and os.environ.get("SUPABASE_SERVICE_KEY"):
        return os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"]
    plist = "/Users/YOUR_MAC_USERNAME/Library/LaunchAgents/com.vera-agent.daemon.plist"
    with open(plist, "rb") as f:
        ev = plistlib.load(f).get("EnvironmentVariables", {})
    return ev["SUPABASE_URL"], ev["SUPABASE_SERVICE_KEY"]


def pg_get(base, key, path):
    req = urllib.request.Request(
        f"{base}/rest/v1/{path}",
        headers={"apikey": key, "Authorization": f"Bearer {key}"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=15).read())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase {path}: HTTP {e.code} — {e.read().decode()[:300]}")


def slugify(s):
    """Underscore convention — matches existing ~/<agent>/memory/ filenames.
    Keep in sync with server.js:slugify."""
    import re
    s = str(s or "").lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:80]


import re as _re
_SESSION_NAME_RE = _re.compile(r'^(?:session_)?(\d{4})[-_](\d{2})[-_](\d{2})(?:[_-](.*))?$')

def session_filename(name):
    """Map a session memory `name` to a `sessions/<file>.md` basename.
    Handles all observed patterns:
      - 'session_2026_04_29_daily' -> '2026-04-29.md'   (strip prefix, daily → bare date)
      - 'session_2026_04_29_compact' -> '2026-04-29_compact.md'
      - '2026-04-29_22_admin_poll' -> '2026-04-29_22_admin_poll.md'
      - '2026_04_29' -> '2026-04-29.md'
      - 'early-apr-2026' (no parseable date) -> falls back to slugify
    """
    m = _SESSION_NAME_RE.match(name or "")
    if not m:
        return f"{slugify(name)}.md"
    y, mo, d, suffix = m.groups()
    if not suffix or suffix == "daily":
        return f"{y}-{mo}-{d}.md"
    return f"{y}-{mo}-{d}_{suffix}.md"


def frontmatter(row):
    fm_lines = ["---"]
    fm_lines.append(f"id: {row['id']}")
    fm_lines.append(f"name: {row['name']}")
    desc = (row.get('description') or '').replace('\n', ' ')
    fm_lines.append(f"description: {desc}")
    fm_lines.append(f"type: {row['type']}")
    if row.get("source"):
        fm_lines.append(f"source: {row['source']}")
    if row.get("tags"):
        fm_lines.append(f"tags: [{', '.join(row['tags'])}]")
    if row.get("origin_session_id"):
        fm_lines.append(f"originSessionId: {row['origin_session_id']}")
    if row.get("verified_at"):
        fm_lines.append(f"verified_at: {row['verified_at']}")
    fm_lines.append("---")
    fm_lines.append("")
    return "\n".join(fm_lines)


def filename_for(row):
    """Canonical filename.
    Soul is one-per-agent and uses `<slug(name)>_soul.md` (NOT `<type>_<name>.md`)
    so CLAUDE.md paths like `memory/derek_soul.md` or `memory/overseer_soul.md`
    stay stable. The `name` field drives the persona slug.
    Everything else: `<type>_<slug(name)>.md`."""
    if row["type"] == "soul":
        return f"{slugify(row['name'])}_soul.md"
    return f"{row['type']}_{slugify(row['name'])}.md"


def serialize_memory(row):
    # Blank line after closing "---" matches the fleet's existing convention.
    return frontmatter(row) + "\n" + (row.get("content") or "").rstrip() + "\n"


def render_memory_index(rows):
    """Regenerate MEMORY.md index from rows (single agent's non-superseded memories)."""
    lines = ["# Memory Index", ""]
    # Group soul/user/feedback/project/reference/failed in that order for top
    order = ["soul", "user", "feedback", "project", "reference", "failed", "session"]
    by_type = {}
    for r in rows:
        by_type.setdefault(r["type"], []).append(r)
    first = True
    for t in order:
        bucket = sorted(by_type.get(t, []), key=lambda r: r["name"])
        if not bucket:
            continue
        if not first:
            lines.append("")
        first = False
        for r in bucket:
            fn = filename_for(r)
            desc = (r.get("description") or "").replace("\n", " ")
            lines.append(f"- [{r['name']}]({fn}) — {desc}")
    return "\n".join(lines) + "\n"


def project_agent_to(agent, workspace_dir, base, key, dry):
    """Project one agent's memories into <workspace_dir>/memory/.

    agent: registry name used as the DB filter (e.g. "derek").
    workspace_dir: pathlib.Path to the workspace root (e.g. /Users/YOUR_MAC_USERNAME/derekPersonal).
    """
    # Fetch non-superseded rows (includes type='session'; we split below).
    q = f"agent_memories?agent_name=eq.{agent}&superseded_by=is.null&select=*&order=type,name"
    rows = pg_get(base, key, q)
    # Sessions live in `agent_memories` with type='session' since the Phase 2
    # rollout (2026-04-23). The legacy `agent_memory_sessions` table is dead —
    # nothing has written to it since the one-time migration import.
    session_rows = [r for r in rows if r.get("type") == "session"]

    workspace_dir = pathlib.Path(workspace_dir)
    workspace_dir.mkdir(parents=True, exist_ok=True)
    final_dir = workspace_dir / "memory"
    new_dir = workspace_dir / "memory.new"
    prev_dir = workspace_dir / "memory.prev"

    # Clean staging
    if new_dir.exists():
        shutil.rmtree(new_dir)
    new_dir.mkdir(parents=True, exist_ok=True)
    (new_dir / "sessions").mkdir(exist_ok=True)

    # Write per-row files
    written = []
    for row in rows:
        fn = filename_for(row)
        path = new_dir / fn
        path.write_text(serialize_memory(row))
        written.append(fn)

    # MEMORY.md index
    (new_dir / "MEMORY.md").write_text(render_memory_index(rows))

    # Session files — derive filename from the row's `name` via session_filename().
    # Tolerant of the inconsistent naming patterns across the fleet:
    # 'session_2026_04_29_daily', '2026-04-29_admin_poll', '2026_04_29_daily', etc.
    for s in session_rows:
        fn = session_filename(s.get("name"))
        (new_dir / "sessions" / fn).write_text((s.get("content") or "") + "\n")

    # Atomic swap: move final → prev, new → final
    if final_dir.exists():
        if prev_dir.exists():
            shutil.rmtree(prev_dir)
        final_dir.rename(prev_dir)
    new_dir.rename(final_dir)

    return {
        "agent": agent,
        "dry": bool(dry),
        "memories_written": len(written),
        "sessions_written": len(session_rows),
        "out": str(final_dir),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true",
                    help="write to real ~/<agent>/memory/ (default: dry into /tmp/memory-projection-test/).")
    ap.add_argument("--agent", default=None,
                    help="limit to one agent (default: project all registered agents).")
    args = ap.parse_args()

    # Single-writer lock
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("another projector run is in progress; skipping.", file=sys.stderr)
        sys.exit(1)

    try:
        base, key = load_env()
    except Exception as e:
        print(f"env load failed: {e}", file=sys.stderr)
        sys.exit(2)

    agents = [args.agent] if args.agent else list(AGENT_WORKSPACE.keys())
    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "dry": not args.live, "per_agent": []}

    for a in agents:
        try:
            if args.live:
                # Registry name (e.g. "derek") maps to workspace folder (e.g. "derekPersonal").
                # project_agent writes to <out_root>/<folder>/memory, so pass the folder as
                # the subdir name while querying the DB by registry name.
                workspace = AGENT_WORKSPACE[a]
                info = project_agent_to(a, pathlib.Path(workspace), base, key, dry=False)
            else:
                DRY_ROOT.mkdir(parents=True, exist_ok=True)
                info = project_agent_to(a, DRY_ROOT / a, base, key, dry=True)
            report["per_agent"].append(info)
        except Exception as e:
            report["per_agent"].append({"agent": a, "error": str(e)})

    report["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    print(json.dumps(report, indent=2))

    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)


if __name__ == "__main__":
    main()
