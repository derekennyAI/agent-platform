#!/bin/bash
# qmd_refresh_all.sh — re-index + re-embed every agent's ISOLATED qmd store.
#
# In a multi-agent fleet each agent has a PRIVATE qmd config + vector DB under
# ~/.qmd-<agent>/ (XDG_CONFIG_HOME / XDG_CACHE_HOME), pointed only at that
# agent's own memory dir, so no agent can search another agent's memory.
# See docs/qmd-setup.md → "Multi-Agent Isolation".
#
# This script discovers every ~/.qmd-<agent>/ store and refreshes it. Pure
# indexing — no LLM, no agent quota. Run it from launchd on a schedule
# (e.g. StartInterval 3600). Memory markdown files are regenerated from the
# source-of-truth store by the memory projector; this keeps the index in step.

export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
QMD="$(command -v qmd || echo "$HOME/.local/bin/qmd")"
LOG="$HOME/.claude/qmd-refresh-all.log"
ts() { date '+%Y-%m-%d %H:%M:%S'; }

mkdir -p "$(dirname "$LOG")"
echo "[$(ts)] qmd refresh-all starting" >> "$LOG"

shopt -s nullglob
for base in "$HOME"/.qmd-*/; do
    base="${base%/}"
    agent="$(basename "$base" | sed 's/^\.qmd-//')"
    cfg="$base/config"
    cache="$base/cache"
    [ -f "$cfg/qmd/index.yml" ] || { echo "[$(ts)] $agent: no index.yml — skip" >> "$LOG"; continue; }
    upd=$(XDG_CONFIG_HOME="$cfg" XDG_CACHE_HOME="$cache" "$QMD" update 2>&1 | tail -1)
    emb=$(XDG_CONFIG_HOME="$cfg" XDG_CACHE_HOME="$cache" "$QMD" embed 2>&1 | tail -1)
    echo "[$(ts)] $agent: $upd | $emb" >> "$LOG"
done

echo "[$(ts)] qmd refresh-all done" >> "$LOG"
