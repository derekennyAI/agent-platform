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

# Outbound-log: after the reply tool successfully sends its text chunks, record
# the verbatim text to sent-messages.jsonl. This is the ground truth of what the
# user received — scheduled-task one-shots send via this same path, and the main
# session (which never sees the one-shot's transcript) reads this log via the
# inject_recent_tasks hook to recover what was sent on its behalf.
OUTBOUND_ANCHOR = "        // Files go as separate messages (Telegram doesn't mix text+file in one"
OUTBOUND_LOG = '''        // outbound-log-v1: record what we actually sent so the main session
        // (which never sees one-shot transcripts) can recover the content.
        try {
          _traceAppend(join(STATE_DIR, 'sent-messages.jsonl'),
            JSON.stringify({ ts: new Date().toISOString(), chat_id, text }) + '\\n')
        } catch {}

'''

# Send-only mode (send-only-v1). Telegram allows exactly ONE getUpdates consumer
# per bot token. Scheduled `claude -p` one-shots run in an isolated -cron config
# that still enables the telegram plugin (so the agent can deliver via the reply
# tool) — but if that plugin POLLS, it steals the long-poll from the live
# --channels session: server.ts SIGTERMs the live poller ("replacing stale
# poller pid=N"), runs a few seconds, exits, and the live session is deaf until
# claude respawns it. That contention was the real cause of Derek's flapping.
# In send-only mode (env TELEGRAM_SEND_ONLY=1) the plugin still serves the reply
# tool (bot.api.sendMessage, independent of polling) but never SIGTERMs the live
# poller, never writes the shared bot.pid, and never starts the inbound poll loop.
SENDONLY_STATIC_OLD = "const STATIC = process.env.TELEGRAM_ACCESS_MODE === 'static'"
SENDONLY_STATIC_NEW = (
    "const STATIC = process.env.TELEGRAM_ACCESS_MODE === 'static'\n"
    "const SEND_ONLY = process.env.TELEGRAM_SEND_ONLY === '1' // send-only-v1: push one-shots send via reply but never poll"
)
SENDONLY_PID_OLD = '''try {
  const stale = parseInt(readFileSync(PID_FILE, 'utf8'), 10)
  if (stale > 1 && stale !== process.pid) {
    process.kill(stale, 0)
    process.stderr.write(`telegram channel: replacing stale poller pid=${stale}\\n`)
    process.kill(stale, 'SIGTERM')
  }
} catch {}
writeFileSync(PID_FILE, String(process.pid))'''
SENDONLY_PID_NEW = '''if (!SEND_ONLY) {
  // send-only-v1: one-shots must not SIGTERM the live poller or clobber bot.pid
  try {
    const stale = parseInt(readFileSync(PID_FILE, 'utf8'), 10)
    if (stale > 1 && stale !== process.pid) {
      process.kill(stale, 0)
      process.stderr.write(`telegram channel: replacing stale poller pid=${stale}\\n`)
      process.kill(stale, 'SIGTERM')
    }
  } catch {}
  writeFileSync(PID_FILE, String(process.pid))
}'''
SENDONLY_IIFE_OLD = '''void (async () => {
  for (let attempt = 1; ; attempt++) {'''
SENDONLY_IIFE_NEW = '''void (async () => {
  if (SEND_ONLY) { trace('send_only_no_poll'); return } // send-only-v1: never start the inbound poll loop
  for (let attempt = 1; ; attempt++) {'''


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

    # --- Patch 4: outbound message logging in the reply tool
    if "outbound-log-v1" not in content and OUTBOUND_ANCHOR in content:
        content = content.replace(OUTBOUND_ANCHOR, OUTBOUND_LOG + OUTBOUND_ANCHOR, 1)

    # --- Patch 5: send-only mode (send-only-v1). Own marker/guard so it applies
    # independently of the trace/outbound patches. Only proceeds when the STATIC
    # anchor is present (so SEND_ONLY actually gets defined before it's used).
    if "send-only-v1" not in content and SENDONLY_STATIC_OLD in content:
        content = content.replace(SENDONLY_STATIC_OLD, SENDONLY_STATIC_NEW, 1)
        content = content.replace(SENDONLY_PID_OLD, SENDONLY_PID_NEW, 1)
        content = content.replace(SENDONLY_IIFE_OLD, SENDONLY_IIFE_NEW, 1)

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
