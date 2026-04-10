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
