# Skills Framework

Skills are self-contained capability modules that give agents specific abilities. Each skill consists of a `SKILL.md` instruction file (which tells the agent how and when to use the skill) and an optional `scripts/` directory containing executable code.

Skills are the primary mechanism for extending what an agent can do. They are stored centrally in the platform repo and shared across all agents, but access is controlled per-agent through the `skill_permissions` Supabase table.

---

## Skill Structure

Every skill lives in its own directory under `skills/<name>/`:

```
skills/<name>/
├── SKILL.md           # Instructions for the agent (required)
├── scripts/           # Executable scripts (optional)
│   ├── run.sh         # Shell runner, Python script, Node script, etc.
│   └── ...
├── references/        # Supporting docs, templates (optional)
└── user-scripts/      # User-facing scripts (optional, used by shell skill)
```

### SKILL.md

The `SKILL.md` file is the skill's brain. It contains:

- **Frontmatter** with `name` and `description` fields. The `description` is critical -- it tells the agent when to activate the skill based on user input.
- **Instructions** that guide the agent through the skill's workflow: what steps to follow, what tools to use, what constraints to obey.
- **Examples** of commands, API calls, or action patterns.
- **Security rules** specific to this skill.

Example frontmatter:

```yaml
---
name: playwright
description: "Run headless Chromium via Playwright to visit websites, fill forms, click buttons, take screenshots, and extract rendered content. Triggered by: 'use playwright', 'open in browser', 'screenshot this site'."
---
```

The `description` field acts as the trigger -- when a user's request matches these phrases, the agent loads and follows the skill's instructions.

### Scripts

Scripts in `scripts/` are the executable components. They can be written in:

- **Python** (stdlib only -- no pip, use `urllib.request` for HTTP)
- **Node.js**
- **Bash/shell**

Scripts are executed via the `run_skill` MCP tool, which passes `AGENT_NAME` and `SUPABASE_SERVICE_KEY` as environment variables. This means the same script works for any agent without code changes.

---

## Available Skills

### skill-maker

**Location**: `skills/skill-maker/`

Meta-skill that helps agents build new skills at runtime. When a user asks an agent to "build a skill that does X" or "add the ability to Y," this skill guides the agent through the full workflow:

1. Clarify requirements (1-2 questions max)
2. Scaffold the directory structure (`mkdir -p skills/<name>/{scripts,references}`)
3. Implement `SKILL.md` and any scripts
4. Test scripts by running them directly
5. Validate security with `skill_validator.py` (zero violations required)
6. Commit to git

The skill-maker enforces all credential and isolation rules. Scripts must use `vault_template.py` or `vault_client.py` for credentials, never hardcode API keys, and always use `AGENT_NAME` env var instead of hardcoded agent names.

**Key files**:
- `skills/skill-maker/SKILL.md` -- complete build workflow and rules

---

### shell

**Location**: `skills/shell/`

Executes shell scripts from a constrained `user-scripts/` directory. Provides a sandboxed execution environment with strict security constraints:

- **Path confinement**: Only scripts inside `skills/shell/user-scripts/` can run. Symlinks are resolved to prevent escaping.
- **No inline commands**: The runner takes a filename, not a command string.
- **Executable bit required**: Scripts must be `chmod +x`.
- **60-second timeout**: Runaway scripts are killed automatically.
- **Audit log**: Every execution is logged to `skills/shell/audit.log` with timestamp, script name, and exit code.

**Execution**:

```bash
bash skills/shell/scripts/run.sh <script-name>.sh [args...]
```

**Included user scripts**:

| Script | Purpose |
|--------|---------|
| `gmail_inbox.py` | Gmail inbox management: list, read, archive, mark-read, batch-archive. Supports `--account` flag for multi-account. |
| `send_email.py` | Send email via Gmail API. Enforces a contacts whitelist -- recipients not in the whitelist are hard-blocked. |
| `gmail_auth.py` | Gmail OAuth token management and refresh. |

Scripts can use any shebang (`#!/usr/bin/env bash`, `#!/usr/bin/env python3`, `#!/usr/bin/env node`). The runner determines the interpreter from the shebang.

**Key files**:
- `skills/shell/SKILL.md` -- usage instructions and security constraints
- `skills/shell/scripts/run.sh` -- the constrained runner
- `skills/shell/user-scripts/` -- all executable scripts

---

### agent-setup

**Location**: `skills/agent-setup/`

Agent creation, Gmail OAuth connection, and account switching. This is the skill used to bootstrap new agents on the platform.

**What it creates for a new agent**:

1. Workspace directory (`~/<agent_name>/`) with `CLAUDE.md`, `settings.json`, `startup-instructions.md`
2. Memory system (`~/<agent_name>/memory/`) with `MEMORY.md`, soul file, user profile, feedback, sessions/
3. Telegram channel config (`~/.claude/channels/telegram_<agent_name>/`) with `.env` and `access.json`
4. Launcher script (`~/<agent_name>/launcher.sh`)
5. launchd plist (`~/Library/LaunchAgents/com.<agent_name>-agent.daemon.plist`)
6. Analytics files (`usage_stats.json`, `analytics.jsonl`)
7. Registry entry in `skills/agent-setup/agents.json`

**Usage**:

```bash
python3 skills/agent-setup/create_agent.py \
  --name myagent \
  --persona "Alex" \
  --human "Your Name" \
  --bot-token "123456:ABC..." \
  --user-id "your-telegram-id"
```

**Gmail connection**:

```bash
# Generate OAuth URL for user to authorize
python3 skills/agent-setup/gmail_connect.py url --agent myagent

# Exchange auth code for tokens after user authorizes
python3 skills/agent-setup/gmail_connect.py exchange --agent myagent --code "4/0AeaYSH..."

# Verify connection works
python3 skills/agent-setup/gmail_connect.py verify --agent myagent
```

**Key files**:
- `skills/agent-setup/SKILL.md` -- usage and post-creation steps
- `skills/agent-setup/create_agent.py` -- full agent creation script
- `skills/agent-setup/gmail_connect.py` -- Gmail OAuth self-serve flow
- `skills/agent-setup/switch_account.py` -- account switching handler

---

### playwright

**Location**: `skills/playwright/`

Browser automation via headless Chromium using Playwright. Used when `web_fetch` fails due to JavaScript rendering, bot protection, or dynamic content. The skill uses exec only -- no Chrome extension or browser relay.

**Workflow**: Write a JSON actions file, run `node browser.js`, read the output.

```json
[
  { "action": "navigate", "url": "https://example.com" },
  { "action": "screenshot", "path": "example-home.png" },
  { "action": "get_text", "selector": "h1" }
]
```

```bash
node skills/playwright/scripts/browser.js /tmp/my-actions.json
```

**Available actions**: `navigate`, `click`, `fill`, `select`, `get_text`, `get_url`, `screenshot`, `wait`, `wait_for`, `hover`, `press_key`, `scroll`.

**Security constraints**:
- Isolated browser profile (no cookies shared with real Chrome)
- `file://` and `data:` URLs blocked
- No arbitrary JavaScript execution (`page.evaluate()` not exposed)
- All navigations logged to `memory/browser-audit.log`
- Screenshots saved to `memory/screenshots/`

**Key files**:
- `skills/playwright/SKILL.md` -- complete action reference and examples
- `skills/playwright/scripts/browser.js` -- Playwright runner
- `skills/playwright/scripts/pw` -- helper launcher script
- `skills/playwright/scripts/package.json` -- Playwright dependency

---

### frontend-design

**Location**: `skills/frontend-design/`

UI/UX design guidance for building distinctive, production-grade frontend interfaces. This is an instruction-only skill (no scripts) that guides agents to create memorable designs that avoid generic "AI slop" aesthetics.

Covers:
- Design thinking: purpose, tone, constraints, differentiation
- Typography: distinctive font pairing, no generic fonts (Inter, Roboto, Arial)
- Color and theme: cohesive palettes, CSS variables, dominant colors with sharp accents
- Motion: animations, micro-interactions, scroll-triggering, CSS-only or Motion library
- Spatial composition: asymmetry, overlap, grid-breaking, negative space
- Backgrounds: gradient meshes, noise textures, geometric patterns, grain overlays

**Key files**:
- `skills/frontend-design/SKILL.md` -- complete design guidelines

---

### diagnose

**Location**: `skills/diagnose/`

System diagnostics skill that sweeps platform systems for errors and investigates root causes. Instruction-only skill (no scripts).

**Usage modes**:
- `/diagnose` -- full sweep of all systems (last 24h)
- `/diagnose quick` -- health checks only
- `/diagnose n8n` -- deep dive on n8n executions and workflow errors
- `/diagnose supabase` -- deep dive on logs, failed queues, constraint issues
- `/diagnose "<issue>"` -- targeted investigation of a specific problem

Checks frontend health, Supabase errors, Grafana incidents, n8n failed executions, Linear open bugs, and Railway deploy status. Produces a structured health report with status icons and recommended actions.

**Key files**:
- `skills/diagnose/SKILL.md` -- diagnostic workflow, queries, and report format

---

### ux-review

**Location**: `skills/ux-review/`

Comprehensive UX teardown of websites. Given a URL, the skill discovers all pages (via sitemap or link crawling), extracts UX data from each page, and produces a detailed analysis referencing specific elements found on the site.

**Workflow**:
1. Extract and normalize the URL
2. Discover pages via sitemap.xml, robots.txt, or homepage link crawling (cap: 20 pages)
3. Extract UX data: titles, meta descriptions, headings, nav links, CTAs, forms, first visible text
4. Write a structured teardown covering navigation, value proposition, CTAs, forms, content hierarchy, trust signals, accessibility, and top 3 fixes

**Key files**:
- `skills/ux-review/SKILL.md` -- review framework and output format
- `skills/ux-review/scripts/extract_ux.py` -- page data extractor (alternative to `web_fetch`)

---

## Permission System

Skills are access-controlled through the `skill_permissions` Supabase table. An agent can only use skills that have been explicitly granted to it.

### Database Tables

**`skills`** -- catalog of all available skills:

| Column | Type | Purpose |
|--------|------|---------|
| `name` | TEXT (unique) | Skill identifier (e.g., `gmail`, `toggl`, `playwright`) |
| `description` | TEXT | Internal description |
| `user_description` | TEXT | User-facing description shown by `my_skills` |
| `category` | TEXT | One of: communication, productivity, research, integration, admin, finance, monitoring |
| `requires_credentials` | BOOLEAN | Whether the skill needs vault credentials to function |
| `credential_services` | TEXT[] | Which credential services the skill depends on |
| `script_path` | TEXT | Absolute path to the executable script (null for instruction-only skills) |

**`skill_permissions`** -- per-agent access grants:

| Column | Type | Purpose |
|--------|------|---------|
| `agent_name` | TEXT | Which agent has access |
| `skill_name` | TEXT | Which skill is granted |
| `granted_by` | TEXT | Who granted the permission (admin agent name) |
| `granted_at` | TIMESTAMPTZ | When it was granted |
| `revoked_at` | TIMESTAMPTZ | When revoked (null = active) |

The `(agent_name, skill_name)` pair is unique. Revoking a skill sets `revoked_at` to the current timestamp (soft delete).

### MCP Tools for Skills

| Tool | Who can call | Purpose |
|------|-------------|---------|
| `my_skills` | Any agent | Get a user-friendly list of granted skills with descriptions. Use when a user asks "what can you do?" |
| `list_skills` | Any agent (own); admin (any) | List granted skills with internal details (category, executable, granted_by) |
| `list_skill_catalog` | Any agent | Browse all available skills in the catalog, optionally filtered by category |
| `grant_skill` | Admin only | Grant an agent permission to use a skill. The skill must exist in the catalog. |
| `revoke_skill` | Admin only | Revoke an agent's permission (soft delete via `revoked_at`) |
| `run_skill` | Any agent (if permitted) | Execute a skill's script. Checks `skill_permissions` before running. |

**Admin agents** are hardcoded in `server.js` as `["derek", "dereklm"]`. Only these agents can grant/revoke skills or query other agents' permissions.

### Example: Granting a skill

An admin agent grants `toggl` to agent `vera`:

```
Tool: grant_skill
  agent: "vera"
  skill_name: "toggl"
```

The MCP server verifies the skill exists in the `skills` catalog, then upserts a row in `skill_permissions`.

### Example: Checking permissions

Agent `vera` checks what she can do:

```
Tool: my_skills
  agent_name: "vera"
```

Returns a plain-language list:

```
- toggl -- enny.ai time tracking: start/stop timer, list entries, weekly summary
- gmail_vera_at_example_com -- Email management -- vera@example.com
```

---

## Dynamic Skills

Agents can create new skills at runtime using the **skill-maker** skill. This is how the platform grows organically -- when a user asks for a capability that doesn't exist, the agent scopes it, builds it, validates it, and adds it to the toolkit.

### Build workflow

1. **Clarify** -- understand what the skill should do (1-2 questions max)
2. **Scaffold** -- create the directory structure:
   ```bash
   mkdir -p skills/<skill-name>/{scripts,references}
   ```
3. **Implement** -- write `SKILL.md` and any scripts following all credential and isolation rules
4. **Test** -- run each script directly to verify it works
5. **Validate** -- run the security scanner:
   ```bash
   python3 mcp-server/skill_validator.py skills/<skill-name>/
   ```
6. **Commit** -- add to git

### Security validation

The post-build validator (`mcp-server/skill_validator.py`) scans all `.py`, `.js`, `.sh`, `.ts`, and `.mjs` files for:

| Category | Severity | What it catches |
|----------|----------|----------------|
| `hardcoded_credential_path` | High | References to `secrets.json`, hardcoded paths to `google-token.json`, `microsoft-token.json`, etc. |
| `cross_workspace_access` | Critical | Reading from other agents' directories (e.g., `/Users/user/otheragent/`) |
| `non_vault_credential` | Medium | Hardcoded agent names in config paths, direct file reads of token files |
| `hardcoded_secret` | Critical | API keys (`sk-ant-...`, `sk-...`), JWT tokens (`eyJ...`), GitHub PATs (`ghp_...`), Slack tokens (`xoxb-...`), Google OAuth secrets (`GOCSPX-...`) |

**Zero violations required** before a skill can be committed. The validator returns exit code 0 if clean, 1 if violations are found.

Certain infrastructure files are exempt from scanning (listed in `ALLOWED_FILES` in `skill_validator.py`): `vault_client.py`, `vault_template.py`, `server.js`, `create_agent.py`, etc.

**Usage**:

```bash
# Scan a single file
python3 mcp-server/skill_validator.py path/to/script.py

# Scan a directory
python3 mcp-server/skill_validator.py skills/my-skill/

# Scan all skills registered in the database
python3 mcp-server/skill_validator.py --all

# Output as JSON
python3 mcp-server/skill_validator.py skills/my-skill/ --json

# Allow own-workspace access for a specific agent
python3 mcp-server/skill_validator.py skills/my-skill/ --agent vera
```

### Credential rules for new skills

Scripts must **never** hardcode API keys, tokens, paths to config files, or agent names. Instead:

```python
import sys
sys.path.insert(0, "<platform-dir>/mcp-server")
from vault_template import AGENT_NAME, WORKSPACE, CONFIG_DIR, get_cred, get_creds

# Single credential
api_key = get_cred("service_name", "api_key")

# All credentials for a service
creds = get_creds("toggl")  # returns {"api_token": "...", ...}
```

The vault template automatically reads `AGENT_NAME` from the environment, scopes all credential reads to the calling agent, and provides `WORKSPACE` and `CONFIG_DIR` paths scoped to the agent's directory.

---

## Skill Execution

The `run_skill` MCP tool is the standard way to execute a skill's script. Here's the complete flow:

1. **Permission check**: The MCP server queries `skill_permissions` for the calling agent + skill name, filtering for non-revoked entries. If no permission exists, the request is denied.

2. **Script lookup**: The server queries the `skills` catalog for the skill's `script_path`. If the skill has no `script_path` (instruction-only skill), it returns an error explaining the skill is capability-based, not script-based.

3. **Execution**: The script is executed via `python3` with:
   - **Timeout**: 120 seconds (2 minutes)
   - **Max buffer**: 5 MB
   - **Environment variables**: All parent env vars, plus:
     - `AGENT_NAME` -- set to the calling agent's name
     - `SUPABASE_SERVICE_KEY` -- for vault access
   - **Arguments**: Passed as space-separated string, split on whitespace

4. **Output**: The tool returns the script's stdout (truncated to 10,000 chars) and stderr (truncated to 2,000 chars). On error, it includes the error message.

**Example**:

```
Tool: run_skill
  skill_name: "toggl"
  args: "list --days 7"
```

The MCP server checks that the agent has `toggl` permission, looks up the script path from the catalog, then runs:

```bash
AGENT_NAME=vera python3 /path/to/toggl.py list --days 7
```

### Instruction-only skills

Skills like `frontend-design`, `diagnose`, and `ux-review` have no `script_path`. They work purely through their `SKILL.md` instructions -- the agent reads the skill file and follows its guidance using standard tools (web_fetch, exec, MCP tools, etc.). These skills cannot be invoked via `run_skill`.

### Service-linked skills

When a service is connected via `connect_service`, the MCP server automatically creates a skill entry in the catalog and grants it to the agent. For example, connecting Gmail for `vera@example.com` creates a skill named `gmail_vera_at_example_com` with the description "Email management -- vera@example.com" and grants it to the agent. This makes connected services visible in `my_skills` output.
