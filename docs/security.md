# Security Model

This document describes the security architecture of the agent platform. If you are an agent reading this, these are the systems that constrain what you can access and how your actions are monitored.

## Overview

Security is enforced through five layers:

1. **Credential scoping** — each agent can only read its own secrets from the vault
2. **Workspace isolation** — each agent has its own directory; behavioral rules and the approval gate prevent cross-workspace access
3. **Skill permissions** — agents only see and execute skills they have been explicitly granted
4. **Post-build validation** — new skill code is scanned for hardcoded secrets, cross-workspace access, and suspicious patterns
5. **Runtime monitoring** — security_watch.py runs hourly to detect tampering, unauthorized access, and suspicious exec patterns

These layers are independent and overlapping. A failure in one layer is caught by another.

## Credential Scoping

**Table:** `agent_credentials` in Supabase

Each agent's credentials are stored in a vault backed by the Supabase `agent_credentials` table. The table has a unique constraint on `(agent_name, service, credential_key)`, meaning each agent has its own namespace.

### Access rules

| Caller | Can read | Can write |
|--------|----------|-----------|
| Non-admin agent | Own credentials only | Own credentials only |
| Admin agent (derek, dereklm) | Any agent's credentials | Any agent's credentials |

This is enforced in the MCP server (`mcp-server/server.js`). Every credential tool (`get_credential`, `store_credential`, `list_credentials`, `revoke_credential`) checks:

```javascript
const ADMIN_AGENTS = ["derek", "dereklm"];
const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;
```

If a non-admin agent passes the `agent` parameter, the request is denied with: "Denied: only admin agents can read/store/revoke other agents' credentials."

### How agents access credentials

Agents should use the MCP vault tools:

```
get_credential(service="gmail", key="access_token")
```

For skill scripts that need credentials at runtime, use the vault client library:

```python
import sys
sys.path.insert(0, "/path/to/fleet/mcp-server")
from vault_template import AGENT_NAME, get_cred, get_creds

# Single credential
api_key = get_cred("notion", "api_key")

# All credentials for a service
gmail_creds = get_creds("gmail")  # Returns dict: {"access_token": "...", "refresh_token": "..."}
```

The `vault_template.py` module reads `AGENT_NAME` from the environment (set automatically by the MCP server and launchd plist) and queries the vault scoped to that agent.

### What agents must NOT do

- Hardcode credential file paths (e.g., `~/.config/derek/secrets.json`)
- Read other agents' config directories
- Store credentials in plaintext files instead of the vault
- Use `AGENT_NAME="derek"` to escalate access

The `skill_validator.py` scanner catches these patterns (see Post-Build Validation below).

## Workspace Isolation

Each agent has its own workspace directory at `~/AGENT_NAME/`:

```
~/derek/       Derek's workspace
~/vera/        Vera's workspace
~/nate/        Nate's workspace
```

### Enforcement mechanisms

**1. Behavioral rules (CLAUDE.md):**

Every agent's `CLAUDE.md` contains:

```markdown
## Safety
- **Workspace isolation:** Only read/write files within `~/AGENT_NAME/`. Never access other agents' directories.
- **No shared scripts:** Use only your own skills (via `my_skills` MCP tool) or built-in skills.
- **Credentials:** Use MCP vault (`get_credential`) -- never hardcode tokens or read other agents' config files.
```

This is a soft enforcement — the agent follows these rules because they are in its system prompt. The approval gate provides hard enforcement.

**2. Approval gate (hard enforcement):**

The PreToolUse hook (`hooks/approval-gate.py`) checks every Edit/Write operation:

```python
# Another agent's workspace = admin gate
for agent, ws in AGENT_WORKSPACES.items():
    if agent != AGENT_NAME and ws and file_path.startswith(ws):
        return "admin"
```

If agent "vera" tries to write to `/Users/YOUR_MAC_USERNAME/nate/anything.py`, the approval gate triggers an admin-tier approval request. The platform admin must explicitly approve cross-workspace access.

**3. MCP server scoping:**

MCP tools like `get_credential`, `list_scheduled_tasks`, and `list_skills` automatically scope to the calling agent's name. A non-admin agent cannot even query what another agent has.

### Workspace contents

Each workspace contains:

| Path | Purpose | Security relevance |
|------|---------|-------------------|
| `CLAUDE.md` | Agent identity and rules | Tampering indicator — monitored by security_watch |
| `.mcp.json` | MCP server connections | Tampering indicator — do not modify at runtime |
| `settings.json` | Model and hook config | Contains approval gate registration |
| `.config/AGENT_NAME/` | Local credential cache | Scoped to agent, used for offline fallback |
| `.state/` | Local state cache | Write-through from Supabase |
| `memory/` | Persistent memory files | Agent-private knowledge base |
| `analytics.jsonl` | Usage logs | No sensitive content (summaries only) |

## Skill Permissions

**Tables:** `skills` (catalog) and `skill_permissions` (access control) in Supabase

### How it works

The platform maintains a global skill catalog (`skills` table) listing all available capabilities. Each skill has a name, description, category, and optional `script_path` for executable skills.

Agents are granted access to specific skills via the `skill_permissions` table, which tracks:
- `agent_name` — who has access
- `skill_name` — which skill
- `granted_by` — who approved the grant
- `granted_at` — when
- `revoked_at` — soft revocation timestamp (null = active)

### Access control

| Operation | Who can do it |
|-----------|---------------|
| `my_skills` / `list_skills` | Any agent (sees own skills only; admins can query others) |
| `list_skill_catalog` | Any agent (browse what exists) |
| `run_skill` | Any agent (but only runs skills they have permission for) |
| `grant_skill` | Admin agents only |
| `revoke_skill` | Admin agents only |

When `run_skill` is called, the MCP server checks permissions BEFORE executing:

```javascript
const perms = await supabaseQuery("skill_permissions", "GET", {
    "agent_name": `eq.${AGENT_NAME}`,
    "skill_name": `eq.${skill_name}`,
    "revoked_at": "is.null",
});

if (!perms || perms.length === 0) {
    return "Skill not granted to this agent. Contact an admin.";
}
```

### Skill execution environment

When a skill script runs via `run_skill`, it executes in a sandboxed environment:

```javascript
const { stdout, stderr } = await execFileAsync("python3", [scriptPath, ...scriptArgs], {
    timeout: 120000,  // 2 minute timeout
    maxBuffer: 1024 * 1024 * 5,  // 5MB output limit
    env: {
        ...process.env,
        AGENT_NAME: AGENT_NAME,
        SUPABASE_SERVICE_KEY: SUPABASE_SERVICE_KEY,
    },
});
```

The `AGENT_NAME` env var is passed so the script can scope its credential lookups correctly.

### Service connections

The `connect_service` tool is an atomic operation that:
1. Stores credentials in the vault (scoped to the agent)
2. Creates a skill entry in the catalog
3. Grants the skill permission to the agent

This ensures that connecting a service (Gmail, Notion, etc.) automatically sets up proper credential scoping and skill access in one step.

## Post-Build Validation

**File:** `mcp-server/skill_validator.py`

After a new skill is built, the validator scans its code for security violations before it can be deployed.

### What it scans

**Hardcoded credential file paths (severity: high):**
- References to `secrets.json`
- Absolute paths to `google-token.json`, `microsoft-token.json`, `notion-token.json`, `caldav-config.json`
- These should use the vault instead

**Cross-workspace file access (severity: critical):**
- Hardcoded references to other agents' home directories (e.g., `/Users/YOUR_MAC_USERNAME/vera/` in nate's skill)
- Generated dynamically from the `KNOWN_AGENTS` environment variable

**Non-vault credential patterns (severity: medium):**
- Hardcoded `.config/derek/` paths (should use `AGENT_NAME`)
- `open()` calls on files named `*token*.json`, `*secret*`, or `*credential*`
- Direct file reads that bypass the vault

**Hardcoded secrets (severity: critical):**
- Anthropic API keys (`sk-ant-...`)
- Generic API keys (`sk-...` with 20+ characters)
- JWT tokens (`eyJ...` with 50+ characters)
- Google OAuth client secrets (`GOCSPX-...`)
- Slack bot tokens (`xoxb-...`)
- GitHub PATs (`ghp_...`)

### Usage

```bash
# Scan a single file
python3 mcp-server/skill_validator.py path/to/skill.py

# Scan a directory
python3 mcp-server/skill_validator.py skills/my-new-skill/

# Scan all skills registered in the database
python3 mcp-server/skill_validator.py --all

# Scan with agent context (allows own-workspace references)
python3 mcp-server/skill_validator.py --agent vera skills/vera-custom/

# Output as JSON
python3 mcp-server/skill_validator.py --json path/to/skill.py
```

### Allowed exceptions

Certain platform files are expected to contain credential patterns and are excluded from scanning:

- `vault_client.py`, `vault_template.py` — the vault libraries themselves
- `server.js` — the MCP server handles credentials
- `skill_validator.py` — contains the patterns as strings
- `create_agent.py`, `switch_account.py`, `gmail_connect.py` — OAuth setup scripts
- `scheduler_executor.sh` — needs Supabase key to execute
- `refresh_all_agents.sh` — token refresh infrastructure
- `analytics_server.py` — OAuth callback server

### Report format

```
Found 3 security violation(s):

  [CRITICAL] (1):
    skills/bad-skill/main.py:15
      Cross-workspace access to derek's directory
      > with open("/Users/YOUR_MAC_USERNAME/derek/memory/secrets.md") as f:

  [HIGH] (1):
    skills/bad-skill/main.py:23
      Hardcoded secrets.json reference -- use vault_client.get_credential() instead
      > config = json.load(open("~/.config/derek/secrets.json"))

  [MEDIUM] (1):
    skills/bad-skill/main.py:31
      Direct file read of token file -- use vault_client
      > token = open("google-token.json").read()
```

Exit code 0 = clean, 1 = violations found.

## Security Monitoring

**File:** `scripts/security_watch.py`

A monitoring script that runs hourly via system crontab. It performs three categories of checks.

### Integrity checks (`--integrity`)

1. **Canary verification.** The agent's `AGENTS.md` file contains a security canary string (`SECURITY-CANARY:7f3a9b2e-4c1d-8f6a-b5e7-2d3c9a1f4e8b`). If this string is missing, the file may have been tampered with or replaced.

2. **Uncommitted changes to critical files.** Runs `git status --porcelain` on these files in the workspace:
   - `AGENTS.md`
   - `SOUL.md`
   - `IDENTITY.md`
   - `USER.md`
   - `MEMORY.md`

   Any uncommitted modifications are flagged for review.

3. **Recent commits to critical files.** Checks `git log --since=2h` for any commits touching the critical files. These are flagged for verification (they may be legitimate, but should be reviewed).

4. **Unexpected untracked files.** Lists files in the workspace that are not tracked by git and not in `memory/` (which is expected to have dynamic content). Unknown files in the workspace root could indicate unauthorized additions.

### Exec audit (`--exec-audit`)

Scans the day's log file (`~/logs/agent-YYYY-MM-DD.log`) for suspicious command patterns:

| Pattern | Why it's suspicious |
|---------|-------------------|
| `rm -rf` or `rm -f` | Destructive file deletion |
| `chmod` with world-writable perms | Opening files to all users |
| `curl` or `wget` to `.onion`, `pastebin`, `ngrok` | Data exfiltration endpoints |
| `base64 ... \| sh` | Obfuscated command execution |
| `eval(` | Dynamic code execution |
| `python -c exec` | Inline Python with exec |
| `> /etc/` or `> /root/` | Writing to system directories |
| `ssh-keygen`, `authorized_keys`, `.ssh/` | SSH key manipulation |
| `exfil` | Data exfiltration keyword |
| `/proc/` | Process information access |

The scanner checks both structured JSON log entries (looking in the message field) and raw log lines.

### Session audit (`--sessions`)

Checks `~/.claude/sessions/sessions.json` for sessions from unexpected origins:

- **Telegram sessions:** If an `EXPECTED_TELEGRAM_USER` env var is set, flags sessions from other Telegram user IDs.
- **Allowed providers:** `telegram`, `webchat`, `heartbeat`, `cron`, `cron-event` are expected.
- **Unexpected providers:** Any other provider string is flagged.

Slash command sessions (`telegram:slash:*`) are always allowed.

### Running the monitor

```bash
# Run all checks
python3 scripts/security_watch.py --all

# Run specific checks
python3 scripts/security_watch.py --integrity
python3 scripts/security_watch.py --exec-audit
python3 scripts/security_watch.py --sessions

# Default (no flags) runs all checks
python3 scripts/security_watch.py
```

### Output

Clean:
```
[GREEN_CIRCLE] SECURITY CLEAN [2026-04-12 15:00 UTC]
  [CHECK] Canary present in AGENTS.md
  [CHECK] No uncommitted changes to critical files
  [CHECK] No critical file commits in last 2h
  [CHECK] No unexpected untracked files
  [CHECK] No suspicious exec patterns in today's logs
  [CHECK] All sessions from expected origins
```

Alert:
```
[RED_CIRCLE] SECURITY ALERT [2026-04-12 15:00 UTC]
[WARNING] Uncommitted changes to critical files:
  M AGENTS.md
[WARNING] Unknown untracked files in workspace:
  suspicious_script.py

Checks passed:
  [CHECK] Canary present in AGENTS.md
  [CHECK] No critical file commits in last 2h
  [CHECK] No suspicious exec patterns in today's logs
```

Exit code 0 = clean, 1 = alerts found.

## Telegram Alerting

**File:** `scripts/security_alert.sh`

A wrapper script that runs `security_watch.py` and sends a Telegram alert if anything trips.

### How it runs

System crontab entry (set up during platform installation):

```crontab
# Hourly integrity + session check
0 * * * * ADMIN_TELEGRAM_BOT_TOKEN=xxx ADMIN_TELEGRAM_CHAT_ID=yyy /path/to/scripts/security_alert.sh --integrity --sessions

# Every 6 hours: full audit including exec patterns
0 */6 * * * ADMIN_TELEGRAM_BOT_TOKEN=xxx ADMIN_TELEGRAM_CHAT_ID=yyy /path/to/scripts/security_alert.sh --all
```

### Alert flow

1. `security_alert.sh` calls `security_watch.py` with the specified check flags
2. If exit code is non-zero (alerts found), it sends the output to Telegram
3. The message is trimmed to 3900 characters (Telegram's limit is 4096)
4. The alert goes to `ADMIN_TELEGRAM_CHAT_ID` via the admin bot token

### Required environment

| Variable | Purpose |
|----------|---------|
| `ADMIN_TELEGRAM_BOT_TOKEN` | Bot token for sending alerts |
| `ADMIN_TELEGRAM_CHAT_ID` | Chat ID of the platform admin |

These must be set in the crontab entry (crontab does not inherit launchd env vars).

## Approval Gate as Security Layer

The approval gate (documented in detail in [approval-system.md](approval-system.md)) serves as a runtime security enforcement mechanism:

- **Prevents unauthorized infra modifications:** Sub-agents cannot modify MCP server code, the scheduler, or infra scripts without admin approval.
- **Prevents cross-workspace access:** File writes to another agent's directory trigger an admin approval.
- **Prevents unauthorized schema changes:** SQL DDL statements (`CREATE TABLE`, `ALTER TABLE`, `DROP TABLE`) require admin approval.
- **Prevents unauthorized git pushes:** Pushing to platform repos requires admin approval.
- **Controls external communications:** Email sends and iMessage replies require user approval (or admin, if user not configured).

The approval gate and the security monitor are complementary:
- The **approval gate** prevents unauthorized actions in real time (pre-execution)
- The **security monitor** detects suspicious activity after the fact (post-execution)

## Agent Hierarchy

The platform has a defined hierarchy for administrative control:

### Admin agents

`derek` and `dereklm` are designated admin agents. They can:

- Create admin tasks for any agent (`create_admin_task`)
- Read/write any agent's credentials
- Grant/revoke skills for any agent
- Schedule tasks for any agent
- List any agent's tasks, skills, and state
- Bypass admin-tier approval gates (their own infra modifications are not gated)

### Sub-agents

All other agents (vera, nate, blake, etc.) are sub-agents. They:

- Can only access their own credentials, tasks, skills, and state
- Cannot create admin tasks
- Cannot grant or revoke skills
- Trigger admin-tier approval when touching shared infrastructure
- Trigger user-tier approval when sending external communications

### Kill authority

Admin agents have supervisory authority over sub-agents:

- Can send admin tasks instructing agents to stop or change behavior
- Can deactivate an agent's scheduled tasks (via `update_scheduled_task` with `active: false`)
- Can revoke an agent's skills (via `revoke_skill`)
- Can revoke an agent's credentials (via `revoke_credential`)
- Can instruct the platform admin to stop an agent's daemon (`launchctl unload`)

The inter-agent communication happens through the `admin_tasks` table:
1. Admin creates a task via `create_admin_task` targeting the sub-agent
2. The sub-agent's `admin_poll` cron checks for pending tasks every 5 minutes
3. The sub-agent calls `verify_task` to confirm legitimacy (checks task exists, hasn't expired, is from an admin)
4. The sub-agent executes the task and calls `complete_task`

Tasks have an expiration time. Expired tasks are rejected by `verify_task` even if they exist in the database.

## Security Checklist for New Agents

When creating a new agent:

1. Add the agent to `AGENT_WORKSPACES` in `hooks/approval-gate.py`
2. Add the agent to `get_session()` in `mcp-server/scheduler_executor.sh`
3. Set `AGENT_NAME` and `APPROVAL_GATE_ADMIN_CHAT` in the agent's launchd plist
4. Add a canary string to the agent's `AGENTS.md` (if applicable)
5. Register the agent in the `agents` table in Supabase
6. Load default crons (which include `security_audit` and `admin_poll`)
7. Verify the agent's `settings.json` includes the approval gate hook
8. Run `skill_validator.py` on any custom skills before granting them

## Security Checklist for New Skills

When building a new skill:

1. Use `vault_template.py` for credential access (never hardcode)
2. Use `AGENT_NAME` env var for scoping (never hardcode agent names)
3. Run `python3 mcp-server/skill_validator.py path/to/skill/` and fix all violations
4. Verify the skill does not access paths outside the agent's workspace
5. Grant the skill via `grant_skill` MCP tool (admin only)
6. The `run_skill` tool enforces permission checks at runtime
