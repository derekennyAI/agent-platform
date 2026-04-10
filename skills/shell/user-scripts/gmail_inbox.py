#!/usr/bin/env python3
"""Gmail inbox management — vault-aware, multi-agent.

Reads credentials from MCP vault (scoped to AGENT_NAME) with local disk fallback.
Each agent uses their own Gmail tokens — no cross-agent credential access.

Usage:
    python3 gmail_inbox.py list [--max 20] [--query "is:unread"]
    python3 gmail_inbox.py read <message_id>
    python3 gmail_inbox.py summary [--max 50]
    python3 gmail_inbox.py archive <message_id>
    python3 gmail_inbox.py mark-read <message_id>

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
from pathlib import Path

import sys as _sys; _sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "mcp-server"))
from vault_client import get_credential as _vault_get, get_credentials as _vault_get_all

# Agent identity — determines whose credentials are loaded
_AGENT_NAME = os.environ.get("AGENT_NAME", "derek")

# Resolve workspace and config paths dynamically per agent
_WORKSPACE = Path.home() / _AGENT_NAME
_CONFIG_DIR = _WORKSPACE / ".config" / _AGENT_NAME
_ACCOUNTS_DIR = _CONFIG_DIR / "accounts"

TOKEN_URI = "https://oauth2.googleapis.com/token"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"

# Active account — set by --account flag (email alias), defaults to None (first available)
_active_account = None

# Account directory mapping — built dynamically from what exists on disk
def _build_account_dirs():
    """Discover account directories for this agent."""
    dirs = {}
    if _ACCOUNTS_DIR.exists():
        for d in _ACCOUNTS_DIR.iterdir():
            if d.is_dir() and (d / "google-token.json").exists():
                # Convert dir name back to a short alias
                name = d.name  # e.g. "derek_at_enny_ai"
                # Create short aliases from the directory name
                email = name.replace("_at_", "@").replace("_", ".")
                short = email.split("@")[0]  # e.g. "derek", "fmischel"
                dirs[short] = d
                dirs[name] = d  # also allow full dir name
    return dirs

_ACCOUNT_DIRS = _build_account_dirs()


def load_json(path):
    with open(path) as f:
        return json.load(f)


def _resolve_account_dir():
    """Resolve the account directory for the active account."""
    if _active_account:
        acct_dir = _ACCOUNT_DIRS.get(_active_account)
        if acct_dir:
            return acct_dir
        raise RuntimeError(f"No Gmail account '{_active_account}' found for agent '{_AGENT_NAME}'. Available: {list(_ACCOUNT_DIRS.keys())}")
    # No specific account — use first available, or fall back to config root
    if _ACCOUNT_DIRS:
        return next(iter(_ACCOUNT_DIRS.values()))
    return _CONFIG_DIR


def _vault_token_key():
    """Build vault credential key, optionally scoped to active account."""
    if _active_account and _active_account in _ACCOUNT_DIRS:
        # Use the directory name as the email qualifier
        dir_name = _ACCOUNT_DIRS[_active_account].name
        return f"access_token_{dir_name}", f"refresh_token_{dir_name}"
    return "access_token", "refresh_token"


def _load_token_data():
    """Load Gmail token — vault first (scoped to AGENT_NAME), disk fallback."""
    access_key, refresh_key = _vault_token_key()

    # Try vault first
    try:
        access = _vault_get("gmail", access_key, agent=_AGENT_NAME)
        refresh = _vault_get("gmail", refresh_key, agent=_AGENT_NAME)
        if access and refresh:
            return {"access_token": access, "refresh_token": refresh}, "vault"
    except Exception:
        pass

    # Disk fallback
    acct_dir = _resolve_account_dir()
    disk = acct_dir / "google-token.json"
    if disk.exists():
        return load_json(disk), "disk"
    raise RuntimeError(f"No Gmail token for agent '{_AGENT_NAME}' in vault or at {disk}")


def _load_creds_data():
    """Load Gmail OAuth client credentials — vault first, disk fallback."""
    # Try vault first
    try:
        client_id = _vault_get("gmail", "client_id", agent=_AGENT_NAME)
        client_secret = _vault_get("gmail", "client_secret", agent=_AGENT_NAME)
        if client_id and client_secret:
            return {"installed": {"client_id": client_id, "client_secret": client_secret}}
    except Exception:
        pass

    # Disk fallback
    acct_dir = _resolve_account_dir()
    disk = acct_dir / "google-credentials.json"
    if disk.exists():
        return load_json(disk)
    # Also check config root
    root_disk = _CONFIG_DIR / "google-credentials.json"
    if root_disk.exists():
        return load_json(root_disk)
    raise RuntimeError(f"No Gmail credentials for agent '{_AGENT_NAME}' in vault or on disk")


# Module-level auth state so token refreshes mid-execution propagate everywhere
_auth = {"token": None, "token_path": None, "creds_path": None}


def refresh_token(creds, token_data):
    client = creds.get("installed", creds)
    data = urllib.parse.urlencode({
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request(TOKEN_URI, data=data)
    with urllib.request.urlopen(req) as resp:
        new_token = json.loads(resp.read().decode())
    new_token["refresh_token"] = token_data["refresh_token"]
    return new_token


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
        if e.code == 401:
            token_data, _ = _load_token_data()
            creds = _load_creds_data()
            new_token = refresh_token(creds, token_data)
            _auth["token"] = new_token["access_token"]
            return _do_request(_auth["token"], method, path, body)
        raise


def _save_refreshed_token(new_token):
    """Save refreshed token back to vault + disk."""
    acct_dir = _resolve_account_dir()
    path = acct_dir / "google-token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(new_token, indent=2))

    # Also update vault
    access_key, refresh_key = _vault_token_key()
    try:
        from vault_client import get_credential  # re-import to avoid circular
        # Use the Supabase REST API directly for upsert
        import urllib.request as _ur
        VAULT_URL = os.environ.get("SUPABASE_URL", "")
        VAULT_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if VAULT_KEY and new_token.get("access_token"):
            for k, v in [(access_key, new_token["access_token"]), (refresh_key, new_token.get("refresh_token", ""))]:
                if not v:
                    continue
                body = json.dumps({"agent_name": _AGENT_NAME, "service": "gmail", "credential_key": k, "credential_value": v, "metadata": {}}).encode()
                req = _ur.Request(
                    f"{VAULT_URL}/rest/v1/agent_credentials?on_conflict=agent_name,service,credential_key",
                    data=body, method="POST",
                    headers={"apikey": VAULT_KEY, "Authorization": f"Bearer {VAULT_KEY}", "Content-Type": "application/json", "Prefer": "return=minimal,resolution=merge-duplicates"},
                )
                try:
                    _ur.urlopen(req)
                except Exception:
                    pass  # Vault write failure is non-fatal
    except Exception:
        pass


def get_access_token():
    token_data, source = _load_token_data()
    _auth["token"] = token_data["access_token"]

    try:
        gmail_request("GET", "/profile")
    except urllib.error.HTTPError as e:
        if e.code == 401:
            pass  # gmail_request already refreshed and retried
        else:
            raise


def get_header(headers, name):
    return next((h["value"] for h in headers if h["name"].lower() == name.lower()), "")


def cmd_list(args):
    get_access_token()
    query = args.query or "in:inbox"
    encoded_q = urllib.parse.quote(query)
    result = gmail_request("GET", f"/messages?q={encoded_q}&maxResults={args.max}")
    messages = result.get("messages", [])

    if not messages:
        print(json.dumps({"count": 0, "messages": []}))
        return

    output = []
    for msg_ref in messages:
        msg = gmail_request("GET",
            f"/messages/{msg_ref['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date")
        headers = msg.get("payload", {}).get("headers", [])
        labels = msg.get("labelIds", [])
        output.append({
            "id": msg_ref["id"],
            "subject": get_header(headers, "Subject"),
            "from": get_header(headers, "From"),
            "date": get_header(headers, "Date"),
            "unread": "UNREAD" in labels,
            "labels": [l for l in labels if l not in ("UNREAD", "INBOX", "CATEGORY_UPDATES", "CATEGORY_PROMOTIONS", "CATEGORY_SOCIAL", "CATEGORY_FORUMS")],
        })

    print(json.dumps({"count": len(output), "messages": output}, indent=2))


def cmd_read(args):
    get_access_token()
    msg = gmail_request("GET", f"/messages/{args.message_id}?format=full")
    headers = msg.get("payload", {}).get("headers", [])

    # Extract body
    body = ""
    payload = msg.get("payload", {})
    if payload.get("body", {}).get("data"):
        body = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", errors="replace")
    elif payload.get("parts"):
        for part in payload["parts"]:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                break
        if not body:
            for part in payload["parts"]:
                if part.get("mimeType") == "text/html" and part.get("body", {}).get("data"):
                    body = base64.urlsafe_b64decode(part["body"]["data"]).decode("utf-8", errors="replace")
                    break

    print(json.dumps({
        "id": args.message_id,
        "subject": get_header(headers, "Subject"),
        "from": get_header(headers, "From"),
        "to": get_header(headers, "To"),
        "date": get_header(headers, "Date"),
        "body": body[:3000],
        "truncated": len(body) > 3000,
    }, indent=2))


def cmd_summary(args):
    """Get inbox stats — counts actual messages, not Gmail's unreliable resultSizeEstimate."""
    get_access_token()

    # Count actual inbox messages by paginating (up to 500)
    total_count = 0
    unread_total = 0
    all_inbox = gmail_request("GET", "/messages?q=" + urllib.parse.quote("in:inbox") + "&maxResults=100")
    total_count = len(all_inbox.get("messages", []))
    next_token = all_inbox.get("nextPageToken")
    while next_token and total_count < 500:
        page = gmail_request("GET", "/messages?q=" + urllib.parse.quote("in:inbox") + f"&maxResults=100&pageToken={next_token}")
        total_count += len(page.get("messages", []))
        next_token = page.get("nextPageToken")

    # Count actual unread
    all_unread = gmail_request("GET", "/messages?q=" + urllib.parse.quote("in:inbox is:unread") + "&maxResults=100")
    unread_total = len(all_unread.get("messages", []))
    next_token = all_unread.get("nextPageToken")
    while next_token and unread_total < 500:
        page = gmail_request("GET", "/messages?q=" + urllib.parse.quote("in:inbox is:unread") + f"&maxResults=100&pageToken={next_token}")
        unread_total += len(page.get("messages", []))
        next_token = page.get("nextPageToken")

    # Get recent unread details
    recent = all_unread
    messages = recent.get("messages", [])

    unread_list = []
    for msg_ref in messages[:20]:
        msg = gmail_request("GET",
            f"/messages/{msg_ref['id']}?format=metadata&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date")
        headers = msg.get("payload", {}).get("headers", [])
        unread_list.append({
            "id": msg_ref["id"],
            "subject": get_header(headers, "Subject"),
            "from": get_header(headers, "From"),
            "date": get_header(headers, "Date"),
        })

    print(json.dumps({
        "total_inbox": total_count,
        "unread": unread_total,
        "recent_unread": unread_list,
    }, indent=2))


def cmd_archive(args):
    get_access_token()
    gmail_request("POST", f"/messages/{args.message_id}/modify", {
        "removeLabelIds": ["INBOX"],
    })
    print(json.dumps({"action": "archived", "id": args.message_id}))


def cmd_mark_read(args):
    get_access_token()
    gmail_request("POST", f"/messages/{args.message_id}/modify", {
        "removeLabelIds": ["UNREAD"],
    })
    print(json.dumps({"action": "marked_read", "id": args.message_id}))


def cmd_batch_archive(args):
    """Archive multiple messages matching a query."""
    get_access_token()
    query = args.query
    encoded_q = urllib.parse.quote(query)
    result = gmail_request("GET", f"/messages?q={encoded_q}&maxResults={args.max}")
    messages = result.get("messages", [])

    archived = []
    for msg_ref in messages:
        gmail_request("POST", f"/messages/{msg_ref['id']}/modify", {
            "removeLabelIds": ["INBOX"],
        })
        archived.append(msg_ref["id"])

    print(json.dumps({"action": "batch_archived", "count": len(archived), "ids": archived}))


def main():
    global _active_account
    available = list(_ACCOUNT_DIRS.keys()) if _ACCOUNT_DIRS else None
    parser = argparse.ArgumentParser(description=f"Gmail inbox management (agent: {_AGENT_NAME})")
    parser.add_argument("--account", "-a", default=None,
                        choices=available,
                        help=f"Gmail account alias (available: {available})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list")
    p_list.add_argument("--max", type=int, default=20)
    p_list.add_argument("--query", "-q", default=None)

    p_read = sub.add_parser("read")
    p_read.add_argument("message_id")

    p_summary = sub.add_parser("summary")
    p_summary.add_argument("--max", type=int, default=50)

    p_archive = sub.add_parser("archive")
    p_archive.add_argument("message_id")

    p_mark = sub.add_parser("mark-read")
    p_mark.add_argument("message_id")

    p_batch = sub.add_parser("batch-archive")
    p_batch.add_argument("--query", "-q", required=True)
    p_batch.add_argument("--max", type=int, default=50)

    args = parser.parse_args()
    _active_account = args.account
    {
        "list": cmd_list,
        "read": cmd_read,
        "summary": cmd_summary,
        "archive": cmd_archive,
        "mark-read": cmd_mark_read,
        "batch-archive": cmd_batch_archive,
    }[args.command](args)


if __name__ == "__main__":
    main()
