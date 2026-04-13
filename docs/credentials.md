# Credential Vault

The credential vault is a Supabase-backed, agent-scoped storage system for API keys, OAuth tokens, and other secrets. Every agent can store and retrieve its own credentials. Admin agents can manage credentials for any agent.

---

## Vault Storage

Credentials are stored in the Supabase `agent_credentials` table:

```sql
CREATE TABLE agent_credentials (
  id SERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL,
  service TEXT NOT NULL,
  credential_key TEXT NOT NULL,
  credential_value TEXT NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(agent_name, service, credential_key)
);
```

### Data Model

Each credential is uniquely identified by three fields:

| Field | Example | Purpose |
|-------|---------|---------|
| `agent_name` | `vera` | Which agent owns this credential |
| `service` | `gmail` | The service/provider name |
| `credential_key` | `access_token` | The specific credential within that service |

A single service typically has multiple keys. For example, a Gmail connection stores:

| agent_name | service | credential_key | credential_value |
|------------|---------|----------------|-----------------|
| vera | gmail | access_token | ya29.a0AfB_by... |
| vera | gmail | refresh_token | 1//0eXxYz... |
| vera | gmail | client_id | 123456.apps.googleusercontent.com |
| vera | gmail | client_secret | GOCSPX-... |

The `metadata` JSONB column stores supplementary info like the email address, token expiry time, or workspace ID:

```json
{"email": "vera@example.com", "expires_at": "2026-04-13T00:00:00Z"}
```

### Scoping

Credential access is scoped per agent. The MCP server enforces this by filtering all queries with `agent_name=eq.{AGENT_NAME}`, where `AGENT_NAME` comes from the agent's launchd environment variable. An agent cannot read, write, or delete another agent's credentials unless it is an admin agent (`derek` or `dereklm`).

The `agent_credentials` table has an index on `(agent_name, service)` for fast lookups:

```sql
CREATE INDEX idx_agent_credentials_lookup ON agent_credentials(agent_name, service);
```

---

## MCP Tools

### store_credential

Store or update a credential. Uses upsert -- if the `(agent_name, service, credential_key)` combination already exists, the value is overwritten.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `service` | Yes | Service name (e.g., `gmail`, `toggl`, `notion`) |
| `key` | Yes | Credential key (e.g., `api_key`, `access_token`) |
| `value` | Yes | The credential value |
| `metadata` | No | JSON string of metadata (e.g., `{"email": "...", "expires_at": "..."}`) |
| `agent` | No | Target agent name (admin only -- omit to store for yourself) |

**Example**:

```
Tool: store_credential
  service: "toggl"
  key: "api_token"
  value: "abc123..."
  metadata: "{\"workspace_id\": \"12345\"}"
```

### get_credential

Retrieve a credential value. Returns the value, metadata, and last update timestamp.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `service` | Yes | Service name |
| `key` | Yes | Credential key |
| `agent` | No | Target agent name (admin only) |

**Response**:

```json
{
  "found": true,
  "agent": "vera",
  "service": "toggl",
  "key": "api_token",
  "value": "abc123...",
  "metadata": {"workspace_id": "12345"},
  "updated_at": "2026-04-10T15:30:00Z"
}
```

### list_credentials

List all connected services and credential keys for the calling agent. Shows service names and keys but **never** shows values -- this is a safe discovery tool.

Takes no parameters. Returns:

```json
{
  "agent": "vera",
  "credentials": [
    {"service": "gmail", "key": "access_token", "metadata": {"email": "vera@example.com"}, "updated_at": "..."},
    {"service": "gmail", "key": "refresh_token", "metadata": {"email": "vera@example.com"}, "updated_at": "..."},
    {"service": "toggl", "key": "api_token", "metadata": {"workspace_id": "12345"}, "updated_at": "..."}
  ]
}
```

### revoke_credential

Delete a credential permanently from the vault.

| Parameter | Required | Description |
|-----------|----------|-------------|
| `service` | Yes | Service name |
| `key` | Yes | Credential key |
| `agent` | No | Target agent name (admin only) |

---

## Write-Through Cache

The vault follows the platform's write-through cache pattern. All credential data exists in two places:

1. **Primary**: Supabase `agent_credentials` table (source of truth)
2. **Local cache**: `~/<agent_name>/.state/` directory (JSON files)

**Writes** go to both Supabase and local files simultaneously. **Reads** try Supabase first, then fall back to local cache if Supabase is unreachable.

This means agents can function offline using cached credentials, and automatically sync when connectivity returns. The local cache also provides a safety net against Supabase outages.

### Local state directory

Each agent has a `.state/` directory in its workspace:

```
~/<agent_name>/
└── .state/
    ├── some_key.json
    └── ...
```

State files are managed by the `get_state` / `set_state` / `delete_state` MCP tools (Module 4b in `server.js`). The credential vault's local sync uses the same write-through approach -- the MCP server writes to Supabase first, then syncs to local JSON.

---

## OAuth Flows

### connect_service

The `connect_service` MCP tool is an atomic operation that handles the full service connection in one step:

1. **Store credentials**: Upserts all credential key-value pairs into `agent_credentials`
2. **Create skill**: Creates or updates a skill entry in the `skills` catalog
3. **Grant permission**: Upserts a row in `skill_permissions` for the agent

| Parameter | Required | Description |
|-----------|----------|-------------|
| `service` | Yes | Service name (e.g., `gmail`, `notion`, `microsoft_mail`, `google_drive`) |
| `credentials` | Yes | JSON string of credential key-value pairs (e.g., `{"access_token": "...", "refresh_token": "..."}`) |
| `metadata` | No | JSON string of metadata (e.g., `{"email": "vera@example.com"}`) |
| `agent` | No | Target agent (admin only) |
| `skill_description` | No | Custom user-facing description (overrides default from service mapping) |

**Service-to-skill mapping**: The MCP server maps service names to skill categories and description templates:

| Service | Category | Description Template |
|---------|----------|---------------------|
| `gmail` | communication | Email management -- {email} |
| `google_calendar` | productivity | Calendar management -- {email} |
| `google_drive` | productivity | Google Drive access -- {email} |
| `google_contacts` | communication | Google Contacts -- {email} |
| `google_tasks` | productivity | Google Tasks -- {email} |
| `microsoft_mail` | communication | Outlook email -- {email} |
| `microsoft_calendar` | productivity | Outlook calendar -- {email} |
| `notion` | productivity | Notion workspace access |
| `icloud_calendar` | productivity | iCloud calendar -- {email} |

The skill name is generated from the service and email: `gmail_vera_at_example_com`. If no email is provided, the service name is used directly.

**Example**:

```
Tool: connect_service
  service: "gmail"
  credentials: "{\"access_token\": \"ya29...\", \"refresh_token\": \"1//0e...\", \"client_id\": \"123.apps.googleusercontent.com\", \"client_secret\": \"GOCSPX-...\"}"
  metadata: "{\"email\": \"vera@example.com\"}"
  agent: "vera"
```

This stores 4 credentials, creates a skill `gmail_vera_at_example_com` in the catalog, and grants it to `vera`.

### disconnect_service

Removes a service connection:

1. **Delete credentials**: Removes all credential rows matching `agent_name` + `service`
2. **Revoke permissions**: Soft-revokes all skill permissions where `skill_name` starts with the service name

| Parameter | Required | Description |
|-----------|----------|-------------|
| `service` | Yes | Service name to disconnect |
| `agent` | No | Target agent (admin only) |

### Gmail OAuth flow

For Gmail (and other Google services), the full flow is:

1. **Generate auth URL**: Run `skills/agent-setup/gmail_connect.py url --agent <name>` to get an OAuth authorization URL
2. **User authorizes**: The user clicks the link, signs in with Google, and authorizes the requested scopes (`gmail.modify`, `gmail.send`, `calendar`)
3. **Exchange code**: Run `skills/agent-setup/gmail_connect.py exchange --agent <name> --code "4/0AeaYSH..."` to exchange the auth code for access + refresh tokens
4. **Store in vault**: The script saves tokens to the agent's config directory and (via `connect_service`) to the vault
5. **Verify**: Run `skills/agent-setup/gmail_connect.py verify --agent <name>` to confirm the connection works

The OAuth redirect is handled by `scripts/claude_oauth_server.py`, which:
- Generates PKCE values (code verifier + challenge + state)
- Serves a web page with the auth link and a paste form
- Exchanges the authorization code for tokens via Google's token endpoint
- Saves tokens to the agent's config directory

---

## OAuth Callback Server

**Script**: `scripts/claude_oauth_server.py`

A lightweight HTTP server that handles OAuth callbacks for Claude subscription setup. This is separate from the Gmail OAuth flow -- it handles the Claude Pro/Max OAuth flow for agent billing.

**Usage**:

```bash
python3 scripts/claude_oauth_server.py --agent <agent-name> --port 8285
```

**Flow**:
1. Script generates PKCE values (code verifier, challenge, state) per RFC 7636
2. Serves a web page with the authorization link and a code-paste form
3. User clicks the link, authorizes on claude.com, copies the returned code
4. User pastes the code into the form
5. Server exchanges the code for an access token via `platform.claude.com/v1/oauth/token`
6. Saves the token to `~/.claude-<agent>/.credentials.json`

**Prerequisites**:
- Agent must already exist (created via `create_agent.py`)
- Port must be accessible to the user's browser (local network or via Tailscale)
- User needs a Claude Pro or Max subscription to authorize

---

## Token Refresh

### Single agent refresh

**Script**: `mcp-server/refresh_agent_token.sh`

Refreshes a single agent's Claude OAuth token by calling the Supabase edge function for token exchange.

```bash
mcp-server/refresh_agent_token.sh <agent_name> <daemon_label>
# Example:
mcp-server/refresh_agent_token.sh vera com.vera-agent.daemon
```

**What it does**:
1. Reads the refresh token from `~/.claude-<agent>/.credentials.json`
2. Calls the Supabase edge function (`/functions/v1/oauth-exchange`) with the refresh token
3. Updates the credentials file with the new access token (and new refresh token if rotated)
4. Restarts the agent's daemon via `launchctl unload` + `launchctl load`
5. Alerts the admin via Telegram if the refresh fails

**Error handling**: If no credentials file exists (agent still on Max subscription), the script exits cleanly with a skip message. If the refresh fails, it alerts the admin and exits with code 1. The agent will fall back to the Max subscription on next restart.

### All agents refresh

**Script**: `mcp-server/refresh_all_agents.sh`

Runs `refresh_agent_token.sh` for every registered sub-agent. Designed to run every 6 hours via cron.

```bash
mcp-server/refresh_all_agents.sh
```

Iterates through all known agents and calls `refresh_agent_token.sh` for each. Agents without credentials files (still on Max) are silently skipped. Produces a summary at the end:

```
Summary: 3 refreshed, 1 skipped (Max), 0 failed
```

Uses `infra_lib.sh` for structured logging and Telegram alerts on failures.

---

## Credential Hierarchy

The platform uses a two-tier credential system:

### Tier 1: Platform-level keys (launchd environment variables)

Set in each agent's launchd plist (`~/Library/LaunchAgents/com.<agent>-agent.daemon.plist`) under `EnvironmentVariables`:

| Variable | Purpose |
|----------|---------|
| `ANTHROPIC_API_KEY` | Anthropic API access (fallback for agents not on Pro/Max) |
| `SUPABASE_URL` | Supabase project URL (shared across all agents) |
| `SUPABASE_SERVICE_KEY` | Supabase service role key (full DB access) |
| `GITHUB_TOKEN` | GitHub API access |
| `AGENT_NAME` | The agent's identity (used for vault scoping) |
| `ADMIN_TELEGRAM_CHAT_ID` | Telegram chat ID for admin alerts |

These are available to the MCP server process and all scripts it spawns. They provide platform infrastructure access -- without these, the agent cannot start.

### Tier 2: Agent-specific keys (vault)

Stored in the `agent_credentials` Supabase table, scoped per agent. These include:

- **OAuth tokens**: Gmail, Microsoft 365, iCloud (access_token, refresh_token, client_id, client_secret)
- **Service API keys**: Toggl, Wave, BugHerd, Wise, Resend, Linear, etc.
- **Configuration values**: workspace IDs, project references, account-specific settings

Vault credentials are accessed via the MCP tools (`get_credential`, `store_credential`) or via the Python libraries (`vault_client.py`, `vault_template.py`).

### Why two tiers?

Platform-level keys must exist before the agent can start (they're needed to connect to Supabase and the MCP server). Agent-specific keys are loaded at runtime from the vault. This separation means:

- A new agent can start with just the platform keys in its plist
- Service credentials are added later through OAuth flows or manual `store_credential` calls
- Rotating a service credential doesn't require restarting the daemon
- Each agent's service keys are isolated from other agents

---

## Security

### Python vault libraries

Two Python libraries provide safe credential access for skill scripts:

#### vault_client.py

**Location**: `mcp-server/vault_client.py`

Low-level vault client. Queries the `agent_credentials` table directly via the Supabase REST API.

```python
from vault_client import get_credential, get_credentials, load_secrets

# Get a single credential
api_key = get_credential("toggl", "api_token")

# Get a single credential with metadata
cred = get_credential("toggl", "api_token", with_metadata=True)
# Returns: {"value": "abc123", "metadata": {"workspace_id": "12345"}}

# Get all credentials for a service
wave_creds = get_credentials("wave")
# Returns: {"access_token": "...", "password": "...", "totp_secret": "..."}

# Get a credential for a specific agent (cross-agent, for admin scripts)
token = get_credential("gmail", "access_token", agent="vera")

# Load all credentials flattened (backward compat with secrets.json)
secrets = load_secrets()
# Returns: {"toggl_api_token": "...", "wave_access_token": "...", ...}
```

**Environment requirements**: `SUPABASE_URL` and `SUPABASE_SERVICE_KEY` must be set. `AGENT_NAME` defaults to `"derek"` if not set.

**Caching**: Results are cached in-memory for the duration of the script run to avoid repeated API calls.

**CLI usage**:

```bash
python3 mcp-server/vault_client.py <service> <key> [agent]
# Example:
python3 mcp-server/vault_client.py toggl api_token vera
```

#### vault_template.py

**Location**: `mcp-server/vault_template.py`

Higher-level template designed for import into skill scripts. Provides agent-scoped convenience functions and path helpers.

```python
from vault_template import AGENT_NAME, WORKSPACE, CONFIG_DIR, get_cred, get_creds, list_connected_accounts

# AGENT_NAME is automatically read from env (required -- raises RuntimeError if missing)
# WORKSPACE = Path.home() / AGENT_NAME (e.g., /Users/user/vera)
# CONFIG_DIR = WORKSPACE / ".config" / AGENT_NAME (e.g., /Users/user/vera/.config/vera)

# Get a single credential (scoped to this agent)
api_key = get_cred("toggl", "api_token")

# Get all credentials for a service
creds = get_creds("wave")  # returns {"access_token": "...", ...}

# List connected Google accounts
accounts = list_connected_accounts("google")
# Returns: [{"email": "vera@example.com", "dir": PosixPath(...), "dir_name": "vera_at_example_com"}]

# List connected Microsoft accounts
ms_accounts = list_connected_accounts("microsoft")
```

**Key differences from vault_client.py**:
- `AGENT_NAME` is **required** (raises `RuntimeError` if not set)
- All functions are pre-scoped to the calling agent (no `agent` parameter)
- Provides `WORKSPACE`, `CONFIG_DIR`, and `ACCOUNTS_DIR` path constants
- Includes `list_connected_accounts()` for discovering OAuth-connected services by scanning the agent's accounts directory

### Skill validator checks

The security validator (`mcp-server/skill_validator.py`) enforces credential hygiene in all skill code:

| Check | Severity | Description |
|-------|----------|-------------|
| Hardcoded `secrets.json` references | High | Must use `vault_client.get_credential()` instead |
| Hardcoded token file paths | High | e.g., `/path/to/google-token.json` -- must use vault with agent scoping |
| Direct file reads of token files | Medium | `open(.*token.*\.json` -- must use vault_client |
| Hardcoded agent names in paths | Medium | `.config/derek/` -- must use `AGENT_NAME` env var |
| Cross-workspace file access | Critical | Reading from other agents' directories |
| Hardcoded API keys | Critical | `sk-ant-...`, `sk-...`, `ghp_...`, JWT tokens, etc. |

### Workspace isolation

- Scripts must use the `AGENT_NAME` env var, never hardcode an agent name
- File paths must be computed from `AGENT_NAME`: `$HOME/{AGENT_NAME}/`
- Scripts must not access other agents' directories
- Credentials must come from the vault (with optional disk fallback to the agent's own config directory)
- The same script works for all agents because `AGENT_NAME` is set by the MCP server at execution time

### Admin privilege model

Admin agents (`derek`, `dereklm`) have elevated access:

| Capability | Regular agent | Admin agent |
|-----------|--------------|-------------|
| Read own credentials | Yes | Yes |
| Read other agent's credentials | No | Yes (via `agent` parameter) |
| Store own credentials | Yes | Yes |
| Store for other agents | No | Yes (via `agent` parameter) |
| Revoke own credentials | Yes | Yes |
| Revoke other agent's credentials | No | Yes |
| Connect/disconnect services for others | No | Yes |
| Grant/revoke skill permissions | No | Yes |
