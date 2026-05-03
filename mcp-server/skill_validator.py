#!/usr/bin/env python3
"""
Post-build skill validator — scans skill files for security violations.

Checks for:
1. Hardcoded credential paths (secrets.json, google-token.json, etc.)
2. Cross-workspace file access (reading from other agents' directories)
3. Non-vault credential loading patterns
4. Hardcoded API keys or tokens
5. Direct file reads of token/credential files instead of vault

Usage:
    python3 skill_validator.py <file_or_directory>
    python3 skill_validator.py --all    # Scan all skills in the DB

Returns exit code 0 if clean, 1 if violations found.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

SUPABASE_URL = "https://mfrzhijvfbwumutajqeh.supabase.co"
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# --- Violation patterns ---

# Hardcoded credential file paths — only flag when used in a hardcoded absolute path,
# NOT when used as a filename pattern in dynamic discovery (e.g. `d / "google-token.json"`)
CREDENTIAL_FILE_PATTERNS = [
    (r'secrets\.json', "Hardcoded secrets.json reference — use vault_client.get_credential() instead"),
    (r'/[^"\']*google-token\.json', "Hardcoded Google token file path — use vault with AGENT_NAME scoping"),
    (r'/[^"\']*google-credentials\.json', "Hardcoded Google credentials file path — use vault"),
    (r'/[^"\']*microsoft-token\.json', "Hardcoded Microsoft token file path — use vault"),
    (r'/[^"\']*notion-token\.json', "Hardcoded Notion token file path — use vault"),
    (r'/[^"\']*caldav-config\.json', "Hardcoded CalDAV config path — use vault"),
]

# Cross-workspace access patterns
CROSS_WORKSPACE_PATTERNS = [
    (r'/Users/YOUR_MAC_USERNAME/derek/\.config/', "Direct access to Derek's config directory"),
    (r'/Users/YOUR_MAC_USERNAME/vera/', "Cross-workspace access to Vera's directory"),
    (r'/Users/YOUR_MAC_USERNAME/nate/', "Cross-workspace access to Nate's directory"),
    (r'/Users/YOUR_MAC_USERNAME/blake/', "Cross-workspace access to Blake's directory"),
    (r'/Users/YOUR_MAC_USERNAME/julie/', "Cross-workspace access to Julie's directory"),
    (r'/Users/YOUR_MAC_USERNAME/macgyver/', "Cross-workspace access to Macgyver's directory"),
    (r'/Users/YOUR_MAC_USERNAME/dereklm/', "Cross-workspace access to DerekLM's directory"),
]

# Non-vault credential patterns
NON_VAULT_PATTERNS = [
    (r'\.config/derek/', "Hardcoded Derek config path — use AGENT_NAME + vault"),
    (r'_CONFIG_DIR\s*=.*"derek"', "Hardcoded agent name in config path"),
    (r'open\(.*token.*\.json', "Direct file read of token file — use vault_client"),
    (r'open\(.*secret', "Direct file read of secrets — use vault_client"),
    (r'open\(.*credential', "Direct file read of credentials — use vault_client"),
]

# Potential hardcoded secrets (API keys, tokens)
SECRET_PATTERNS = [
    (r'sk-ant-[a-zA-Z0-9_-]{20,}', "Hardcoded Anthropic API key"),
    (r'sk-[a-zA-Z0-9]{20,}', "Possible hardcoded API key (sk-...)"),
    (r'eyJ[a-zA-Z0-9_-]{50,}', "Possible hardcoded JWT token"),
    (r'GOCSPX-[a-zA-Z0-9_-]+', "Hardcoded Google OAuth client secret"),
    (r'xoxb-[0-9]+-[0-9]+-[a-zA-Z0-9]+', "Hardcoded Slack bot token"),
    (r'ghp_[a-zA-Z0-9]{36}', "Hardcoded GitHub PAT"),
]

# Allowed exceptions (files that are expected to have these patterns)
ALLOWED_FILES = {
    "vault_client.py",
    "vault_template.py",
    "skill_validator.py",  # this file
    "server.js",  # MCP server handles credentials
    "analytics_server.py",  # OAuth callback server
    "create_agent.py",  # agent creation template
    "switch_account.py",  # OAuth flow handler
    "refresh_all_agents.sh",  # token refresh script
    "gmail_connect.py",  # OAuth setup script — needs client secret to initiate flow
    "scheduler_executor.sh",  # scheduler needs Supabase key to execute skills
}


def scan_file(filepath, agent_name=None):
    """Scan a single file for security violations."""
    path = Path(filepath)
    if path.name in ALLOWED_FILES:
        return []

    if not path.exists() or not path.is_file():
        return []

    # Only scan Python, JavaScript, shell scripts
    if path.suffix not in ('.py', '.js', '.sh', '.ts', '.mjs'):
        return []

    try:
        content = path.read_text(errors='replace')
    except Exception:
        return []

    violations = []
    lines = content.split('\n')

    for line_num, line in enumerate(lines, 1):
        # Skip comments
        stripped = line.strip()
        if stripped.startswith('#') or stripped.startswith('//'):
            continue

        # Check credential file patterns
        for pattern, message in CREDENTIAL_FILE_PATTERNS:
            if re.search(pattern, line):
                violations.append({
                    "file": str(path),
                    "line": line_num,
                    "severity": "high",
                    "category": "hardcoded_credential_path",
                    "message": message,
                    "code": stripped[:120],
                })

        # Check cross-workspace access (skip if checking the agent's own workspace)
        for pattern, message in CROSS_WORKSPACE_PATTERNS:
            if re.search(pattern, line):
                # If we know the agent, allow access to their own workspace
                if agent_name and f"/Users/YOUR_MAC_USERNAME/{agent_name}/" in line:
                    continue
                violations.append({
                    "file": str(path),
                    "line": line_num,
                    "severity": "critical",
                    "category": "cross_workspace_access",
                    "message": message,
                    "code": stripped[:120],
                })

        # Check non-vault credential patterns
        for pattern, message in NON_VAULT_PATTERNS:
            if re.search(pattern, line):
                violations.append({
                    "file": str(path),
                    "line": line_num,
                    "severity": "medium",
                    "category": "non_vault_credential",
                    "message": message,
                    "code": stripped[:120],
                })

        # Check for hardcoded secrets
        for pattern, message in SECRET_PATTERNS:
            if re.search(pattern, line):
                violations.append({
                    "file": str(path),
                    "line": line_num,
                    "severity": "critical",
                    "category": "hardcoded_secret",
                    "message": message,
                    "code": stripped[:40] + "..." if len(stripped) > 40 else stripped,
                })

    return violations


def scan_directory(dirpath, agent_name=None):
    """Recursively scan a directory for violations."""
    all_violations = []
    for root, dirs, files in os.walk(dirpath):
        # Skip common non-code directories
        dirs[:] = [d for d in dirs if d not in ('node_modules', '.git', '__pycache__', '.venv', 'venv')]
        for fname in files:
            filepath = os.path.join(root, fname)
            violations = scan_file(filepath, agent_name)
            all_violations.extend(violations)
    return all_violations


def scan_all_skills():
    """Scan all skills registered in the database."""
    if not SUPABASE_SERVICE_KEY:
        print("SUPABASE_SERVICE_KEY required for --all mode", file=sys.stderr)
        sys.exit(1)

    url = f"{SUPABASE_URL}/rest/v1/skills?select=name,script_path&script_path=not.is.null&script_path=not.like.anthropics/*"
    req = urllib.request.Request(url, headers={
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    })
    resp = urllib.request.urlopen(req)
    skills = json.loads(resp.read())

    all_violations = []
    for skill in skills:
        path = skill.get("script_path", "")
        if path and os.path.exists(path):
            violations = scan_file(path)
            for v in violations:
                v["skill_name"] = skill["name"]
            all_violations.extend(violations)

    return all_violations


def format_report(violations):
    """Format violations into a readable report."""
    if not violations:
        return "✓ No security violations found."

    by_severity = {"critical": [], "high": [], "medium": [], "low": []}
    for v in violations:
        by_severity.get(v["severity"], by_severity["low"]).append(v)

    lines = [f"Found {len(violations)} security violation(s):\n"]

    for severity in ["critical", "high", "medium", "low"]:
        items = by_severity[severity]
        if not items:
            continue
        lines.append(f"  [{severity.upper()}] ({len(items)}):")
        for v in items:
            skill = v.get("skill_name", "")
            skill_prefix = f"[{skill}] " if skill else ""
            lines.append(f"    {skill_prefix}{v['file']}:{v['line']}")
            lines.append(f"      {v['message']}")
            lines.append(f"      > {v['code']}")
        lines.append("")

    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Skill security validator")
    parser.add_argument("path", nargs="?", help="File or directory to scan")
    parser.add_argument("--all", action="store_true", help="Scan all skills in the database")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--agent", default=None, help="Agent name (allows own-workspace access)")
    args = parser.parse_args()

    if args.all:
        violations = scan_all_skills()
    elif args.path:
        path = Path(args.path)
        if path.is_file():
            violations = scan_file(path, args.agent)
        elif path.is_dir():
            violations = scan_directory(path, args.agent)
        else:
            print(f"Not found: {path}", file=sys.stderr)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)

    if args.json:
        print(json.dumps(violations, indent=2))
    else:
        print(format_report(violations))

    sys.exit(1 if violations else 0)


if __name__ == "__main__":
    main()
