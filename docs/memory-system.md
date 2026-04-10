# Memory System

Each agent has a persistent memory system that survives across conversations. Memory is stored as markdown files in the agent's `memory/` directory.

## How It Works

1. **Session memory cron** runs every 30 minutes
2. When a conversation goes idle, the agent compacts the session into a summary
3. Important information is extracted into categorized memory files
4. QMD (optional) indexes everything for semantic search across sessions

## Memory Types

| Type | What to save | Example |
|------|-------------|---------|
| **user** | User's role, preferences, style, knowledge level | "Senior engineer, hates sycophancy, prefers terse responses" |
| **feedback** | User corrections AND confirmations of approach | "Don't mock the database in tests — use real DB" |
| **project** | Ongoing work, goals, deadlines, decisions | "Auth rewrite driven by compliance, not tech debt" |
| **reference** | Pointers to external resources | "Pipeline bugs tracked in Linear project INGEST" |

## Memory File Format

Each memory file uses frontmatter:

```markdown
---
name: Short title
description: One-line description (used to decide relevance)
type: user|feedback|project|reference
---

Content here. For feedback/project types, structure as:
- The rule or fact
- **Why:** the reason behind it
- **How to apply:** when this guidance matters
```

## Memory Index

Each agent keeps a `memory/MEMORY.md` index file — one line per memory, under 200 lines total. This index is loaded into every conversation so the agent knows what it remembers.

## Session Summaries

Stored in `memory/sessions/YYYY-MM-DD.md`. Each session summary includes:
- What was discussed
- What was done (actions taken)
- Decisions made
- Open items
- Memories saved or updated

## QMD (Optional — On-Device Semantic Search)

QMD provides hybrid search (keyword + vector) across all agent memory and skills. Useful for agents with large memory stores.

### Setup
1. Install QMD from [github.com/nicobailey/qmd](https://github.com/nicobailey/qmd)
2. Configure collections for memory/, skills/, and root docs
3. Add QMD MCP server to agent's `.mcp.json`
4. Set up refresh cron: `qmd update && qmd embed` every 2 hours

### Collections
- **workspace** — memory/ directory (session logs, preferences, feedback)
- **skills** — skills/ directory (SKILL.md files, script docs)
- **root-docs** — top-level markdown files (CLAUDE.md, AGENTS.md, etc.)

## Default Crons

Every agent should have these crons (see `configs/default-crons.json`):

1. **Session memory review** — every 30 min, compacts idle sessions
2. **Admin task polling** — every 5 min, checks for inter-agent tasks

## What NOT to Save

- Code patterns or architecture (derivable from reading the code)
- Git history (use `git log` / `git blame`)
- Debugging solutions (the fix is in the code)
- Ephemeral task details (use tasks/plans within the conversation)
