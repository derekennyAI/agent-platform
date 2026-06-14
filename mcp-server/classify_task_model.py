#!/usr/bin/env python3
"""Classify a scheduled task to the cheapest Claude model that won't degrade it.

Tiering policy (cost vs intelligence), per 1M tokens:
  - claude-haiku-4-5  ($1/$5)   mechanical/deterministic: run a script, post
                                 pre-approved content, send a templated reminder,
                                 rotate logs, poll a queue, pull metrics.
  - claude-sonnet-4-6 ($3/$15)  synthesis/judgment-lite: summarize/compact
                                 memory, compile a report, triage email, health
                                 checks, briefs.
  - claude-opus-4-8   ($5/$25)  genuine creation/reasoning: content pipeline,
                                 infra research, idea hunting, blog drafting.

Default when nothing matches is OPUS — never silently downgrade an unknown task.

Usage:
  classify_task_model.py "<task description>"        -> prints model id
  classify_task_model.py --map                        -> reads all active tasks
        from Supabase and writes <script_dir>/task_models.json (tid -> model),
        also prints a review table to stderr.
  classify_task_model.py --map --dry-run              -> table only, no write.

Keep this self-maintaining: when a new scheduled task is created, run with the
description to get its tier, or re-run --map to refresh the whole fleet.
"""
import json
import os
import re
import sys
import urllib.request

HAIKU = "claude-haiku-4-5"
SONNET = "claude-sonnet-4-6"
OPUS = "claude-opus-4-8"

# Rules are evaluated in order; first match wins. Each rule is (regex, model).
# Regexes match against the lowercased task id + description.
RULES = [
    # --- OPUS: genuine creation / open-ended reasoning -------------------
    (r"content pipeline|ai content pipeline", OPUS),
    (r"idea hunt", OPUS),
    (r"\bfleet blog\b|draft the next post|blog post", OPUS),
    # "infra research" = the daily WebSearch+synthesize pass. The Notion
    # "research digest/email digest" (compile existing findings) is Sonnet,
    # handled below — exclude it here.
    (r"infrastructure research|infra_research|research pass.*websearch|find the latest developments", OPUS),

    # --- HAIKU: mechanical / deterministic ------------------------------
    (r"post (ig|instagram) story|one-off weekend post|daily founder quote reel post", HAIKU),
    (r"reminder|nag|\bsmog\b|watering|\bpill\b|mother's day|world cup", HAIKU),
    (r"log rotation|qmd index refresh|qmd_refresh", HAIKU),
    (r"social metrics collection|usage monitor|usage_monitor", HAIKU),
    (r"skincare|needs action auto-complete|needs_action|email triage script|email_triage", HAIKU),
    (r"playwright|e2e test", HAIKU),
    (r"quote reel queue|auto-replenish", HAIKU),
    # generic "run this script/command" with no synthesis described
    (r"^run the |run python3|run /users", HAIKU),

    # --- SONNET: synthesis / judgment-lite ------------------------------
    (r"memory compaction|daily memory compact", SONNET),
    (r"session memory review|session_memory", SONNET),
    (r"weekly memory review", SONNET),
    (r"admin poll|admin_poll|admin_tasks|pending admin tasks", SONNET),
    (r"scope inbox|scope_inbox|scope request", SONNET),
    (r"ops report|pipeline health|grafana", SONNET),
    (r"engagement report|evening brief|weekly brief|morning digest", SONNET),
    (r"finance report|invoice|toggl report", SONNET),
    (r"inbox declutter|inbox triage", SONNET),
    (r"research digest|email digest|infra research digest|research email", SONNET),
]


def classify(text: str) -> str:
    # Classify on the leading portion only — a task's purpose is stated up front,
    # while incidental keywords (noise-filter lists, examples, embedded snippets)
    # live deeper in the body and would otherwise cause false matches. E.g. the
    # fleet engagement report lists "content pipeline" as a string to filter out,
    # which must not tier it as Opus.
    t = " ".join((text or "").split()).lower()[:240]
    for pattern, model in RULES:
        if re.search(pattern, t):
            return model
    return OPUS  # safe default — never silently downgrade an unknown task


def _supabase_tasks():
    base = os.environ["SUPABASE_URL"].rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ["SUPABASE_KEY"]
    url = (base + "/rest/v1/scheduled_tasks"
           "?select=id,agent_name,task_description&active=eq.true&order=agent_name,id")
    req = urllib.request.Request(url, headers={"apikey": key, "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.load(r)


def main():
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)

    if args[0] == "--map":
        dry = "--dry-run" in args
        rows = _supabase_tasks()
        mapping = {}
        counts = {HAIKU: 0, SONNET: 0, OPUS: 0}
        for row in rows:
            tid = row["id"]
            # classify on id + description so id-only hints (e.g. *_admin_poll) match
            model = classify(f"{tid} {row.get('task_description','')}")
            mapping[tid] = model
            counts[model] += 1
            short = " ".join((row.get("task_description") or "").split())[:60]
            tier = {HAIKU: "HAIKU ", SONNET: "SONNET", OPUS: "OPUS  "}[model]
            print(f"{row['agent_name']:9} {tier} {tid[:34]:36} {short}", file=sys.stderr)
        print(f"\nTOTAL {len(rows)}  ->  HAIKU {counts[HAIKU]}  SONNET {counts[SONNET]}  OPUS {counts[OPUS]}",
              file=sys.stderr)
        if dry:
            print("(dry-run — task_models.json NOT written)", file=sys.stderr)
        else:
            out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "task_models.json")
            with open(out, "w") as f:
                json.dump(mapping, f, indent=2, sort_keys=True)
            print(f"wrote {out} ({len(mapping)} tasks)", file=sys.stderr)
        return

    # single-description mode
    print(classify(args[0]))


if __name__ == "__main__":
    main()
