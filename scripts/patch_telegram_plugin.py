#!/usr/bin/env python3
"""Idempotent patcher for the telegram channel plugin.

Two changes:

1. Orphan-watchdog fix: removes the PPID drift check that caused the plugin
   to self-terminate on CC 2.1.153. Keeps stdin-based death detection.
2. Lifecycle tracing: writes structured events to a per-agent trace log so
   we can diagnose future deaths (bun rarely emits useful errors on its own).

Idempotent: re-run safely after the marketplace re-pulls the plugin.
Called from launcher.sh on every boot.
"""

import glob
import os
import re
import sys
from pathlib import Path

# Tracing block injected before `let shuttingDown = false`. Marker comment
# `telegram-trace-v1` keeps it idempotent.
TRACE_BLOCK = '''// telegram-trace-v1 — lifecycle tracing for debugging plugin deaths
import { appendFileSync as _traceAppend, statSync as _traceStat, renameSync as _traceRename } from 'fs'
const TRACE_FILE = join(STATE_DIR, 'lifecycle-trace.jsonl')
const TRACE_BOOT_PPID = process.ppid
let TRACE_LAST_PPID = process.ppid
function trace(event: string, extra: Record<string, unknown> = {}): void {
  try {
    try {
      const st = _traceStat(TRACE_FILE)
      if (st.size > 5_000_000) _traceRename(TRACE_FILE, TRACE_FILE + '.1')
    } catch {}
    _traceAppend(TRACE_FILE, JSON.stringify({
      ts: new Date().toISOString(),
      pid: process.pid,
      ppid: process.ppid,
      event,
      ...extra,
    }) + '\\n')
  } catch {}
}
trace('boot', { bootPpid: TRACE_BOOT_PPID, stateDir: STATE_DIR })

'''

# Replacement for the orphan setInterval block. Logs PPID drift but does NOT
# auto-shutdown on it. Still shuts down on stdin destroyed/ended (real death).
WATCHDOG_REPLACEMENT = '''setInterval(() => {
  if (process.ppid !== TRACE_LAST_PPID) {
    trace('ppid_change', { from: TRACE_LAST_PPID, to: process.ppid })
    TRACE_LAST_PPID = process.ppid
  }
  const orphaned = process.stdin.destroyed || process.stdin.readableEnded
  if (orphaned) shutdown('orphan_watchdog')
}, 5000).unref()'''


def patch_one(path: str) -> bool:
    """Patch one server.ts file. Returns True if changed."""
    content = Path(path).read_text()
    original = content

    # Backup once before any modification
    bak = path + ".bak-pre-trace"
    if not os.path.exists(bak):
        Path(bak).write_text(original)

    # --- Patch 1: inject TRACE_BLOCK before `let shuttingDown = false`
    if "telegram-trace-v1" not in content:
        content = content.replace(
            "let shuttingDown = false",
            TRACE_BLOCK + "let shuttingDown = false",
            1,
        )

    # --- Patch 2: wrap shutdown() to accept a reason and trace it
    if "function shutdown(reason" not in content:
        content = content.replace(
            "function shutdown(): void {\n  if (shuttingDown) return",
            "function shutdown(reason: string = 'unknown'): void {\n  trace('shutdown', { reason })\n  if (shuttingDown) return",
            1,
        )
        # Update each shutdown caller to pass a reason
        for trigger, label in [
            ("process.stdin.on('end', shutdown)", "process.stdin.on('end', () => shutdown('stdin_end'))"),
            ("process.stdin.on('close', shutdown)", "process.stdin.on('close', () => shutdown('stdin_close'))"),
            ("process.on('SIGTERM', shutdown)", "process.on('SIGTERM', () => shutdown('SIGTERM'))"),
            ("process.on('SIGINT', shutdown)", "process.on('SIGINT', () => shutdown('SIGINT'))"),
            ("process.on('SIGHUP', shutdown)", "process.on('SIGHUP', () => shutdown('SIGHUP'))"),
        ]:
            content = content.replace(trigger, label, 1)

    # --- Patch 3: replace the orphan setInterval block (PPID drift no longer
    # triggers shutdown — but we log when it happens).
    orphan_re = re.compile(
        r"setInterval\(\(\) => \{\s*\n"
        r"  const orphaned =\s*\n"
        r"    \(process\.platform !== 'win32' && process\.ppid !== bootPpid\) \|\|\s*\n"
        r"    process\.stdin\.destroyed \|\|\s*\n"
        r"    process\.stdin\.readableEnded\s*\n"
        r"  if \(orphaned\) shutdown\(\)\s*\n"
        r"\}, 5000\)\.unref\(\)"
    )
    if orphan_re.search(content):
        content = orphan_re.sub(WATCHDOG_REPLACEMENT, content)

    if content != original:
        Path(path).write_text(content)
        return True
    return False


def main():
    home = os.path.expanduser("~")
    paths = glob.glob(
        f"{home}/.claude-*/plugins/cache/claude-plugins-official/telegram/*/server.ts"
    )
    # Skip retired/archived agent dirs
    paths = [p for p in paths if ".retired-" not in p]
    changed = 0
    for p in paths:
        try:
            if patch_one(p):
                print(f"patched: {p}")
                changed += 1
        except Exception as e:
            print(f"ERROR {p}: {e}", file=sys.stderr)
    print(f"done — {changed} of {len(paths)} files patched")


if __name__ == "__main__":
    main()
