# QMD — On-Device Semantic Search

QMD (Query Markup Documents) provides hybrid search (keyword + vector) across all agent memory and skills. It runs entirely on-device using Metal GPU acceleration — no data leaves the machine.

## Why You Need This

The file-based memory system works well for small memory stores. But once an agent has 50+ memory files and months of session logs, finding the right context becomes slow. QMD indexes everything and provides semantic search so the agent can find relevant memories even when exact keywords don't match.

## Installation

```bash
# Clone and build
git clone https://github.com/nicobailey/qmd.git ~/.qmd-install
cd ~/.qmd-install
bun install
bun run build

# Install binary
mkdir -p ~/.local/bin
ln -sf ~/.qmd-install/dist/qmd ~/.local/bin/qmd

# Verify
qmd status
```

**Requirements**: Bun (not Node.js), Apple Silicon Mac recommended for Metal GPU embeddings.

## Configure Collections

Create `~/.config/qmd/index.yml`:

```yaml
global_context: "Agent memory and skill documentation for semantic search."

collections:
  workspace:
    path: ~/AGENT_NAME/memory
    pattern: "**/*.md"
    context:
      "/": "Agent memory files — user preferences, feedback, project context, session logs"
      "/sessions": "Chronological session summaries"

  skills:
    path: ~/AGENT_NAME/skills
    pattern: "**/SKILL.md"
    context:
      "/": "Skill documentation and capabilities"

  root-docs:
    path: ~/AGENT_NAME
    pattern: "*.md"
    context:
      "/": "Top-level agent documentation — CLAUDE.md, AGENTS.md, etc."
```

Replace `AGENT_NAME` with your agent's name.

## Index and Embed

```bash
# Index all collections (keyword search)
qmd update

# Generate vector embeddings (semantic search)
qmd embed
```

## Start MCP Server

```bash
# Run as HTTP daemon (recommended — auto-restarts)
qmd mcp --http --port 8181 --daemon

# Or run as stdio (for direct .mcp.json integration)
qmd mcp
```

### Add to Agent's .mcp.json

For HTTP mode (recommended):
```json
{
  "qmd": {
    "type": "http",
    "url": "http://localhost:8181/mcp"
  }
}
```

For stdio mode:
```json
{
  "qmd": {
    "type": "stdio",
    "command": "qmd",
    "args": ["mcp"]
  }
}
```

## Auto-Refresh Cron

The default crons include a QMD refresh every 2 hours (disabled by default). Enable it after installing QMD:

```json
{
  "id": "sched_AGENT_NAME_qmd_refresh",
  "enabled": true
}
```

Or add to system crontab:
```bash
0 */2 * * * cd ~ && qmd update && qmd embed
```

## Multi-Agent Isolation (Fleet)

The single shared config above is fine for ONE agent. In a fleet where agents
serve **different people**, you must NOT put every agent's memory into one
shared qmd index — a single daemon over one DB lets any agent search every
other agent's private memory. Instead, give each agent its **own isolated qmd
instance** using XDG paths.

QMD respects `XDG_CONFIG_HOME` (config) and `XDG_CACHE_HOME` (the SQLite index),
so each agent gets a private store with zero cross-agent access:

```
~/.qmd-<agent>/config/qmd/index.yml   # one "memory" collection → ~/<agent>/memory
~/.qmd-<agent>/cache/qmd/index.sqlite # that agent's private vector index
```

Per-agent `index.yml` (only that agent's memory; no shared collections):

```yaml
collections:
  memory:
    path: ~/<agent>/memory
    pattern: "**/*.md"
    context:
      "": "<agent>'s agent memory — private to <agent>."
```

Build the index with the agent's XDG env:

```bash
XDG_CONFIG_HOME=~/.qmd-<agent>/config XDG_CACHE_HOME=~/.qmd-<agent>/cache qmd update
XDG_CONFIG_HOME=~/.qmd-<agent>/config XDG_CACHE_HOME=~/.qmd-<agent>/cache qmd embed
```

Wire qmd into that agent's `.mcp.json` as a **stdio** server scoped by the same
XDG env (no shared HTTP daemon, no ports, fully isolated):

```json
{
  "qmd": {
    "type": "stdio",
    "command": "qmd",
    "args": ["mcp"],
    "env": {
      "XDG_CONFIG_HOME": "/Users/YOU/.qmd-<agent>/config",
      "XDG_CACHE_HOME":  "/Users/YOU/.qmd-<agent>/cache"
    }
  }
}
```

Refresh all agents' private indexes on a schedule with
[`scripts/qmd_refresh_all.sh`](../scripts/qmd_refresh_all.sh) (pure indexing —
no LLM, no agent quota), driven by a launchd `StartInterval` job. The memory
markdown files themselves are regenerated from the source-of-truth store by the
memory projector; this keeps the vector index in step.

## Search Commands

```bash
qmd query "what does the user prefer for reports"  # Semantic + keyword (recommended)
qmd search "report style"                           # Keyword only (BM25)
qmd vsearch "data visualization preferences"        # Vector only
```

## Launchd Auto-Start (Optional)

Create `~/Library/LaunchAgents/com.qmd.daemon.plist` to auto-start the MCP server on boot:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.qmd.daemon</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Users/YOU/.local/bin/qmd</string>
        <string>mcp</string>
        <string>--http</string>
        <string>--port</string>
        <string>8181</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.qmd.daemon.plist
```
