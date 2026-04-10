#!/bin/bash
# Security tripwire runner — sends Telegram alert if anything trips.
# Called by system cron. No openclaw agent involvement.

BOT_TOKEN="8665131154:AAFVMScwCp5YT4BwB6-jDaYRhAEcNYkBDKM"
CHAT_ID="8676483103"
SCRIPT="$HOME/derek/scripts/security_watch.py"

# Run checks passed as args (e.g. --integrity --sessions or --exec-audit)
OUTPUT=$(python3 "$SCRIPT" "$@" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    # Trim to Telegram's 4096-char limit
    MSG=$(echo "🚨 SECURITY ALERT from Derek:

$OUTPUT" | head -c 3900)

    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=${MSG}" \
        --data-urlencode "text=${MSG}" \
        > /dev/null 2>&1
fi
