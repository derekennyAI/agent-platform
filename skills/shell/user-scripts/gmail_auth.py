#!/usr/bin/env python3
"""Shared Gmail OAuth token management. Reads from credential vault, auto-refreshes.

On-disk tokens delegate to ~/derek/skills/_lib/google_auth.py for refresh-at-use
semantics (concurrency-safe, atomic write, persists `expires_at`). The vault
fallback path stays as legacy for the rare case where an agent has a vault
token but no disk file.
"""
import json
import os
import sys
import time
import urllib.request
import urllib.parse
from pathlib import Path

# Vault integration
sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import get_credential

# Refresh-at-use helper
sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/_lib")
from google_auth import get_token as _disk_get_token, GoogleAuthError

# Agent identity — determines whose credentials are loaded
_AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
_WORKSPACE = Path(f"/Users/YOUR_MAC_USERNAME/{_AGENT_NAME}")
_CONFIG_DIR = _WORKSPACE / ".config" / _AGENT_NAME
_ACCOUNTS_DIR = _CONFIG_DIR / "accounts"

def _find_token_path():
    """Find the best token file for this agent."""
    if _ACCOUNTS_DIR.exists():
        for d in sorted(_ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and (d / "google-token.json").exists():
                return d / "google-token.json"
    root = _CONFIG_DIR / "google-token.json"
    if root.exists():
        return root
    return None

def _find_creds_path():
    """Find the best credentials file for this agent."""
    if _ACCOUNTS_DIR.exists():
        for d in sorted(_ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and (d / "google-credentials.json").exists():
                return d / "google-credentials.json"
    root = _CONFIG_DIR / "google-credentials.json"
    if root.exists():
        return root
    return None

_TOKEN_PATH = _find_token_path()
_CREDS_PATH = _find_creds_path()

_EXPIRY_BUFFER_SECS = 300


def _load_token():
    """Load Gmail OAuth token — vault first (agent-scoped), disk fallback."""
    try:
        raw = get_credential("gmail", "oauth_token", agent=_AGENT_NAME)
        if raw:
            return json.loads(raw), "vault"
    except Exception:
        pass
    if _TOKEN_PATH and _TOKEN_PATH.exists():
        return json.loads(_TOKEN_PATH.read_text()), "disk"
    raise RuntimeError(f"No Gmail OAuth token found for agent '{_AGENT_NAME}'")


def _load_creds():
    """Load Gmail OAuth client credentials — vault first (agent-scoped), disk fallback."""
    try:
        raw = get_credential("gmail", "oauth_credentials", agent=_AGENT_NAME)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    if _CREDS_PATH and _CREDS_PATH.exists():
        return json.loads(_CREDS_PATH.read_text())
    raise RuntimeError(f"No Gmail OAuth credentials found for agent '{_AGENT_NAME}'")


def _save_token(token_data, source):
    """Persist refreshed token back to vault (and disk if that was the source)."""
    try:
        from vault_client import get_credential as _gc
        # Store updated token in vault via direct Supabase call
        SUPABASE_URL = "https://YOUR_SUPABASE_PROJECT_ID.supabase.co"
        svc_key = os.environ.get("SUPABASE_SERVICE_KEY", "")
        if svc_key:
            data = json.dumps({
                "agent_name": _AGENT_NAME,
                "service": "gmail",
                "credential_key": "oauth_token",
                "credential_value": json.dumps(token_data),
                "metadata": {"agent": _AGENT_NAME, "type": "oauth_token"},
            }).encode()
            req = urllib.request.Request(
                f"{SUPABASE_URL}/rest/v1/agent_credentials?on_conflict=agent_name,service,credential_key",
                data=data,
                headers={
                    "apikey": svc_key,
                    "Authorization": f"Bearer {svc_key}",
                    "Content-Type": "application/json",
                    "Prefer": "return=minimal,resolution=merge-duplicates",
                },
                method="POST",
            )
            urllib.request.urlopen(req)
    except Exception:
        pass
    # Also write to disk as fallback
    if _TOKEN_PATH and (source == "disk" or _TOKEN_PATH.exists()):
        try:
            _TOKEN_PATH.write_text(json.dumps(token_data, indent=2))
        except Exception:
            pass


def get_access_token(max_retries=3):
    """Get a valid access token, auto-refreshing if needed.

    Disk path delegates to google_auth.get_token() — that helper handles
    expiry checks, refresh, atomic write, concurrency lock. After a successful
    disk refresh, we mirror the new token into the vault for cross-process
    visibility (backwards-compat with downstream consumers that read the
    vault directly).

    If no disk file exists, falls back to legacy vault-only refresh path.
    """
    # Common path: token file on disk → new helper.
    # Only mirror to vault when the helper actually refreshed (i.e. the
    # access_token on disk changed). The vault write is a Supabase POST;
    # doing it on every call adds 200–500ms of network latency per Gmail
    # API request even when the cached token was reused.
    if _TOKEN_PATH and _TOKEN_PATH.exists():
        try:
            try:
                pre_refresh_token = json.loads(_TOKEN_PATH.read_text()).get("access_token")
            except Exception:
                pre_refresh_token = None
            access = _disk_get_token(_TOKEN_PATH)
            if access != pre_refresh_token:
                try:
                    latest = json.loads(_TOKEN_PATH.read_text())
                    _save_token(latest, "disk")
                except Exception:
                    pass
            return access
        except GoogleAuthError:
            # Fall through to legacy vault-only path on any helper failure.
            pass

    # Legacy path: vault-only token (rare; no disk file present)
    token, source = _load_token()

    expires_at = token.get("expires_at", 0)
    if expires_at > time.time() + _EXPIRY_BUFFER_SECS:
        return token["access_token"]

    try:
        _test_token(token["access_token"])
        return token["access_token"]
    except Exception:
        pass

    for attempt in range(max_retries):
        try:
            new_access, expires_in = _refresh_token(token)
            token["access_token"] = new_access
            token["expires_at"] = int(time.time()) + expires_in
            _save_token(token, source)
            _test_token(new_access)
            return new_access
        except Exception as e:
            if attempt == max_retries - 1:
                raise RuntimeError(f"Token refresh failed after {max_retries} attempts: {e}")

    raise RuntimeError("Token refresh exhausted")


def _test_token(access_token):
    """Quick test that the token works."""
    url = "https://gmail.googleapis.com/gmail/v1/users/me/profile"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {access_token}"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read())


def _refresh_token(token_data):
    """Refresh the access token using the refresh token."""
    creds = _load_creds()
    installed = creds.get("installed", creds.get("web", {}))

    data = urllib.parse.urlencode({
        "client_id": installed["client_id"],
        "client_secret": installed["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token"
    }).encode()

    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data, method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read())

    return result["access_token"], result.get("expires_in", 3600)
