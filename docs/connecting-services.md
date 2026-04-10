# Connecting Services

How to connect external services (Gmail, Calendar, Microsoft, etc.) to your agents.

## Gmail / Google Workspace

### Prerequisites
1. A Google Cloud project with OAuth 2.0 credentials
2. Gmail API enabled
3. OAuth consent screen configured (can be "internal" for Workspace, or "external" with test users)

### Setup

```bash
python3 skills/agent-setup/gmail_connect.py url --agent <agent-name>
```

This generates an OAuth URL. The user visits it, authorizes, and gets a code. Then:

```bash
python3 skills/agent-setup/gmail_connect.py exchange --agent <agent-name> --code <code>
```

Tokens are saved to `~/agent-name/.config/agent-name/accounts/<email>/google-token.json`.

### Multiple Accounts

Each agent can connect multiple Google accounts. The account directory is named by email (e.g., `user_at_gmail_com/`). Scripts discover accounts dynamically by scanning the accounts directory.

### Google Cloud Project Setup

1. Go to [Google Cloud Console](https://console.cloud.google.com/)
2. Create a project (or use existing)
3. Enable APIs: Gmail API, Google Calendar API
4. OAuth consent screen: Add scopes for Gmail and Calendar
5. Create OAuth 2.0 Client ID (type: Desktop app or Web app)
6. Download credentials JSON → save as `google-credentials.json` in agent's config

### Vault Pattern

Scripts use the vault-with-disk-fallback pattern:

```python
# Try vault first
token = get_credential("gmail", "oauth_token", agent=AGENT_NAME)

# Fall back to disk
if not token:
    token_path = find_disk_token()  # Scans accounts directory
    if token_path:
        token = json.loads(token_path.read_text())
```

## Microsoft 365

### Prerequisites
1. Azure AD app registration
2. Microsoft Graph API permissions (Mail.ReadWrite, Calendars.Read, User.Read)

### Token Storage

Microsoft tokens go to `~/agent-name/.config/agent-name/accounts/<email>/microsoft-token.json`.

Token refresh uses the standard OAuth2 refresh_token grant against `https://login.microsoftonline.com/common/oauth2/v2.0/token`.

## iCloud (CalDAV)

For iCloud calendar access, use app-specific passwords:

1. Go to appleid.apple.com → Sign-In & Security → App-Specific Passwords
2. Generate a password for the agent
3. Save config to `~/agent-name/.config/agent-name/accounts/<email>/caldav-config.json`:

```json
{
  "url": "https://caldav.icloud.com",
  "username": "user@icloud.com",
  "password": "app-specific-password"
}
```

## Credential Security

All credentials follow these rules:

1. **Vault first**: Store in Supabase via `store_credential` MCP tool
2. **Disk fallback**: Local files in agent-scoped config directory
3. **Never hardcode**: Scripts use `AGENT_NAME` env var to find the right credentials
4. **Workspace isolation**: Each agent can only access its own `~/agent-name/.config/agent-name/` directory
5. **Validation**: The `skill_validator.py` scanner catches hardcoded paths and cross-workspace access

## Adding New Services

When building a skill that needs a new service:

```python
import os, sys
sys.path.insert(0, "path/to/mcp-server")
from vault_template import AGENT_NAME, WORKSPACE, get_cred, get_creds

# Get credentials from vault
api_key = get_cred("service_name", "api_key")

# Or get all credentials for a service
all_creds = get_creds("service_name")
```

Store credentials via the MCP tool:
```
store_credential(service="service_name", key="api_key", value="sk-...", agent="agent-name")
```
