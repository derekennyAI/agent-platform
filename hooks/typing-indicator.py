#!/usr/bin/env python3
"""
Typing indicator hook for Telegram.

Fires on UserPromptSubmit (message arrival) and PreToolUse (keep alive during work).
Sends sendChatAction "typing" so the user sees "typing..." in Telegram.

On UserPromptSubmit: starts a background pulser that sends typing every 4s.
On PreToolUse: refreshes the pulser's stop-time (keeps it alive).
On telegram__reply/react: kills the pulser (response is going out).
"""

import json, sys, os, re, urllib.request, time, subprocess, signal

PULSE_PID_FILE = "/tmp/derek_typing_pulse.pid"
PULSE_STOP_FILE = "/tmp/derek_typing_stop"
CHATID_FILE = "/tmp/derek_typing_chatid"

PULSER_SCRIPT = r"""
import json, urllib.request, time, os, sys

token = sys.argv[1]
chat_id = sys.argv[2]
stop_file = sys.argv[3]

def send_typing():
    try:
        url = f"https://api.telegram.org/bot{token}/sendChatAction"
        data = json.dumps({"chat_id": chat_id, "action": "typing"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=3)
    except Exception:
        pass

# Pulse immediately, then every 4 seconds
# Stop after 120s max (safety net) or when stop file appears
start = time.time()
while time.time() - start < 120:
    send_typing()
    time.sleep(4)
    if os.path.exists(stop_file):
        break

# Clean up
try:
    os.remove(stop_file)
except Exception:
    pass
"""


def get_bot_token():
    state_dir = os.environ.get("TELEGRAM_STATE_DIR", os.path.expanduser("~/.claude/channels/telegram"))
    token_file = os.path.join(state_dir, ".env")
    try:
        with open(token_file) as f:
            for line in f:
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    return line.strip().split("=", 1)[1]
    except Exception:
        pass
    return None


def extract_chat_id(text):
    m = re.search(r'source="plugin:telegram:telegram"[^>]*chat_id="(\d+)"', text)
    return m.group(1) if m else None


def kill_pulser():
    """Kill any existing background pulser."""
    try:
        if os.path.exists(PULSE_PID_FILE):
            pid = int(open(PULSE_PID_FILE).read().strip())
            os.kill(pid, signal.SIGTERM)
    except (ProcessLookupError, ValueError, OSError):
        pass
    # Also create stop file as backup
    try:
        with open(PULSE_STOP_FILE, "w") as f:
            f.write("stop")
    except Exception:
        pass


def start_pulser(token, chat_id):
    """Start background pulser process."""
    # Kill any existing one first
    kill_pulser()
    # Remove old stop file
    try:
        os.remove(PULSE_STOP_FILE)
    except Exception:
        pass
    # Launch background python process
    proc = subprocess.Popen(
        ["python3", "-c", PULSER_SCRIPT, token, chat_id, PULSE_STOP_FILE],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True
    )
    # Save PID
    with open(PULSE_PID_FILE, "w") as f:
        f.write(str(proc.pid))


def main():
    hook_input = json.loads(sys.stdin.read())
    event = hook_input.get("hookEventName", "")

    if event == "UserPromptSubmit":
        prompt = hook_input.get("prompt", "")
        chat_id = extract_chat_id(prompt)
        if chat_id:
            # Save chat_id for later
            with open(CHATID_FILE, "w") as f:
                f.write(chat_id)
            token = get_bot_token()
            if token:
                start_pulser(token, chat_id)

    elif event == "PreToolUse":
        tool_name = hook_input.get("toolName", "")
        # Stop pulsing when we're about to reply
        if "telegram__reply" in tool_name or "telegram__react" in tool_name:
            kill_pulser()
        # Otherwise do nothing — pulser is already running in background

    # Always allow
    json.dump({}, sys.stdout)


if __name__ == "__main__":
    main()
