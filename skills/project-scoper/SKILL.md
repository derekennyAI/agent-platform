---
name: project-scoper
description: "Create and manage project scope documents and development plans in Notion. Also checks Derek's inbox for scope requests sent via the daily email's 'Scope this' button. Triggered by: 'scope this idea', 'build a scope for', 'create a project from idea', 'scope out', 'write up a project plan', 'check the scope', 'read the scope', 'what did Farlen say about the scope', 'list projects', 'project status', 'check for scope requests', 'check inbox', 'write the dev plan', 'create the build doc', 'start building'."
---

# Project Scoper Skill

Creates detailed scope documents in Notion from ideas in the idea-hunter database. Farlen reviews and approves in Notion, Derek reads back the state via API.

## Overview

- **Platform**: Notion (API via `NOTION_API_KEY` env var)
- **Database**: Project Pipeline (inline DB inside "Derek's Projects" page)
- **Config**: `skills/project-scoper/references/config.json` (database ID, page ID)
- **Scripts**: `scripts/create_scope.py`, `scripts/read_scope.py`, `scripts/notion_client.py`

## Workflow: Scope a New Project

When Farlen picks an idea (e.g. "scope out idea-20260304-001" or "build a project plan for the resume roaster"):

### 1. Research the idea deeper
Before creating the scope doc, spend time understanding the idea:
- Re-read the idea from `ideas.json` via manage_ideas.py
- If the idea came from Reddit, revisit the source posts for more context
- Think about: target user, MVP feature set, simplest tech stack, what to cut

### 2. Create the wireframe diagram
Before creating the scope doc, write a Mermaid diagram that shows the core user flow or key screens. Use `graph TD` for flows or `graph LR` for layouts. Keep it simple — 5–10 nodes max. Example for a resume tool:

```
graph TD
    A[Landing Page\nUpload resume] --> B[Processing\nAI analysis]
    B --> C[Results Page\nOverall score + roast]
    C --> D[Section Breakdown\nScores per section]
    C --> E[Rewrite Suggestions\nImproved bullet points]
    D --> F[Download Report]
    E --> F
```

### 3. Create the scope document
Pass the wireframe as `--wireframe`. Use single-quoted string and escape newlines with `\n`:

```bash
python3 skills/project-scoper/scripts/create_scope.py \
  --id idea-20260304-001 \
  --target-user "Job seekers aged 25-40 actively applying" \
  --features "PDF upload|AI roast feedback|Section scoring|Rewrite suggestions" \
  --tech "Next.js + Tailwind|pdf-parse for PDF|Claude API for analysis|Vercel hosting" \
  --wireframe "graph TD\n    A[Upload Resume] --> B[AI Analysis]\n    B --> C[Score + Roast]\n    C --> D[Section Breakdown]\n    C --> E[Rewrite Suggestions]"
```

Or without an idea ID:
```bash
python3 skills/project-scoper/scripts/create_scope.py \
  --title "AI Resume Roaster" \
  --problem "People get zero honest resume feedback" \
  --solution "Upload PDF, get brutal AI feedback" \
  --target-user "Job seekers 25-40" \
  --features "PDF upload|AI roast|Section scores|Rewrites" \
  --tech "Next.js|Claude API|Vercel" \
  --wireframe "graph TD\n    A[Upload Resume] --> B[AI Analysis]\n    B --> C[Results]"
```

The wireframe renders as a Mermaid diagram in Notion. Always include one — don't leave the placeholder.

### 3. Send Farlen the link
After creating, send Farlen the Notion URL via Telegram so he can review:
```
Created scope doc for "AI Resume Roaster": <notion-url>

Check it out — review the features, uncheck what to cut, and leave comments on anything you want changed. Set status to Approved when ready.
```

### 4. Wait for Farlen's feedback
Do NOT proceed until Farlen tells you to check the scope or sets status to Approved.

## Workflow: Read Scope Feedback

When Farlen says "check the scope" or "I updated the project" or "what's the status":

### 1. Read the scope document
```bash
python3 skills/project-scoper/scripts/read_scope.py --title "Resume Roaster"
# or
python3 skills/project-scoper/scripts/read_scope.py --page-id <id>
```

### 2. Analyze what changed
Look at:
- **Checkboxes**: Which features are checked (approved) vs unchecked (cut)
- **Comments**: What Farlen said — address each comment
- **Status**: If set to "Approved", move to build phase
- **Text edits**: If Farlen changed problem/solution/tech text, respect the changes

### 3. Respond via Telegram
Summarize what you see:
```
Checked the scope for Resume Roaster:
✅ PDF upload, AI roast, Section scoring
❌ Shareable results page (cut)
💬 Comment: "Make the scoring visual, like a report card"

I'll update the scope with the report card idea. Want me to start building once you approve?
```

### 4. Update the scope if needed
Make changes via the Notion API (update blocks, add new features, etc.) then tell Farlen to re-check.

## Workflow: Create Dev Doc (After Approval)

**Trigger:** Farlen sets scope status to Approved, or says "write the dev plan" / "start building" / "create the build doc".

This creates a comprehensive, phased development document as a **child page** of the scope doc in Notion. It covers every detail Derek needs to actually build the project.

### 1. Read the approved scope
```bash
python3 skills/project-scoper/scripts/read_scope.py --title "Project Name"
```
Use this as context to understand exactly what was approved.

### 2. Write the JSON spec
Think through the entire build. Write to `memory/dev-docs/{project-slug}.json`.

**Standard: a senior engineer could pick this up and build with zero guesswork.**

That means:
- Every phase has 5–15 tasks minimum
- Every task has exact shell commands, not descriptions of commands
- Every file created is listed
- Every SQL table has every column with type and constraints
- Every API route has request shape and response shape
- Every env var is listed with name, purpose, and example value
- Every non-obvious decision has a `notes` field explaining why

**Bad task** (do not write this):
```json
{"title": "Set up auth", "details": "Configure Supabase auth"}
```

**Good task** (write this):
```json
{
  "title": "Configure Supabase Auth — Phone OTP",
  "details": "Enable Phone provider in Supabase dashboard. Set up OTP flow using supabase.auth.signInWithOtp({ phone }). Redirect to /verify after sending OTP. On verify screen, call supabase.auth.verifyOtp({ phone, token, type: 'sms' }). Session cookie set automatically by auth-helpers.",
  "commands": [
    "npm install @supabase/auth-helpers-nextjs @supabase/supabase-js",
    "npx supabase gen types typescript --project-id $PROJECT_ID > src/types/supabase.ts"
  ],
  "files_created": [
    "src/app/(auth)/login/page.tsx",
    "src/app/(auth)/verify/page.tsx",
    "src/lib/supabase/client.ts",
    "src/lib/supabase/server.ts",
    "src/middleware.ts"
  ],
  "test_command": "npx vitest run src/lib/supabase",
  "notes": "Use createServerClient from auth-helpers for server components, createBrowserClient for client components. Middleware handles session refresh on every request."
}
```

A high-level summary like '3 phases, 6 weeks' is not a dev plan. Do not write that.

**JSON schema:**
```json
{
  "title": "Project Name — Dev Plan",
  "overview": "2-3 sentences: what it does, who it's for, core value prop.",
  "stack": [
    "Next.js 14 + TypeScript (App Router)",
    "Supabase (auth + PostgreSQL)",
    "Resend (transactional email)",
    "jsPDF (PDF generation)",
    "Vercel (hosting)"
  ],
  "environment_vars": [
    {
      "name": "NEXT_PUBLIC_SUPABASE_URL",
      "description": "Supabase project URL",
      "example": "https://xxxx.supabase.co"
    },
    {
      "name": "SUPABASE_SERVICE_ROLE_KEY",
      "description": "Service role key for server-side Supabase operations"
    }
  ],
  "database_schema": [
    {
      "table": "clients",
      "columns": [
        {"name": "id", "type": "uuid", "notes": "primary key, default gen_random_uuid()"},
        {"name": "user_id", "type": "uuid", "notes": "references auth.users on delete cascade"},
        {"name": "name", "type": "text", "notes": "not null"},
        {"name": "email", "type": "text"},
        {"name": "company", "type": "text"},
        {"name": "created_at", "type": "timestamptz", "notes": "default now()"}
      ]
    }
  ],
  "api_routes": [
    {
      "method": "POST",
      "path": "/api/invoices",
      "description": "Create a new invoice and return its ID and preview URL",
      "auth": true,
      "body": {"client_id": "uuid", "line_items": "LineItem[]", "tax_rate": "number"},
      "returns": {"invoice_id": "uuid", "url": "string"}
    }
  ],
  "phases": [
    {
      "name": "Phase 0: Project Setup",
      "goal": "Working Next.js app deployed to Vercel, Supabase project connected, env vars wired up.",
      "tasks": [
        {
          "title": "Initialize Next.js project",
          "details": "Create the app with TypeScript, Tailwind, and App Router. Use src/ directory layout.",
          "commands": [
            "npx create-next-app@latest contractor-invoicing --typescript --tailwind --app --src-dir",
            "cd contractor-invoicing"
          ],
          "files_created": [
            "package.json",
            "tailwind.config.ts",
            "src/app/layout.tsx",
            "src/app/page.tsx"
          ],
          "notes": "Use App Router — not Pages Router. Do not use create-next-app's built-in ESLint config."
        },
        {
          "title": "Install dependencies",
          "details": "Install all third-party packages upfront so nothing is missing mid-build.",
          "commands": [
            "npm install @supabase/supabase-js @supabase/auth-helpers-nextjs",
            "npm install resend jspdf",
            "npm install -D @types/node"
          ],
          "files_created": ["package-lock.json"]
        }
      ],
      "done_when": "localhost:3000 loads the app. Vercel deployment URL is live. Supabase health check passes."
    }
  ]
}
```

### 3. Run the script
```bash
python3 skills/project-scoper/scripts/create_dev_doc.py \
  --scope-id <notion-scope-page-id> \
  --from-file memory/dev-docs/contractor-invoicing.json
```

### 4. Update scope status + notify Farlen
```python
# In a quick exec — update scope to Building
python3 skills/project-scoper/scripts/read_scope.py --title "Contractor Invoicing" | grep page_id
# then update_page with status Building
```
Send Farlen the dev doc Notion link via Telegram:
```
Dev plan for Contractor Invoicing Tool is ready: <notion-url>

N phases, fully detailed. Starting Phase 0 now.
```

## Workflow: Build a Project

**Trigger:** After `start_build.py` has been run and state.json exists. Any session where Farlen says "keep building", "what's the status", or "continue".

### Every Session Start
1. Read `memory/builds/{slug}/state.json`
2. If `blocked_on` is set → message Farlen with the exact error, stop
3. If `waiting_for_farlen` is true → remind Farlen the phase is complete and waiting for approval, stop
4. If tests exist: `cd projects/{slug} && npx vitest run` — if they fail, investigate before touching anything

### Per-Task TDD Loop
1. Write the test file first (or use `call_agent.py --prompt-file ... "write tests for {task title}"`)
2. Confirm tests fail (red)
3. Implement the code (or use `call_agent.py` with the test file as context)
4. Run the task's `test_command` — if pass, continue
5. If fail after 2 attempts: `update_build_status.py --blocked "{exact error}"` → message Farlen → stop (circuit breaker)
6. `git add -A && git commit -m "[{slug}] Phase {n} Task {m}: {title}"`
7. `update_build_status.py --state-file memory/builds/{slug}/state.json --complete-task --commit {hash}`

### Sub-Agent Usage (call_agent.py)
Use for any task that involves writing more than ~50 lines of code:

```bash
# Write a prompt file first
mkdir -p memory/builds/{slug}/prompts
cat > memory/builds/{slug}/prompts/write-auth.md << 'EOF'
Write the complete Supabase phone OTP auth flow for a Next.js 14 App Router app.
Requirements: [paste task details from dev doc]
Output: Complete file contents for each file in files_created.
EOF

python3 skills/project-scoper/scripts/call_agent.py \
  --prompt-file memory/builds/{slug}/prompts/write-auth.md \
  --context projects/{slug}/src/lib/supabase.ts projects/{slug}/src/types/ \
  --output /tmp/agent-response.md
```

Review `/tmp/agent-response.md`, extract file contents, write files, run tests.

### Phase Gate
When `update_build_status.py --complete-phase` runs:
1. Take a Playwright screenshot of the working feature (if applicable)
2. Send Farlen the screenshot + summary via Telegram: phase name, what was built, test results
3. "Ready for Phase N+1 when you are. Say 'continue' to proceed."
4. **Stop** — do NOT start next phase until Farlen confirms

When Farlen says "continue" / "approved" / "start next phase":
```bash
python3 skills/project-scoper/scripts/update_build_status.py \
  --state-file memory/builds/{slug}/state.json --continue
```

### Phase 0 Rule
Phase 0 is **always** the landing page. The landing page must be built and deployed before any other feature work. This lets Farlen see something real immediately and gives a deployment target for all future phases.

Landing page Phase 0 tasks should include:
- Initialize repo + deploy to Vercel (get live URL)
- Build the landing page (hero, CTA, basic copy)
- Set up domain/DNS if applicable

`test_command` for landing page tasks: `npx playwright test --grep "landing"`

### Update Commands Reference

```bash
STATE=memory/builds/{slug}/state.json

# After completing a task (with optional commit hash)
python3 skills/project-scoper/scripts/update_build_status.py --state-file $STATE --complete-task --commit abc1234

# After completing a phase manually
python3 skills/project-scoper/scripts/update_build_status.py --state-file $STATE --complete-phase

# When blocked by an error (circuit breaker: 2 failures = blocked)
python3 skills/project-scoper/scripts/update_build_status.py --state-file $STATE --blocked "TypeError: Cannot read property 'x' of undefined at auth.ts:42"

# After Farlen unblocks (issue resolved)
python3 skills/project-scoper/scripts/update_build_status.py --state-file $STATE --unblocked

# Set a custom status message (mid-task progress)
python3 skills/project-scoper/scripts/update_build_status.py --state-file $STATE --status "Phase 1 — writing Supabase middleware"

# Set phase gate (waiting for Farlen review)
python3 skills/project-scoper/scripts/update_build_status.py --state-file $STATE --waiting

# Farlen approved — start next phase
python3 skills/project-scoper/scripts/update_build_status.py --state-file $STATE --continue
```

### Full Build Initialization Sequence

After `create_dev_doc.py` runs and returns `block_map` + `phase_task_counts`:

```bash
# 1. Note the page_id and phase_task_counts from create_dev_doc.py output
# 2. Initialize the build
python3 skills/project-scoper/scripts/start_build.py \
  --scope-id <scope-page-id> \
  --dev-doc-id <dev-doc-page-id> \
  --block-map-file memory/builds/{slug}/block-map.json \
  --slug {slug} \
  --phases '[{"name":"Phase 0: Landing Page","task_count":2},{"name":"Phase 1: Core Features","task_count":8}]'
```

This creates `memory/builds/{slug}/state.json` and `projects/{slug}/` with a git repo, and sets Phase 0 to IN PROGRESS in Notion.

## Workflow: List Projects

```bash
python3 skills/project-scoper/scripts/read_scope.py --list
python3 skills/project-scoper/scripts/read_scope.py --status Review
python3 skills/project-scoper/scripts/read_scope.py --status Approved
```

## Status Lifecycle

| Status | Meaning |
|--------|---------|
| Scoping | Derek is researching and building the scope doc |
| Review | Scope is ready for Farlen to review |
| Approved | Farlen approved — ready to build |
| Building | Derek is writing code |
| Shipped | Project is live |
| Rejected | Idea didn't survive scoping |

## Workflow: Check Inbox for Scope Requests

The daily idea email includes a "Scope this" button per idea. When Farlen taps it, it sends an email to YOUR_BUSINESS_EMAIL with subject "Scope: idea-YYYYMMDD-NNN". Derek checks for these:

```bash
python3 skills/project-scoper/scripts/check_inbox.py
```

If requests are found:
1. Read each idea ID from the response
2. Create a scope doc for each: `python3 skills/project-scoper/scripts/create_scope.py --id <idea-id>`
3. Mark the emails as read: `python3 skills/project-scoper/scripts/check_inbox.py --mark-read`
4. Send Farlen the Notion link(s) via Telegram

This can also be checked during the daily cron (after the idea hunt) or when Farlen says "check for scope requests."

## Script Reference

### create_scope.py
Creates a new Notion page in the Project Pipeline database with the full scope template.

| Flag | Required | Description |
|------|----------|-------------|
| `--id` | No* | Idea ID from ideas.json |
| `--title` | No* | Project title (overrides idea) |
| `--problem` | No | Problem statement |
| `--target-user` | No | Who is this for |
| `--solution` | No | What the product does |
| `--features` | No | Pipe-separated MVP features |
| `--tech` | No | Pipe-separated tech stack items |
| `--wireframe` | No | Mermaid diagram string (use `\n` for newlines) |

*Must provide `--id` or `--title`.

### read_scope.py
Reads scope documents from Notion. Returns JSON with properties, sections (with checkbox states), and comments.

| Flag | Description |
|------|-------------|
| `--page-id` | Read a specific page by ID |
| `--title` | Find and read by title (partial match) |
| `--status` | List projects filtered by status |
| `--list` | List all projects |

### create_dev_doc.py
Creates a comprehensive phased development document as a child page of the scope in Notion.
Tasks render as checkboxes (to_do blocks). Each phase has a status callout.

| Flag | Required | Description |
|------|----------|-------------|
| `--scope-id` | Yes | Notion page ID of the approved scope |
| `--from-file` | Yes | Path to dev doc JSON spec (relative to workspace root or absolute) |
| `--slug` | No | Project slug — if set, writes block-map to `memory/builds/{slug}/block-map.json` |

Output: JSON with `page_id`, `url`, `title`, `phase_count`, `block_count`, `block_map`, `phase_task_counts`.

### call_agent.py
Calls Claude API with a prompt file and optional context files. Use for tasks requiring >50 lines of code.

| Flag | Required | Description |
|------|----------|-------------|
| `--prompt-file` | Yes | Path to the prompt markdown file |
| `--context` | No | One or more files/directories to append as context |
| `--output` | No | Write response to this file (default: stdout) |
| `--model` | No | Model ID (default: claude-sonnet-4-6) |

### start_build.py
Initializes the build: creates state.json, git repo, sets Phase 0 IN PROGRESS in Notion. Run once after `create_dev_doc.py`.

| Flag | Required | Description |
|------|----------|-------------|
| `--scope-id` | Yes | Notion page ID of the scope doc |
| `--dev-doc-id` | Yes | Notion page ID of the dev doc page |
| `--block-map-file` | Yes | Path to block-map.json from create_dev_doc.py |
| `--slug` | Yes | Project slug |
| `--phases` | Yes | JSON array: `[{"name":"Phase 0: Landing Page","task_count":2},...]` |

### update_build_status.py
Updates Notion and state.json after task/phase events.

| Flag | Description |
|------|-------------|
| `--complete-task` | Tick current task checkbox, advance counter (auto-completes phase if last task) |
| `--complete-phase` | Mark phase done, set waiting_for_farlen |
| `--blocked REASON` | Set blocked state, update callout to 🚫 |
| `--unblocked` | Clear blocked state, restore IN PROGRESS |
| `--status TEXT` | Set custom status banner text |
| `--waiting` | Phase gate — set waiting_for_farlen without marking phase done |
| `--continue` | Farlen approved — advance to next phase, set it IN PROGRESS |
| `--commit HASH` | Record commit hash (used with --complete-task) |

### check_inbox.py
Checks Derek's Gmail for scope request emails from Farlen.

| Flag | Description |
|------|-------------|
| `--mark-read` | Mark processed emails as read |

Returns JSON: `{"count": N, "requests": [{"message_id": "...", "subject": "...", "idea_id": "..."}]}`

### notion_client.py
Low-level Notion API wrapper. Imported by other scripts. Not called directly.

## Data Locations

| What | Path |
|------|------|
| Notion config | `skills/project-scoper/references/config.json` |
| Notion client | `skills/project-scoper/scripts/notion_client.py` |
| Scope creator | `skills/project-scoper/scripts/create_scope.py` |
| Scope reader | `skills/project-scoper/scripts/read_scope.py` |
| Dev doc creator | `skills/project-scoper/scripts/create_dev_doc.py` |
| Sub-agent caller | `skills/project-scoper/scripts/call_agent.py` |
| Build initializer | `skills/project-scoper/scripts/start_build.py` |
| Build status updater | `skills/project-scoper/scripts/update_build_status.py` |
| Idea database | `memory/idea-hunter/ideas.json` |
| Dev doc specs | `memory/dev-docs/{slug}.json` |
| Build state | `memory/builds/{slug}/state.json` |
| Build block map | `memory/builds/{slug}/block-map.json` |
| Agent prompts | `memory/builds/{slug}/prompts/` |
| Project code | `projects/{slug}/` |
