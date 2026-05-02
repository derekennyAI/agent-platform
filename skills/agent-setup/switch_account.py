#!/usr/bin/env python3
"""
Switch an agent from shared API key to their own Claude Pro/Max OAuth token.

Usage:
  # Step 1: Generate authorize URL
  python3 switch_account.py auth-url --agent test

  # Step 2: Exchange code for token (after user pastes code)
  python3 switch_account.py exchange --agent test --code "ABC123" --state "xyz"

  # Step 3: Check usage for an agent
  python3 switch_account.py usage --agent test

  # Step 4: Check usage for all agents
  python3 switch_account.py usage
"""

import argparse
import hashlib
import base64
import os
import json
import sys
import urllib.request
import urllib.parse
import subprocess
import plistlib
from pathlib import Path

EDGE_FUNCTION_BASE = "os.environ.get("SUPABASE_URL", "")/functions/v1"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "user:inference"  # inference-only scope — sufficient for agent operation
AGENTS_JSON = Path(__file__).parent / "agents.json"


def generate_pkce():
    verifier = base64.urlsafe_b64encode(os.urandom(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = base64.urlsafe_b64encode(os.urandom(32)).rstrip(b"=").decode()
    return verifier, challenge, state


def cmd_auth_url(args):
    verifier, challenge, state = generate_pkce()

    # Save PKCE for later exchange
    pkce_file = f"/tmp/pkce_{args.agent}_switch.json"
    with open(pkce_file, "w") as f:
        json.dump({"verifier": verifier, "challenge": challenge, "state": state}, f, indent=2)

    scopes = SCOPES

    params = urllib.parse.urlencode({
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": scopes,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })
    url = f"https://claude.ai/oauth/authorize?{params}"

    print(json.dumps({
        "authorize_url": url,
        "state": state,
        "pkce_file": pkce_file,
    }, indent=2))


def cmd_exchange(args):
    # Load PKCE
    pkce_file = f"/tmp/pkce_{args.agent}_switch.json"
    if not os.path.exists(pkce_file):
        print(json.dumps({"error": f"No PKCE file found at {pkce_file}. Run auth-url first."}))
        sys.exit(1)

    with open(pkce_file) as f:
        pkce = json.load(f)

    # Exchange via edge function
    payload = json.dumps({
        "code": args.code,
        "code_verifier": pkce["verifier"],
        "state": args.state or pkce["state"],
    }).encode()

    req = urllib.request.Request(
        f"{EDGE_FUNCTION_BASE}/oauth-exchange",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        result = json.loads(e.read().decode())
        print(json.dumps({"error": "Exchange failed", "details": result}, indent=2))
        sys.exit(1)

    # Extract token from response
    token_data = result.get("response", result)
    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    if not access_token:
        print(json.dumps({"error": "No access_token in response", "response": result}, indent=2))
        sys.exit(1)

    # Load agent info
    with open(AGENTS_JSON) as f:
        agents = json.load(f)["agents"]

    if args.agent not in agents:
        print(json.dumps({"error": f"Agent '{args.agent}' not found in agents.json"}))
        sys.exit(1)

    agent = agents[args.agent]

    # Store tokens in Supabase agent_credentials table via REST API
    import datetime
    supabase_url = os.environ.get("SUPABASE_URL", "")
    svc_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
    if not svc_key:
        # Fallback: read from Management API token for DB query
        svc_key = "YOUR_JWT_TOKEN"

    expires_at = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=token_data.get("expires_in", 28800))).isoformat()
    account_email = token_data.get("account", {}).get("email_address", "unknown")

    cred_records = [
        {"agent_name": args.agent, "service": "claude_oauth", "credential_key": "access_token", "credential_value": access_token, "metadata": {"account_email": account_email, "subscription_type": "pro", "expires_at": expires_at}},
        {"agent_name": args.agent, "service": "claude_oauth", "credential_key": "refresh_token", "credential_value": refresh_token, "metadata": {"account_email": account_email, "subscription_type": "pro"}},
    ]

    for rec in cred_records:
        try:
            cred_req = urllib.request.Request(
                f"{supabase_url}/rest/v1/agent_credentials",
                data=json.dumps(rec).encode(),
                headers={
                    "apikey": svc_key,
                    "Authorization": f"Bearer {svc_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal,resolution=merge-duplicates",
                },
                method="POST",
            )
            urllib.request.urlopen(cred_req, timeout=15)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()
            print(json.dumps({"warning": "DB insert failed but token was obtained", "db_error": err_body, "status": e.code}, indent=2), file=sys.stderr)
            # Continue anyway — token was obtained, plist update is more important

    # Save credentials file (launcher reads this to inject CLAUDE_CODE_OAUTH_TOKEN)
    creds_dir = Path.home() / f".claude-{args.agent}"
    creds_dir.mkdir(exist_ok=True)
    creds_file = creds_dir / ".credentials.json"
    creds_data = {
        "claudeAiOauth": {
            "accessToken": access_token,
            "refreshToken": refresh_token,
            "expiresAt": int((datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=token_data.get("expires_in", 28800))).timestamp() * 1000),
            "scopes": SCOPES.split(),
            "subscriptionType": token_data.get("account", {}).get("subscription_type", "pro"),
            "rateLimitTier": f"default_claude_{token_data.get('account', {}).get('subscription_type', 'pro')}",
        }
    }
    with open(creds_file, "w") as f:
        json.dump(creds_data, f, indent=2)
    os.chmod(creds_file, 0o600)

    # Update plist
    plist_path = Path.home() / "Library" / "LaunchAgents" / f"{agent['daemon_label']}.plist"
    if plist_path.exists():
        with open(plist_path, "rb") as f:
            plist = plistlib.load(f)

        env = plist.get("EnvironmentVariables", {})
        env["CLAUDE_CONFIG_DIR"] = str(creds_dir)
        # Remove API key — launcher uses env -u to strip from tmux server env
        if "ANTHROPIC_API_KEY" in env:
            env.pop("ANTHROPIC_API_KEY")
        # Remove direct OAuth token from plist — launcher reads from .credentials.json
        if "CLAUDE_CODE_OAUTH_TOKEN" in env:
            env.pop("CLAUDE_CODE_OAUTH_TOKEN")
        plist["EnvironmentVariables"] = env

        with open(plist_path, "wb") as f:
            plistlib.dump(plist, f)

    print(json.dumps({
        "success": True,
        "agent": args.agent,
        "account": token_data.get("account", {}).get("email_address"),
        "organization": token_data.get("organization", {}).get("name"),
        "expires_in": token_data.get("expires_in"),
        "plist_updated": str(plist_path),
        "note": "Restart the daemon to apply: launchctl unload/load the plist",
    }, indent=2))

    # Clean up PKCE file
    os.remove(pkce_file)


def cmd_usage(args):
    url = f"{EDGE_FUNCTION_BASE}/usage-check"

    if args.agent:
        payload = json.dumps({"agent": args.agent}).encode()
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
    else:
        req = urllib.request.Request(url, method="GET")

    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
        print(json.dumps(result, indent=2))
    except urllib.error.HTTPError as e:
        print(json.dumps({"error": json.loads(e.read().decode())}, indent=2))
        sys.exit(1)


def _get_supabase_token():
    import sys as _sys; _sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "mcp-server"))
    from vault_client import load_secrets
    return load_secrets()["supabase_access_token"]


def main():
    parser = argparse.ArgumentParser(description="Switch agent Claude account")
    sub = parser.add_subparsers(dest="command")

    auth = sub.add_parser("auth-url", help="Generate OAuth authorize URL")
    auth.add_argument("--agent", required=True)

    exch = sub.add_parser("exchange", help="Exchange auth code for token")
    exch.add_argument("--agent", required=True)
    exch.add_argument("--code", required=True)
    exch.add_argument("--state", default=None)

    usage = sub.add_parser("usage", help="Check agent usage")
    usage.add_argument("--agent", default=None)

    args = parser.parse_args()

    if args.command == "auth-url":
        cmd_auth_url(args)
    elif args.command == "exchange":
        cmd_exchange(args)
    elif args.command == "usage":
        cmd_usage(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
