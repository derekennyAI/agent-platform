#!/bin/bash
# Thin wrapper that runs the Python patcher (pure Python — no bash heredoc
# escaping issues). See patch_telegram_plugin.py for the actual logic.
exec python3 "$(dirname "$0")/patch_telegram_plugin.py"
