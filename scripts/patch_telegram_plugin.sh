#!/bin/bash
# Idempotent patcher for the telegram channel plugin.
#
# Why: Claude Code 2.1.153 changed MCP server lifecycle (changelog item
# "Fixed stateful MCP servers without optional GET SSE stream reconnect-looping
# on tools/list"). This interacts badly with the plugin's orphan watchdog,
# which self-terminates when process.ppid changes. Result: bun subprocess
# dies on active agents, claude stays alive, agent is deaf to Telegram.
#
# This patch removes the PPID check from the orphan watchdog. stdin-based
# death detection (process.stdin.destroyed / readableEnded) is preserved.
#
# Idempotent: re-run safely after the marketplace re-pulls the plugin.
# Run from launcher.sh on every boot.

set -e

# Patch every per-agent plugin cache we find. Hardlinks mean we may patch
# the same inode multiple times — that's fine, the grep guard handles it.
for server in "$HOME"/.claude-*/plugins/cache/claude-plugins-official/telegram/*/server.ts; do
    [ -f "$server" ] || continue
    if ! grep -q "process.ppid !== bootPpid" "$server"; then
        continue  # already patched
    fi

    python3 << PYEOF
import re
path = "$server"
content = open(path).read()
new = re.sub(
    r"const orphaned =\s*\n\s*\(process\.platform !== 'win32' && process\.ppid !== bootPpid\) \|\|\s*\n\s*process\.stdin\.destroyed \|\|\s*\n\s*process\.stdin\.readableEnded",
    "const orphaned = process.stdin.destroyed || process.stdin.readableEnded",
    content
)
if new != content:
    # One-time backup
    bak = path + ".bak-pre-ppid-fix"
    import os
    if not os.path.exists(bak):
        open(bak, "w").write(content)
    open(path, "w").write(new)
    print(f"patched: {path}")
PYEOF
done
