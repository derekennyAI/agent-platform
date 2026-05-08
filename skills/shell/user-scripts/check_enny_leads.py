#!/usr/bin/env python3
"""Check Gmail for enny.ai contact form submissions and process leads.

Looks for unread emails from noreply@glizzytracker.app (Enny AI website contact form).
Extracts lead info, researches company/email, and sends a summary email to Farlen.
"""

import base64
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import get_credential as _vault_get

# --- Config ---
SENDER_FILTER = "from:noreply@glizzytracker.app is:unread"
NOTIFY_TO = "YOUR_ADMIN_EMAIL"

_AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
WORKSPACE = Path(f"/Users/YOUR_MAC_USERNAME/{_AGENT_NAME}")
_CONFIG_DIR = WORKSPACE / ".config" / _AGENT_NAME
_ACCOUNTS_DIR = _CONFIG_DIR / "accounts"

def _find_token_path():
    """Find best Google token for this agent."""
    if _ACCOUNTS_DIR.exists():
        for d in sorted(_ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and (d / "google-token.json").exists():
                return d / "google-token.json"
    root = _CONFIG_DIR / "google-token.json"
    return root if root.exists() else None

TOKEN_PATH = _find_token_path()
STATE_PATH = WORKSPACE / "memory/enny-leads-state.json"


def get_access_token():
    """Get access token with auto-refresh (3 retries). Vault first, then gmail_auth, then disk."""
    # Try vault-backed gmail_auth first
    sys.path.insert(0, str(Path("/Users/YOUR_MAC_USERNAME/derek/skills/shell/user-scripts")))
    try:
        from gmail_auth import get_access_token as _get
        return _get(max_retries=3)
    except ImportError:
        pass
    # Fallback: vault direct read (agent-scoped)
    try:
        raw = _vault_get("gmail", "oauth_token", agent=_AGENT_NAME)
        if raw:
            token = json.loads(raw)
            return token["access_token"]
    except Exception:
        pass
    # Fallback: disk
    if not TOKEN_PATH or not TOKEN_PATH.exists():
        raise RuntimeError(f"No Google token found for agent '{_AGENT_NAME}'")
    token = json.loads(TOKEN_PATH.read_text())
    return token["access_token"]


def gmail_get(path, params=None):
    token = get_access_token()
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def gmail_post(path, data=None):
    token = get_access_token()
    url = f"https://gmail.googleapis.com/gmail/v1/users/me/{path}"
    body = json.dumps(data or {}).encode()
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def parse_contact_body(text):
    """Parse the contact form email body into structured fields."""
    fields = {}
    lines = text.strip().split("\n")
    current_key = None
    message_lines = []
    in_message = False

    for line in lines:
        if in_message:
            message_lines.append(line)
            continue
        match = re.match(r"^(Name|Email|Company|Phone|Message):\s*(.*)", line, re.IGNORECASE)
        if match:
            key = match.group(1).lower()
            val = match.group(2).strip()
            if key == "message":
                in_message = True
                if val:
                    message_lines.append(val)
            else:
                fields[key] = val
        else:
            # might be continuation of previous field
            pass

    fields["message"] = "\n".join(message_lines).strip()
    return fields


def research_lead(email, company):
    """Try to gather info on the lead using public web data."""
    findings = []

    # Extract domain from email
    domain = ""
    if email and "@" in email:
        domain = email.split("@")[1].lower()

    # Skip research for obviously fake/test emails
    test_indicators = ["test", "example.com", "fake", "asdf", "123"]
    if any(t in email.lower() for t in test_indicators):
        findings.append("⚠️ Likely a test/fake submission — email contains test indicators.")
        return findings, "test"

    # Check if it's a free email provider
    free_providers = ["gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "aol.com",
                      "icloud.com", "protonmail.com", "mail.com", "zoho.com"]
    if domain in free_providers:
        findings.append(f"📧 Personal email ({domain}) — no company domain to verify.")
    elif domain:
        findings.append(f"🏢 Company domain: {domain}")
        # Try to fetch the company website
        try:
            req = urllib.request.Request(
                f"https://{domain}",
                headers={"User-Agent": "Mozilla/5.0"},
                method="GET"
            )
            req.timeout = 5
            with urllib.request.urlopen(req, timeout=5) as resp:
                html = resp.read().decode(errors="replace")[:5000]
                title_match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
                if title_match:
                    findings.append(f"🌐 Website title: {title_match.group(1).strip()}")
                desc_match = re.search(r'<meta[^>]*name=["\']description["\'][^>]*content=["\'](.*?)["\']', html, re.IGNORECASE)
                if desc_match:
                    findings.append(f"📝 Description: {desc_match.group(1).strip()[:200]}")
                findings.append(f"✅ Website is live at https://{domain}")
        except Exception:
            findings.append(f"❌ Website https://{domain} is not reachable or doesn't exist")

    if company and company.lower() not in ["n/a", "none", "", "test", "test co"]:
        findings.append(f"🏷️ Self-reported company: {company}")

    if not findings:
        findings.append("🤷 Not enough info to research this lead.")

    # Classify: real, suspicious, test
    classification = "unknown"
    if any("test" in f.lower() or "fake" in f.lower() for f in findings):
        classification = "test"
    elif any("not reachable" in f for f in findings):
        classification = "suspicious"
    elif any("Website is live" in f for f in findings):
        classification = "legit"

    return findings, classification


def send_notification(lead_fields, research, classification):
    """Send lead notification email to Farlen via the send_email script."""
    name = lead_fields.get("name", "Unknown")
    email = lead_fields.get("email", "Unknown")
    company = lead_fields.get("company", "N/A")
    phone = lead_fields.get("phone", "N/A")
    message = lead_fields.get("message", "N/A")

    emoji = {"legit": "🟢", "suspicious": "🟡", "test": "🔴", "unknown": "⚪"}.get(classification, "⚪")

    body = f"""New contact form submission from enny.ai {emoji}

━━━ Contact Details ━━━
Name: {name}
Email: {email}
Company: {company}
Phone: {phone}

━━━ Their Message ━━━
{message}

━━━ Lead Research {emoji} ({classification.upper()}) ━━━
""" + "\n".join(research) + """

━━━━━━━━━━━━━━━━━━━━━━
Auto-processed by Derek
"""

    subject = f"{emoji} Enny AI Lead: {name}" + (f" ({company})" if company and company != "N/A" else "")

    # Use send_email.py
    import subprocess
    result = subprocess.run(
        ["python3", str(WORKSPACE / "skills/idea-hunter/scripts/send_email.py"),
         "--to", NOTIFY_TO, "--subject", subject, "--body", body],
        capture_output=True, text=True
    )
    if result.returncode == 0:
        print(f"  ✉️  Notification sent to {NOTIFY_TO}")
    else:
        print(f"  ❌ Failed to send email: {result.stderr}")

    return subject


def mark_as_read(msg_id):
    """Remove UNREAD label from a Gmail message."""
    try:
        gmail_post(f"messages/{msg_id}/modify", {"removeLabelIds": ["UNREAD"]})
    except Exception as e:
        print(f"  ⚠️  Couldn't mark as read: {e}")


def load_state():
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text())
    return {"processed": []}


def save_state(state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def main():
    state = load_state()
    processed_ids = set(state.get("processed", []))

    # Search for unread contact form emails
    try:
        results = gmail_get("messages", {"q": SENDER_FILTER, "maxResults": 10})
    except urllib.error.HTTPError as e:
        if e.code == 401:
            print("❌ Gmail token expired — need refresh")
            sys.exit(1)
        raise

    messages = results.get("messages", [])
    if not messages:
        print(json.dumps({"leads": 0, "message": "No new contact form submissions"}))
        return

    new_leads = 0
    for msg_ref in messages:
        msg_id = msg_ref["id"]
        if msg_id in processed_ids:
            continue

        print(f"Processing message {msg_id}...")

        # Get full message
        msg = gmail_get(f"messages/{msg_id}", {"format": "full"})
        payload = msg.get("payload", {})

        # Extract body
        text = ""
        body_data = payload.get("body", {}).get("data")
        if body_data:
            text = base64.urlsafe_b64decode(body_data).decode()
        for part in payload.get("parts", []):
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                text = base64.urlsafe_b64decode(part["body"]["data"]).decode()
                break

        if not text:
            print(f"  ⚠️  No text body found, skipping")
            continue

        # Parse contact fields
        fields = parse_contact_body(text)
        print(f"  Lead: {fields.get('name', '?')} / {fields.get('email', '?')} / {fields.get('company', '?')}")

        # Research
        research, classification = research_lead(
            fields.get("email", ""),
            fields.get("company", "")
        )
        print(f"  Classification: {classification}")

        # Send notification
        send_notification(fields, research, classification)

        # Mark as read
        mark_as_read(msg_id)

        # Track
        processed_ids.add(msg_id)
        new_leads += 1

    state["processed"] = list(processed_ids)[-200:]  # keep last 200
    state["last_check"] = datetime.now().isoformat()
    save_state(state)

    print(json.dumps({"leads": new_leads, "message": f"Processed {new_leads} new lead(s)"}))


if __name__ == "__main__":
    main()
