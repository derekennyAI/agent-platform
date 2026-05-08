#!/usr/bin/env python3
"""Update build status in Notion and local state.json after task/phase events.

Usage:
    python3 update_build_status.py --state-file memory/builds/{slug}/state.json --complete-task [--commit abc123]
    python3 update_build_status.py --state-file memory/builds/{slug}/state.json --complete-phase
    python3 update_build_status.py --state-file memory/builds/{slug}/state.json --blocked "exact error"
    python3 update_build_status.py --state-file memory/builds/{slug}/state.json --unblocked
    python3 update_build_status.py --state-file memory/builds/{slug}/state.json --status "Phase 1 — Task 3: Writing middleware"
    python3 update_build_status.py --state-file memory/builds/{slug}/state.json --waiting
    python3 update_build_status.py --state-file memory/builds/{slug}/state.json --continue
"""

import argparse
import json
import sys
from pathlib import Path

import notion_client as notion


def _workspace():
    return Path(__file__).resolve().parent.parent.parent.parent


def load_state(state_file_arg):
    p = Path(state_file_arg)
    if not p.exists():
        p = _workspace() / state_file_arg
    if not p.exists():
        print(f"Error: state file not found: {state_file_arg}", file=sys.stderr)
        sys.exit(1)
    with open(p) as f:
        state = json.load(f)
    state["_path"] = str(p)
    return state


def save_state(state):
    path = state.pop("_path")
    with open(path, "w") as f:
        json.dump(state, f, indent=2)
    state["_path"] = path


def current_banner(state):
    n = state["current_phase"]
    m = state["current_task"]
    phases = state["phases"]
    if n >= len(phases):
        return "Build complete"
    phase_name = phases[n]["name"]
    task_count = phases[n]["task_count"]
    return f"{phase_name} — Task {m + 1} of {task_count}"


def do_complete_task(state, commit):
    n = state["current_phase"]
    m = state["current_task"]
    block_map = state["block_map"]
    phases = state["phases"]
    task_count = phases[n]["task_count"]

    # Tick the checkbox in Notion
    task_key = f"phase_{n}_task_{m}"
    if task_key in block_map:
        notion.check_todo(block_map[task_key])
    else:
        print(f"Warning: no block_map entry for {task_key}", file=sys.stderr)

    if commit:
        state["last_commit"] = commit

    m += 1
    state["current_task"] = m

    if m >= task_count:
        # Auto-complete the phase
        do_complete_phase(state)
    else:
        banner = current_banner(state)
        if "status_banner" in block_map:
            notion.update_callout(block_map["status_banner"], banner, "🔄")
        save_state(state)
        print(json.dumps({"action": "task_complete", "next_task": m + 1, "banner": banner}))


def do_complete_phase(state):
    n = state["current_phase"]
    block_map = state["block_map"]
    phases = state["phases"]
    phase_name = phases[n]["name"]

    if f"phase_{n}_status" in block_map:
        notion.update_callout(block_map[f"phase_{n}_status"], "COMPLETE", "🟢")

    banner = f"{phase_name} COMPLETE — waiting for Farlen"
    if "status_banner" in block_map:
        notion.update_callout(block_map["status_banner"], banner, "✅")

    state["phase_statuses"][str(n)] = "complete"
    state["waiting_for_farlen"] = True
    save_state(state)
    print(json.dumps({"action": "phase_complete", "phase": n, "phase_name": phase_name, "banner": banner}))


def do_blocked(state, reason):
    n = state["current_phase"]
    block_map = state["block_map"]
    phases = state["phases"]
    phase_name = phases[n]["name"]
    short = reason[:80]

    if f"phase_{n}_status" in block_map:
        notion.update_callout(block_map[f"phase_{n}_status"], f"BLOCKED: {short}", "🚫")

    banner = f"BLOCKED ({phase_name}): {short}"
    if "status_banner" in block_map:
        notion.update_callout(block_map["status_banner"], banner, "🚫")

    state["blocked_on"] = reason
    save_state(state)
    print(json.dumps({"action": "blocked", "reason": reason, "banner": banner}))


def do_unblocked(state):
    n = state["current_phase"]
    block_map = state["block_map"]

    if f"phase_{n}_status" in block_map:
        notion.update_callout(block_map[f"phase_{n}_status"], "IN PROGRESS", "🟡")

    banner = current_banner(state)
    if "status_banner" in block_map:
        notion.update_callout(block_map["status_banner"], banner, "🔄")

    state["blocked_on"] = None
    save_state(state)
    print(json.dumps({"action": "unblocked", "banner": banner}))


def do_status(state, text):
    block_map = state["block_map"]
    if "status_banner" in block_map:
        notion.update_callout(block_map["status_banner"], text, "🔄")
    save_state(state)
    print(json.dumps({"action": "status_updated", "banner": text}))


def do_waiting(state):
    """Phase gate — mark as waiting for Farlen's approval."""
    n = state["current_phase"]
    block_map = state["block_map"]
    phases = state["phases"]
    phase_name = phases[n]["name"]

    if f"phase_{n}_status" in block_map:
        notion.update_callout(block_map[f"phase_{n}_status"], "WAITING FOR FARLEN", "⏳")

    banner = f"{phase_name} COMPLETE — waiting for Farlen"
    if "status_banner" in block_map:
        notion.update_callout(block_map["status_banner"], banner, "✅")

    state["waiting_for_farlen"] = True
    save_state(state)
    print(json.dumps({"action": "waiting", "banner": banner}))


def do_continue(state):
    """Farlen approved — start the next phase."""
    n = state["current_phase"]
    phases = state["phases"]
    block_map = state["block_map"]

    new_phase = n + 1
    if new_phase >= len(phases):
        print(json.dumps({"action": "error", "message": "No more phases — build is complete"}))
        return

    phase_name = phases[new_phase]["name"]
    task_count = phases[new_phase]["task_count"]

    if f"phase_{new_phase}_status" in block_map:
        notion.update_callout(block_map[f"phase_{new_phase}_status"], "IN PROGRESS", "🟡")

    banner = f"{phase_name} — Task 1 of {task_count}"
    if "status_banner" in block_map:
        notion.update_callout(block_map["status_banner"], banner, "🔄")

    state["current_phase"] = new_phase
    state["current_task"] = 0
    state["phase_statuses"][str(new_phase)] = "in_progress"
    state["waiting_for_farlen"] = False
    save_state(state)
    print(json.dumps({"action": "continued", "phase": new_phase, "phase_name": phase_name, "banner": banner}))


def main():
    parser = argparse.ArgumentParser(description="Update build status in Notion and state.json")
    parser.add_argument("--state-file", required=True, help="Path to state.json")

    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--complete-task", action="store_true", help="Mark current task done, advance counter")
    mode.add_argument("--complete-phase", action="store_true", help="Mark current phase complete, set waiting_for_farlen")
    mode.add_argument("--blocked", metavar="REASON", help="Set blocked state with error message")
    mode.add_argument("--unblocked", action="store_true", help="Clear blocked state, resume IN PROGRESS")
    mode.add_argument("--status", metavar="TEXT", help="Set custom status banner text")
    mode.add_argument("--waiting", action="store_true", help="Set phase gate waiting state")
    mode.add_argument("--continue", dest="cont", action="store_true", help="Farlen approved — start next phase")

    parser.add_argument("--commit", default="", help="Git commit hash to record (used with --complete-task)")
    args = parser.parse_args()

    state = load_state(args.state_file)

    if args.complete_task:
        do_complete_task(state, args.commit or None)
    elif args.complete_phase:
        do_complete_phase(state)
    elif args.blocked:
        do_blocked(state, args.blocked)
    elif args.unblocked:
        do_unblocked(state)
    elif args.status:
        do_status(state, args.status)
    elif args.waiting:
        do_waiting(state)
    elif args.cont:
        do_continue(state)


if __name__ == "__main__":
    main()
