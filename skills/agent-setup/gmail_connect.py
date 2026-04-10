#!/usr/bin/env python3
"""Gmail self-serve connection flow for agent users.

Generates an OAuth URL for a user to authorize Gmail access,
then exchanges the auth code for tokens and saves them to
the agent's config directory.

Usage:
  # Step 1: Generate auth URL for user
  python3 gmail_connect.py url --agent vera

  # Step 2: After user pastes back the redirect URL or code
  python3 gmail_connect.py exchange --agent vera --code "4/0AeaYSH..."

  # Step 3: Verify it works
  python3 gmail_connect.py verify --agent vera
"""

import argparse
import json
import sys
import urllib.parse
import urllib.request
from pathlib import Path

# Google OAuth app credentials (enny.ai "derek-agent" project)
CLIENT_ID = "975899325505-24kqu935trcmrmbjc5mp4ok3dbh8vjr8.apps.googleusercontent.com"
CLIENT_SECRET = "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"
REDIRECT_URI_LOCAL = "http://localhost"
REDIRECT_URI_FUNNEL = "https://dereks-macbook-pro.tailf80e44.ts.net/oauth/callback"
REDIRECT_URI = REDIRECT_URI_FUNNEL  # Default to Funnel for better UX
SCOPES = "https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/gmail.send https://www.googleapis.com/auth/calendar"

AGENTS_REGISTRY = Path.home() / "derek" / "skills" / "agent-setup" / "agents.json"


def get_agent_config_dir(agent_name):
    """Get or create the config directory for an agent."""
    agents = json.loads(AGENTS_REGISTRY.read_text()).get("agents", {})
    agent = agents.get(agent_name)
    if not agent:
        print(f"Error: Agent '{agent_name}' not found in registry", file=sys.stderr)
        sys.exit(1)
    ws = Path(agent["workspace"])
    config_dir = ws / ".config" / agent_name
    config_dir.mkdir(parents=True, exist_ok=True)
    return config_dir


def generate_url(agent_name):
    """Generate the Gmail OAuth authorization URL."""
    config_dir = get_agent_config_dir(agent_name)

    # Save credentials file (same app for all agents)
    creds_file = config_dir / "google-credentials.json"
    creds_data = {
        "installed": {
            "client_id": CLIENT_ID,
            "project_id": "derek-agent",
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "client_secret": CLIENT_SECRET,
            "redirect_uris": [REDIRECT_URI],
        }
    }
    creds_file.write_text(json.dumps(creds_data))

    # Build auth URL
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = "https://accounts.google.com/o/oauth2/auth?" + urllib.parse.urlencode(params)

    print(json.dumps({
        "status": "ok",
        "auth_url": url,
        "agent": agent_name,
        "config_dir": str(config_dir),
        "instructions": (
            "Send this URL to the user on Telegram. They should:\n"
            "1. Open the link in their browser\n"
            "2. Sign into their Google account\n"
            "3. Click 'Allow' to grant Gmail access\n"
            "4. The browser will try to redirect to localhost and fail — that's expected\n"
            "5. Copy the FULL URL from their browser's address bar\n"
            "6. Paste it back to you on Telegram\n"
            "Then run: gmail_connect.py exchange --agent NAME --code 'THE_URL_OR_CODE'"
        ),
    }))


def exchange_code(agent_name, code_or_url):
    """Exchange an authorization code for access/refresh tokens."""
    config_dir = get_agent_config_dir(agent_name)

    # Extract code from URL if user pasted the full redirect URL
    code = code_or_url.strip()
    if "code=" in code:
        parsed = urllib.parse.urlparse(code)
        params = urllib.parse.parse_qs(parsed.query)
        if "code" in params:
            code = params["code"][0]

    # Exchange code for tokens
    data = urllib.parse.urlencode({
        "code": code,
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "redirect_uri": REDIRECT_URI,
        "grant_type": "authorization_code",
    }).encode()

    req = urllib.request.Request(
        "https://oauth2.googleapis.com/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req) as resp:
            token_data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        print(json.dumps({
            "status": "error",
            "error": f"Token exchange failed: {e.code}",
            "detail": error_body,
        }))
        sys.exit(1)

    # Save token
    token_file = config_dir / "google-token.json"
    token_file.write_text(json.dumps(token_data, indent=2))

    # Redact sensitive fields for output
    safe_output = {k: ("..." if k in ("access_token", "refresh_token") else v)
                   for k, v in token_data.items()}

    print(json.dumps({
        "status": "ok",
        "agent": agent_name,
        "token_file": str(token_file),
        "token_info": safe_output,
        "message": "Gmail connected successfully! The agent can now read and send email.",
    }))


def verify(agent_name):
    """Verify Gmail connection works by fetching the user's email address."""
    config_dir = get_agent_config_dir(agent_name)
    token_file = config_dir / "google-token.json"

    if not token_file.exists():
        print(json.dumps({"status": "error", "error": "No token file found. Run 'exchange' first."}))
        sys.exit(1)

    token_data = json.loads(token_file.read_text())
    access_token = token_data.get("access_token")

    # Try to get user profile
    req = urllib.request.Request(
        "https://www.googleapis.com/gmail/v1/users/me/profile",
        headers={"Authorization": f"Bearer {access_token}"},
    )

    try:
        with urllib.request.urlopen(req) as resp:
            profile = json.loads(resp.read())
            print(json.dumps({
                "status": "ok",
                "agent": agent_name,
                "email": profile.get("emailAddress"),
                "messages_total": profile.get("messagesTotal"),
                "message": f"Gmail verified — connected to {profile.get('emailAddress')}",
            }))
    except urllib.error.HTTPError as e:
        if e.code == 401:
            # Token expired, try refresh
            refresh_token = token_data.get("refresh_token")
            if refresh_token:
                refresh_data = urllib.parse.urlencode({
                    "refresh_token": refresh_token,
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "refresh_token",
                }).encode()
                refresh_req = urllib.request.Request(
                    "https://oauth2.googleapis.com/token",
                    data=refresh_data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                try:
                    with urllib.request.urlopen(refresh_req) as rresp:
                        new_tokens = json.loads(rresp.read())
                        token_data["access_token"] = new_tokens["access_token"]
                        if "refresh_token" in new_tokens:
                            token_data["refresh_token"] = new_tokens["refresh_token"]
                        token_file.write_text(json.dumps(token_data, indent=2))
                        # Retry with new token
                        req2 = urllib.request.Request(
                            "https://www.googleapis.com/gmail/v1/users/me/profile",
                            headers={"Authorization": f"Bearer {token_data['access_token']}"},
                        )
                        with urllib.request.urlopen(req2) as resp2:
                            profile = json.loads(resp2.read())
                            print(json.dumps({
                                "status": "ok",
                                "agent": agent_name,
                                "email": profile.get("emailAddress"),
                                "message": f"Token refreshed. Gmail connected to {profile.get('emailAddress')}",
                            }))
                            return
                except Exception as re:
                    print(json.dumps({"status": "error", "error": f"Token refresh failed: {re}"}))
                    sys.exit(1)
            else:
                print(json.dumps({"status": "error", "error": "Token expired and no refresh token. Re-run 'url' and 'exchange'."}))
                sys.exit(1)
        else:
            print(json.dumps({"status": "error", "error": f"Gmail API error: {e.code} {e.read().decode()}"}))
            sys.exit(1)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Gmail self-serve connection for agent users")
    sub = parser.add_subparsers(dest="command")

    url_cmd = sub.add_parser("url", help="Generate OAuth URL")
    url_cmd.add_argument("--agent", required=True)

    ex_cmd = sub.add_parser("exchange", help="Exchange auth code for tokens")
    ex_cmd.add_argument("--agent", required=True)
    ex_cmd.add_argument("--code", required=True)

    ver_cmd = sub.add_parser("verify", help="Verify Gmail connection")
    ver_cmd.add_argument("--agent", required=True)

    args = parser.parse_args()

    if args.command == "url":
        generate_url(args.agent)
    elif args.command == "exchange":
        exchange_code(args.agent, args.code)
    elif args.command == "verify":
        verify(args.agent)
    else:
        parser.print_help()
