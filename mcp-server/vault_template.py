#!/usr/bin/env python3
"""
Vault-aware credential template for agent skills.

Import this in any skill that needs credentials. It automatically scopes
credential access to the calling agent via AGENT_NAME env var.

Usage:
    from vault_template import AGENT_NAME, get_cred, get_creds, WORKSPACE, CONFIG_DIR

    # Single credential
    api_key = get_cred("toggl", "api_token")

    # All credentials for a service
    wave_creds = get_creds("wave")  # returns {"access_token": "...", ...}

    # Agent-scoped paths
    my_data = WORKSPACE / "data" / "output.json"

Environment:
    AGENT_NAME — which agent's credentials to use (required)
    SUPABASE_SERVICE_KEY — required for vault access
"""

import os
import sys
from pathlib import Path

# --- Agent identity ---
AGENT_NAME = os.environ.get("AGENT_NAME", "")
if not AGENT_NAME:
    raise RuntimeError(
        "AGENT_NAME env var is required. "
        "This script must be run via the MCP run_skill tool or with AGENT_NAME set."
    )

# --- Workspace paths (scoped to this agent) ---
WORKSPACE = Path.home() / AGENT_NAME
CONFIG_DIR = WORKSPACE / ".config" / AGENT_NAME
ACCOUNTS_DIR = CONFIG_DIR / "accounts"

# --- Vault client ---
sys.path.insert(0, str(Path(__file__).parent))
from vault_client import get_credential, get_credentials


def get_cred(service, key, with_metadata=False):
    """Get a single credential from the vault, scoped to this agent."""
    return get_credential(service, key, agent=AGENT_NAME, with_metadata=with_metadata)


def get_creds(service):
    """Get all credentials for a service, scoped to this agent."""
    return get_credentials(service, agent=AGENT_NAME)


# --- Convenience: discover connected accounts ---
def list_connected_accounts(provider="google"):
    """List connected accounts for this agent by scanning the accounts directory."""
    accounts = []
    if ACCOUNTS_DIR.exists():
        for d in sorted(ACCOUNTS_DIR.iterdir()):
            if not d.is_dir():
                continue
            token_file = {
                "google": "google-token.json",
                "microsoft": "microsoft-token.json",
                "icloud": "caldav-config.json",
            }.get(provider, f"{provider}-token.json")
            if (d / token_file).exists():
                email = d.name.replace("_at_", "@").replace("_", ".")
                accounts.append({"email": email, "dir": d, "dir_name": d.name})
    return accounts
