#!/usr/bin/env python3
"""
Vault client — reads credentials from the admin-control credential vault.
Replaces secrets.json for all scripts.

Usage:
    from vault_client import get_credential, get_credentials

    # Single credential
    api_key = get_credential("bugherd", "api_key")

    # With metadata
    cred = get_credential("toggl", "api_token", with_metadata=True)
    # Returns {"value": "...", "metadata": {"workspace_id": "..."}}

    # All credentials for a service
    wave_creds = get_credentials("wave")
    # Returns {"access_token": "...", "password": "...", "totp_secret": "..."}

Environment:
    AGENT_NAME — which agent's credentials to read (default: "derek")
    SUPABASE_SERVICE_KEY — required for auth
"""

import json
import os
import urllib.request

SUPABASE_URL = "https://YOUR_SUPABASE_PROJECT_ID.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
AGENT_NAME = os.environ.get("AGENT_NAME", "derek")

# Cache to avoid repeated API calls within a single script run
_cache = {}


def _ensure_key():
    if not SUPABASE_SERVICE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_KEY env var required for vault access")


def get_credential(service, key, agent=None, with_metadata=False):
    """Get a single credential value from the vault."""
    _ensure_key()
    target = agent or AGENT_NAME
    cache_key = f"{target}/{service}/{key}"
    if cache_key in _cache:
        entry = _cache[cache_key]
        return entry if with_metadata else entry["value"]

    url = (
        f"{SUPABASE_URL}/rest/v1/agent_credentials"
        f"?agent_name=eq.{target}&service=eq.{service}&credential_key=eq.{key}"
        f"&select=credential_value,metadata"
    )
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    })
    resp = urllib.request.urlopen(req)
    rows = json.loads(resp.read())
    if not rows:
        return None
    entry = {"value": rows[0]["credential_value"], "metadata": rows[0].get("metadata", {})}
    _cache[cache_key] = entry
    return entry if with_metadata else entry["value"]


def get_credentials(service, agent=None):
    """Get all credentials for a service as {key: value} dict."""
    _ensure_key()
    target = agent or AGENT_NAME
    url = (
        f"{SUPABASE_URL}/rest/v1/agent_credentials"
        f"?agent_name=eq.{target}&service=eq.{service}"
        f"&select=credential_key,credential_value,metadata"
    )
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    })
    resp = urllib.request.urlopen(req)
    rows = json.loads(resp.read())
    result = {}
    for row in rows:
        result[row["credential_key"]] = row["credential_value"]
        _cache[f"{target}/{service}/{row['credential_key']}"] = {
            "value": row["credential_value"],
            "metadata": row.get("metadata", {}),
        }
    return result


# Backward compat: load_secrets() returns a dict shaped like the old secrets.json
def load_secrets(agent=None):
    """Load all credentials for an agent, flattened like secrets.json."""
    _ensure_key()
    target = agent or AGENT_NAME
    url = (
        f"{SUPABASE_URL}/rest/v1/agent_credentials"
        f"?agent_name=eq.{target}"
        f"&select=service,credential_key,credential_value,metadata"
    )
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    })
    resp = urllib.request.urlopen(req)
    rows = json.loads(resp.read())
    result = {}
    for row in rows:
        flat_key = f"{row['service']}_{row['credential_key']}"
        result[flat_key] = row["credential_value"]
        # Also merge metadata values
        for mk, mv in (row.get("metadata") or {}).items():
            result[f"{row['service']}_{mk}"] = mv
    return result


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 3:
        print("Usage: vault_client.py <service> <key> [agent]")
        sys.exit(1)
    service, key = sys.argv[1], sys.argv[2]
    agent = sys.argv[3] if len(sys.argv) > 3 else None
    val = get_credential(service, key, agent=agent, with_metadata=True)
    if val:
        print(json.dumps(val, indent=2))
    else:
        print(f"Not found: {agent or AGENT_NAME}/{service}/{key}")
        sys.exit(1)
