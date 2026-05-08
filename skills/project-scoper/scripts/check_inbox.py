#!/usr/bin/env python3
"""Check Derek's Gmail inbox for scope requests from Farlen.

Looks for emails with subject starting "Scope:" and extracts the idea ID.
Marks processed emails as read.

Usage:
    python3 check_inbox.py
    python3 check_inbox.py --mark-read
"""

import argparse
import json
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path

import os as _os
_AGENT_NAME = _os.environ.get("AGENT_NAME", "derek")
_WORKSPACE = Path(f"/Users/YOUR_MAC_USERNAME/{_AGENT_NAME}")
_CONFIG_DIR = _WORKSPACE / ".config" / _AGENT_NAME
_ACCOUNTS_DIR = _CONFIG_DIR / "accounts"


def _discover_paths(filename):
    """Discover credential files from agent's accounts directory."""
    paths = []
    if _ACCOUNTS_DIR.exists():
        for d in sorted(_ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and (d / filename).exists():
                paths.append(d / filename)
    root = _CONFIG_DIR / filename
    if root.exists():
        paths.append(root)
    return paths


TOKEN_PATHS = _discover_paths("google-token.json")
CREDS_PATHS = _discover_paths("google-credentials.json")

TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"


def find_file(paths):
    for p in paths:
        if p.exists():
            return p
    return None


def load_json(path):
    with open(path) as f:
        return json.load(f)


# Module-level auth state so token refreshes propagate to all requests
_auth = {"token": None, "token_path": None, "creds_path": None}


def refresh_token(creds, token_data):
    """Force-refresh via the shared helper. `creds` and `token_data` are
    accepted for back-compat; the helper resolves credentials as a sibling
    of the token file. Returns the freshly-written token dict."""
    import sys as _sys
    _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/_lib")
    from google_auth import get_token as _gauth_get_token
    token_path = _auth.get("token_path")
    if not token_path:
        raise RuntimeError("token_path not set; call get_access_token() first")
    _gauth_get_token(token_path, force_refresh=True)
    with open(token_path) as f:
        return json.load(f)


def _do_request(token, method, path, body=None):
    url = f"{GMAIL_API}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def gmail_request(method, path, body=None):
    """Make a Gmail API request. Auto-refreshes token on 401."""
    try:
        return _do_request(_auth["token"], method, path, body)
    except urllib.error.HTTPError as e:
        if e.code == 401 and _auth["token_path"]:
            creds = load_json(_auth["creds_path"])
            token_data = load_json(_auth["token_path"])
            new_token = refresh_token(creds, token_data)
            with open(_auth["token_path"], "w") as f:
                json.dump(new_token, f, indent=2)
            _auth["token"] = new_token["access_token"]
            return _do_request(_auth["token"], method, path, body)
        raise


def get_access_token():
    """Load and refresh access token if needed."""
    creds_path = find_file(CREDS_PATHS)
    token_path = find_file(TOKEN_PATHS)
    if not creds_path or not token_path:
        print('{"error": "google credentials or token not found"}', file=sys.stderr)
        sys.exit(1)

    _auth["token_path"] = token_path
    _auth["creds_path"] = creds_path

    creds = load_json(creds_path)
    token_data = load_json(token_path)
    _auth["token"] = token_data["access_token"]

    try:
        gmail_request("GET", "/profile")
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise  # gmail_request already handled 401 and retried


def search_scope_emails():
    """Search for unread emails with 'Scope:' in subject from Farlen."""
    query = urllib.parse.quote("subject:Scope: is:unread from:YOUR_ADMIN_EMAIL")
    result = gmail_request("GET", f"/messages?q={query}&maxResults=10")
    return result.get("messages", [])


def get_message(msg_id):
    """Get a specific message's details."""
    return gmail_request("GET", f"/messages/{msg_id}?format=metadata&metadataHeaders=Subject")


def mark_read(msg_id):
    """Mark a message as read."""
    gmail_request("POST", f"/messages/{msg_id}/modify", {
        "removeLabelIds": ["UNREAD"],
    })


def extract_idea_id(subject):
    """Extract idea ID from a subject line like 'Scope: idea-20260305-001'."""
    # Match "Scope: idea-YYYYMMDD-NNN" or just "Scope: <anything>"
    match = re.search(r"Scope:\s*(idea-\d{8}-\d{3})", subject)
    if match:
        return match.group(1)
    # Fall back to everything after "Scope:"
    match = re.search(r"Scope:\s*(.+)", subject)
    if match:
        return match.group(1).strip()
    return None


def main():
    parser = argparse.ArgumentParser(description="Check inbox for scope requests")
    parser.add_argument("--mark-read", action="store_true", help="Mark processed emails as read")
    args = parser.parse_args()

    get_access_token()
    messages = search_scope_emails()

    if not messages:
        print(json.dumps({"count": 0, "requests": []}))
        return

    requests = []
    for msg_ref in messages:
        msg = get_message(msg_ref["id"])
        headers = msg.get("payload", {}).get("headers", [])
        subject = next((h["value"] for h in headers if h["name"] == "Subject"), "")
        idea_id = extract_idea_id(subject)

        if idea_id:
            requests.append({
                "message_id": msg_ref["id"],
                "subject": subject,
                "idea_id": idea_id,
            })

            if args.mark_read:
                mark_read(msg_ref["id"])

    print(json.dumps({"count": len(requests), "requests": requests}, indent=2))


if __name__ == "__main__":
    main()
