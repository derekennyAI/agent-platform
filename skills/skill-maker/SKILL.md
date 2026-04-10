---
name: skill-maker
description: "Create new skills for agents. Use when the user asks to build a new skill, capability, or tool — e.g. make a skill that does X, add the ability to Y, build a tool for Z. Handles the full workflow: scaffold, implement, test, validate security, and commit."
---

# Skill Maker

Create new workspace skills on this machine.

## Environment

| Thing | Value |
|-------|-------|
| Workspace skills dir | `~/derek/skills/` |
| Available runtimes | `python3` (stdlib only — no pip), `node`, `curl`, `bash` |
| Skill hot-reload | yes — skills appear immediately, no restart needed |
| Vault client | `~/derek/skills/admin-mcp/vault_client.py` |
| Vault template | `~/derek/skills/admin-mcp/vault_template.py` |
| Security validator | `~/derek/skills/admin-mcp/skill_validator.py` |

## Workflow

### 1. Clarify (if needed)
Understand what the skill should do and what triggers it. One or two focused questions max.

### 2. Scaffold
Create the skill directory structure manually:
```bash
mkdir -p ~/derek/skills/<skill-name>/{scripts,references}
```

### 3. Implement
Write `SKILL.md` and any scripts. Follow ALL rules below.

### 4. Test scripts
Run each script directly to confirm it works before declaring done. Fix any errors.

### 5. Validate security
Run the post-build validator:
```bash
python3 ~/derek/skills/admin-mcp/skill_validator.py ~/derek/skills/<skill-name>/
```
Fix any violations before committing. Zero violations required.

### 6. Commit
```bash
git -C ~/derek add skills/<skill-name>/
git -C ~/derek commit -m "feat: add <skill-name> skill"
```

## Script Rules

- **Python**: stdlib only. Use `urllib.request` for HTTP. No `requests`, no `pip`.
- **Bash/curl**: fine for simple fetches and text processing.
- Keep scripts deterministic and testable.

## Credential Rules (CRITICAL)

**NEVER** hardcode API keys, tokens, paths to config files, or agent names in scripts.

### For skills that need credentials

Import the vault template at the top of every script that needs credentials:

```python
import os, sys
sys.path.insert(0, "<platform-dir>/mcp-server")
from vault_template import AGENT_NAME, WORKSPACE, CONFIG_DIR, get_cred, get_creds

# Single credential
api_key = get_cred("service_name", "api_key")

# All credentials for a service
creds = get_creds("toggl")  # returns {"api_token": "...", ...}
```

The vault template automatically:
- Reads `AGENT_NAME` from env (set by the MCP run_skill tool)
- Scopes all credential reads to the calling agent
- Provides `WORKSPACE` and `CONFIG_DIR` paths scoped to the agent

### For skills that need Google OAuth (Gmail, Calendar, etc.)

Use the vault-aware pattern with disk fallback:

```python
import os, sys, json
sys.path.insert(0, "<platform-dir>/mcp-server")
from vault_client import get_credential

AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
WORKSPACE = Path(f"$HOME/{AGENT_NAME}")
CONFIG_DIR = WORKSPACE / ".config" / AGENT_NAME
ACCOUNTS_DIR = CONFIG_DIR / "accounts"

def load_google_token():
    """Load Google OAuth token — vault first, disk fallback."""
    try:
        raw = get_credential("gmail", "oauth_token", agent=AGENT_NAME)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    # Disk fallback — discover from agent's accounts directory
    if ACCOUNTS_DIR.exists():
        for d in sorted(ACCOUNTS_DIR.iterdir()):
            if d.is_dir() and (d / "google-token.json").exists():
                return json.loads((d / "google-token.json").read_text())
    raise RuntimeError(f"No Google token for agent '{AGENT_NAME}'")
```

### For skills that need secrets.json values (BugHerd, Toggl, Wave, etc.)

```python
import sys
sys.path.insert(0, "<platform-dir>/mcp-server")
from vault_client import load_secrets

secrets = load_secrets()
api_key = secrets["toggl_api_token"]
```

### What the validator checks

The security validator (`skill_validator.py`) flags:
1. **Hardcoded credential paths** — `/path/to/google-token.json` etc.
2. **Cross-workspace access** — reading from other agents' directories
3. **Non-vault credential patterns** — `open(.*token`, hardcoded agent names in paths
4. **Hardcoded secrets** — API keys, JWT tokens, OAuth client secrets in source code

## Workspace Isolation Rules

- Scripts MUST use `AGENT_NAME` env var, never hardcode an agent name
- Paths MUST be computed from `AGENT_NAME`: `$HOME/{AGENT_NAME}/`
- Scripts MUST NOT access other agents' directories
- Credentials MUST come from vault (with disk fallback to agent's OWN config dir)
- Each agent runs with `AGENT_NAME` set by the MCP server — the same script works for all agents

## Available MCP Tools (for reference)

Skills can be invoked by any agent via the `run_skill` MCP tool. The MCP server also provides:
- `get_credential` / `list_credentials` — vault reads (auto-scoped to agent)
- `store_credential` — vault writes
- `connect_service` — OAuth connection flow
- `list_skill_catalog` / `my_skills` — skill discovery
- `grant_skill` — permission management (admin only)

## What makes a good skill

- **Trigger phrase in description**: The description is what the agent reads to decide whether to load the skill. Be explicit about what user phrases activate it.
- **Use urllib.request for HTTP**: it's always available and needs no imports beyond stdlib.
- **Scripts over mental parsing**: if the task involves structured data extraction (HTML, JSON, XML), write a script.
- **Short SKILL.md**: under 300 lines. Put detailed reference in separate files and link them.
- **Secrets in vault**: never hardcode API keys or tokens in scripts. Use vault_template or vault_client.
- **Agent-scoped**: use AGENT_NAME everywhere. A skill built for Derek should work for Vera, Nate, or any future agent without code changes.
