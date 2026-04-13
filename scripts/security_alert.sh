#!/bin/bash
# Security tripwire runner — sends Telegram alert if anything trips.
# Called by system cron. Runs independently of the agent session.

BOT_TOKEN="${ADMIN_TELEGRAM_BOT_TOKEN:?Set ADMIN_TELEGRAM_BOT_TOKEN}"
CHAT_ID="${ADMIN_TELEGRAM_CHAT_ID:?Set ADMIN_TELEGRAM_CHAT_ID}"
SCRIPT="$(dirname "$0")/security_watch.py"

# Run checks passed as args (e.g. --integrity --sessions or --exec-audit)
OUTPUT=$(python3 "$SCRIPT" "$@" 2>&1)
EXIT_CODE=$?

if [ $EXIT_CODE -ne 0 ]; then
    # Trim to Telegram's 4096-char limit
    MSG=$(echo "🚨 SECURITY ALERT:

$OUTPUT" | head -c 3900)

    curl -s -X POST "https://api.telegram.org/bot${BOT_TOKEN}/sendMessage" \
        -d "chat_id=${CHAT_ID}" \
        -d "text=${MSG}" \
        --data-urlencode "text=${MSG}" \
        > /dev/null 2>&1
fi
