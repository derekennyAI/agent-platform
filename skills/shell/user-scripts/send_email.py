#!/usr/bin/env python3
"""Send email via Gmail API — vault-aware, multi-agent.

Reads credentials from MCP vault (scoped to AGENT_NAME) with local disk fallback.
Each agent sends from their own Gmail account.

Usage:
    python3 send_email.py --to "farlen@enny.ai" --subject "Daily Idea Digest" --body "markdown or plain text here"
    python3 send_email.py --to "farlen@enny.ai" --subject "Test" --body-file /tmp/digest.md
    echo "email body" | python3 send_email.py --to "farlen@enny.ai" --subject "Test" --stdin

Environment:
    AGENT_NAME — which agent's credentials to use (default: "derek")
    SUPABASE_SERVICE_KEY — required for vault access
"""

import argparse
import base64
import json
import os
import sys
import urllib.request
import urllib.error
import urllib.parse
from email.mime.text import MIMEText
from pathlib import Path

import sys as _sys; _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "mcp-server"))
from vault_client import get_credential as _vault_get

# Agent identity — determines whose credentials are loaded
_AGENT_NAME = os.environ.get("AGENT_NAME", "derek")

# Resolve workspace and config paths dynamically per agent
_WORKSPACE = Path.home() / _AGENT_NAME
_CONFIG_DIR = _WORKSPACE / ".config" / _AGENT_NAME
_ACCOUNTS_DIR = _CONFIG_DIR / "accounts"

# Discover token/creds paths for this agent
def _find_token_paths():
    paths = []
    if _ACCOUNTS_DIR.exists():
        for d in sorted(_ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and (d / "google-token.json").exists():
                paths.append(d / "google-token.json")
    root = _CONFIG_DIR / "google-token.json"
    if root.exists():
        paths.append(root)
    return paths

def _find_creds_paths():
    paths = []
    if _ACCOUNTS_DIR.exists():
        for d in sorted(_ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and (d / "google-credentials.json").exists():
                paths.append(d / "google-credentials.json")
    root = _CONFIG_DIR / "google-credentials.json"
    if root.exists():
        paths.append(root)
    return paths

TOKEN_PATHS = _find_token_paths()
CREDS_PATHS = _find_creds_paths()
CONTACTS_PATHS = [_CONFIG_DIR / "contacts.json"]

TOKEN_URI = "https://oauth2.googleapis.com/token"
SEND_URI = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"

# Detect FROM_EMAIL from the first token path directory name
def _detect_from_email():
    if TOKEN_PATHS:
        dir_name = TOKEN_PATHS[0].parent.name  # e.g. "derek_at_enny_ai"
        if "_at_" in dir_name:
            return dir_name.replace("_at_", "@").replace("_", ".")
    # Fallback: try vault metadata
    try:
        cred = _vault_get("gmail", "client_id", agent=_AGENT_NAME, with_metadata=True)
        if cred and cred.get("metadata", {}).get("email"):
            return cred["metadata"]["email"]
    except Exception:
        pass
    return f"{_AGENT_NAME}@enny.ai"  # last resort

FROM_EMAIL = _detect_from_email()


def find_file(paths):
    for p in paths:
        if p.exists():
            return p
    return None


def load_json(path):
    with open(path) as f:
        return json.load(f)


def _load_google_token():
    """Load Google OAuth token — vault first (scoped to AGENT_NAME), disk fallback."""
    # Try vault — look for access_token with email qualifier first, then bare
    try:
        if TOKEN_PATHS:
            dir_name = TOKEN_PATHS[0].parent.name
            access = _vault_get("gmail", f"access_token_{dir_name}", agent=_AGENT_NAME)
            refresh = _vault_get("gmail", f"refresh_token_{dir_name}", agent=_AGENT_NAME)
            if access and refresh:
                return {"access_token": access, "refresh_token": refresh}
        access = _vault_get("gmail", "access_token", agent=_AGENT_NAME)
        refresh = _vault_get("gmail", "refresh_token", agent=_AGENT_NAME)
        if access and refresh:
            return {"access_token": access, "refresh_token": refresh}
    except Exception:
        pass
    # Disk fallback
    for p in TOKEN_PATHS:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    raise RuntimeError(f"No Google token for agent '{_AGENT_NAME}' in vault or on disk")


def _load_google_creds():
    """Load Google OAuth credentials — vault first (scoped to AGENT_NAME), disk fallback."""
    try:
        client_id = _vault_get("gmail", "client_id", agent=_AGENT_NAME)
        client_secret = _vault_get("gmail", "client_secret", agent=_AGENT_NAME)
        if client_id and client_secret:
            return {"installed": {"client_id": client_id, "client_secret": client_secret}}
    except Exception:
        pass
    for p in CREDS_PATHS:
        if p.exists():
            with open(p) as f:
                return json.load(f)
    raise RuntimeError(f"No Google credentials for agent '{_AGENT_NAME}' in vault or on disk")


def _load_contacts():
    """Load contacts whitelist — vault first, disk fallback."""
    try:
        raw = _vault_get("gmail", "contacts_whitelist", agent=_AGENT_NAME)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    contacts_path = find_file(CONTACTS_PATHS)
    if contacts_path:
        return load_json(contacts_path)
    return None


def check_contacts_whitelist(to_email):
    """Verify recipient is in the approved contacts list. Exits if not."""
    contacts = _load_contacts()
    if not contacts:
        print("[error] contacts.json not found — cannot send without an address book", file=sys.stderr)
        sys.exit(1)

    allowed = [c["email"].lower() for c in contacts.get("contacts", [])]

    if to_email.lower() not in allowed:
        print(f"[blocked] {to_email} is not in the approved contacts list", file=sys.stderr)
        print(f"[blocked] ask admin to add this contact to {_CONFIG_DIR}/contacts.json", file=sys.stderr)
        sys.exit(1)


def refresh_access_token(creds, token_data):
    """Refresh the access token using the refresh token."""
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        print("[error] no refresh token found", file=sys.stderr)
        sys.exit(1)

    client = creds.get("installed", creds)
    data = urllib.parse.urlencode({
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(TOKEN_URI, data=data)
    with urllib.request.urlopen(req) as resp:
        new_token = json.loads(resp.read().decode())

    # Preserve refresh token (not always returned on refresh)
    new_token["refresh_token"] = refresh_token
    return new_token


def send_email(access_token, to, subject, body, content_type="plain"):
    """Send an email via Gmail API."""
    msg = MIMEText(body, content_type)
    msg["to"] = to
    msg["from"] = FROM_EMAIL
    msg["subject"] = subject

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    payload = json.dumps({"raw": raw}).encode()

    req = urllib.request.Request(SEND_URI, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.loads(resp.read().decode())
            return result
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        if e.code == 401:
            return None  # signal to refresh and retry
        print(f"[error] Gmail API {e.code}: {error_body}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description=f"Send email via Gmail (agent: {_AGENT_NAME}, from: {FROM_EMAIL})")
    parser.add_argument("--to", required=True, help="Recipient email")
    parser.add_argument("--subject", required=True, help="Email subject")
    parser.add_argument("--body", default=None, help="Email body text")
    parser.add_argument("--body-file", default=None, help="Read body from file")
    parser.add_argument("--stdin", action="store_true", help="Read body from stdin")
    parser.add_argument("--html", action="store_true", help="Send as HTML instead of plain text")
    args = parser.parse_args()

    # Get body
    if args.body:
        body = args.body
    elif args.body_file:
        body = Path(args.body_file).read_text()
    elif args.stdin:
        body = sys.stdin.read()
    else:
        print("[error] provide --body, --body-file, or --stdin", file=sys.stderr)
        sys.exit(1)

    # Check recipient is approved
    check_contacts_whitelist(args.to)

    # Load credentials and token (vault first, disk fallback)
    try:
        token_data = _load_google_token()
        creds = _load_google_creds()
    except RuntimeError as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
    token_path = find_file(TOKEN_PATHS)  # for disk write-back on refresh
    access_token = token_data.get("access_token", "")

    content_type = "html" if args.html else "plain"

    # Try sending
    result = send_email(access_token, args.to, args.subject, body, content_type)

    if result is None:
        # Token expired, refresh and retry
        print("[info] refreshing access token...", file=sys.stderr)
        token_data = refresh_access_token(creds, token_data)
        access_token = token_data["access_token"]

        # Save refreshed token to disk if path exists
        if token_path:
            with open(token_path, "w") as f:
                json.dump(token_data, f, indent=2)

        result = send_email(access_token, args.to, args.subject, body, content_type)

    if result:
        print(json.dumps({
            "status": "sent",
            "to": args.to,
            "subject": args.subject,
            "message_id": result.get("id", ""),
        }))
    else:
        print("[error] failed to send after token refresh", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
