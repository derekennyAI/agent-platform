#!/usr/bin/env python3
"""Send a Telegram message via the Bot API directly — no MCP plugin dependency.

Scheduled one-shots (claude -p) cold-start the Telegram MCP plugin on every run;
a fast model (e.g. Haiku) can finish its turns and conclude "telegram tool not
available" before the plugin connects, so the send never happens. This helper
removes that race: it sends straight through the Bot API, which is available the
instant bash is, on any model.

Token source matches the plugin's: TELEGRAM_BOT_TOKEN from the environment, or
from $TELEGRAM_STATE_DIR/.env (the file the plugin itself loads).

Usage:
  tg_send.py <chat_id> <text...>
  echo "<text>" | tg_send.py <chat_id>

Exit 0 on success (prints 'sent (message_id=...)'), non-zero on failure.
"""
import json
import os
import sys
import urllib.request


def _resolve_token() -> str:
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if token:
        return token.strip()
    state_dir = os.environ.get("TELEGRAM_STATE_DIR") or os.path.join(
        os.path.expanduser("~"), ".claude", "channels", "telegram")
    env_path = os.path.join(state_dir, ".env")
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.split("=", 1)[1].strip()
    except OSError as e:
        sys.exit(f"tg_send: cannot read token from {env_path}: {e}")
    sys.exit("tg_send: no TELEGRAM_BOT_TOKEN (env or $TELEGRAM_STATE_DIR/.env)")


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: tg_send.py <chat_id> <text>")
    chat_id = sys.argv[1]
    text = " ".join(sys.argv[2:]).strip()
    if not text:
        text = sys.stdin.read().strip()
    if not text:
        sys.exit("tg_send: empty message")

    token = _resolve_token()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat_id, "text": text}).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            resp = json.load(r)
    except Exception as e:
        sys.exit(f"tg_send: send failed: {e}")
    if not resp.get("ok"):
        sys.exit(f"tg_send: API error: {resp}")
    print(f"sent (message_id={resp['result']['message_id']})")


if __name__ == "__main__":
    main()
