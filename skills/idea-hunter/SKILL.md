---
name: idea-hunter
description: "Scrape Reddit for product and SaaS ideas, score them on a 5-dimension rubric, store in Supabase, and send weekly digests via email. Triggered by: 'find me ideas', 'idea hunt', 'run idea hunter', 'scrape reddit for ideas', 'what should I build', 'product ideas', 'SaaS ideas'. Also runs automatically via weekly Monday cron."
---

# Idea Hunter Skill

Automated pipeline for discovering, scoring, and tracking product/SaaS ideas from Reddit. All ideas stored in Supabase with full CRUD support.

## Overview

- **Source**: Reddit public JSON endpoints (free, no API key)
- **Storage**: Supabase `ideas` table in personal-tools project (qvomfdbkxhoiigilyzjk) — uses PERSONAL_SUPABASE_URL/PERSONAL_SUPABASE_KEY env vars
- **Scoring**: 5-dimension weighted rubric (demand, simplicity, timing, moat, founder fit)
- **Output**: HTML email digest with "Scope this" buttons + short Telegram notification

## Weekly Hunt Workflow (Monday cron)

When triggered by the weekly cron job, follow these steps exactly:

### 1. Pick targets
Read `skills/idea-hunter/references/subreddits.md` and select:
- 3 primary subreddits (rotate through the list — don't repeat yesterday's picks)
- 2 secondary subreddits (random)
- 2–3 search terms from different categories

### 2. Scrape Reddit
For each subreddit+term combination, run:
```bash
python3 skills/idea-hunter/scripts/scrape_reddit.py --subreddit <name> --search "<term>" --limit 15 --comments 3 --save
```
Also do one broad scrape per primary subreddit:
```bash
python3 skills/idea-hunter/scripts/scrape_reddit.py --subreddit <name> --sort hot --limit 20 --save
```

### 3. Review all existing ideas
**BEFORE analyzing scrape results**, fetch and review all existing ideas from Supabase so you know what's already in the database:
```bash
python3 skills/idea-hunter/scripts/manage_ideas.py list --top 100
```
Read through the titles and problems carefully. Keep these in mind during analysis — do NOT add ideas that overlap with what already exists.

### 4. Analyze results
Read the JSON output from each scrape. Look for:
- Posts describing pain points, frustrations, or unmet needs
- Posts asking for tools/solutions that don't exist
- Posts about manual processes that could be automated
- Posts with high engagement (upvotes + comments) around a problem

For each promising post, extract:
- A concise **title** for the idea
- The **problem** being described
- A potential **solution** (SaaS/tool/product)
- **Evidence**: the source URL, a key quote, and upvote count
- Relevant **tags**

### 5. Check each candidate for overlap
Before adding any idea, run the overlap checker against the existing database:
```bash
python3 skills/idea-hunter/scripts/manage_ideas.py check \
  --title "Candidate idea name" \
  --problem "The problem it addresses"
```
If `"overlap": true`, **skip the idea** — it's too similar to something already tracked. Only proceed to add ideas where `"overlap": false`.

### 6. Add new ideas
For each new idea that passed the overlap check:
```bash
python3 skills/idea-hunter/scripts/manage_ideas.py add \
  --title "Short idea name" \
  --problem "The pain point in one sentence" \
  --solution "What the product would do" \
  --evidence '[{"source": "reddit", "url": "https://...", "quote": "relevant quote", "upvotes": 42}]' \
  --tags "saas,category"
```

### 7. Score new ideas
For each newly added idea, score it using the rubric in `skills/idea-hunter/references/scoring.md`:
```bash
python3 skills/idea-hunter/scripts/manage_ideas.py score \
  --id <idea-id> --demand 7 --simplicity 8 --timing 6 --moat 5 --fit 7
```

### 8. Generate and send digest
Send a formatted HTML email to Farlen. Each idea has a "Scope this" button that emails YOUR_BUSINESS_EMAIL with the idea ID.
```bash
# HTML email with scope buttons
python3 skills/idea-hunter/scripts/format_digest.py --days 1 | \
  python3 skills/idea-hunter/scripts/send_email.py \
    --to "YOUR_ADMIN_EMAIL" --subject "🔍 Idea Hunt — $(date +%B\ %d,\ %Y)" --stdin --html
```
Send a short Telegram notification (NOT the full digest — ideas are email-only):
```
"Sent today's idea hunt to your email — X new ideas. Tap 'Scope this' on any idea to kick off a project."
```

### 9. Check for scope requests
After sending the digest, check if Farlen has previously requested any scopes via the email button:
```bash
python3 skills/project-scoper/scripts/check_inbox.py
```
If scope requests are found, process them per the project-scoper skill workflow.

## Manual Hunt Workflow

When Farlen says "find me ideas about X" or "hunt for ideas in <topic>":

1. Search relevant subreddits for the topic:
   ```bash
   python3 skills/idea-hunter/scripts/scrape_reddit.py --subreddit SaaS --search "<topic>" --limit 20 --comments 5
   ```
2. Analyze results, extract ideas
3. Add and score them
4. Present findings directly (no need for digest format)

## Script Reference

### scrape_reddit.py
Fetches Reddit posts via public JSON endpoints. Stdlib only.
```bash
# Browse a subreddit
python3 skills/idea-hunter/scripts/scrape_reddit.py --subreddit SaaS --sort hot --limit 25

# Search within a subreddit
python3 skills/idea-hunter/scripts/scrape_reddit.py --subreddit startups --search "I wish there was" --limit 10

# With comments and save raw data
python3 skills/idea-hunter/scripts/scrape_reddit.py --subreddit SaaS --sort hot --limit 10 --comments 5 --save
```

Options:
- `--subreddit, -s`: Subreddit name (required)
- `--search, -q`: Search query
- `--sort`: hot, new, top, relevance (default: hot)
- `--time, -t`: hour, day, week, month, year, all (default: week)
- `--limit, -l`: Max posts (default: 25)
- `--comments, -c`: Fetch top N comments per post (default: 0)
- `--save`: Save raw JSON to `memory/idea-hunter/raw/`

### manage_ideas.py
Full CRUD on the ideas database.

```bash
# Add a new idea
python3 skills/idea-hunter/scripts/manage_ideas.py add \
  --title "Auto-invoice SaaS" \
  --problem "Freelancers waste time on manual invoicing" \
  --solution "AI reads time logs, generates and sends invoices" \
  --evidence '[{"source": "reddit", "url": "...", "quote": "...", "upvotes": 89}]' \
  --tags "saas,freelance,invoicing"

# Score an idea (1–10 per dimension)
python3 skills/idea-hunter/scripts/manage_ideas.py score \
  --id idea-20260304-001 --demand 8 --simplicity 7 --timing 6 --moat 4 --fit 8

# List ideas (with filters)
python3 skills/idea-hunter/scripts/manage_ideas.py list --status scored --top 10
python3 skills/idea-hunter/scripts/manage_ideas.py list --since 7d

# Update an idea
python3 skills/idea-hunter/scripts/manage_ideas.py update --id idea-20260304-001 --status selected --notes "Worth exploring"

# Archive an idea
python3 skills/idea-hunter/scripts/manage_ideas.py archive --id idea-20260304-001 --reason "Already exists as product"

# Find duplicates
python3 skills/idea-hunter/scripts/manage_ideas.py dedup

# Generate daily digest (markdown)
python3 skills/idea-hunter/scripts/manage_ideas.py digest --days 1

# Export full database to markdown
python3 skills/idea-hunter/scripts/manage_ideas.py export --format md
```

### scaffold_project.py
Create project boilerplate from a selected idea.
```bash
python3 skills/idea-hunter/scripts/scaffold_project.py --id idea-20260304-001
python3 skills/idea-hunter/scripts/scaffold_project.py --id idea-20260304-001 --output ~/projects
```

Creates: README.md, .gitignore, docs/PRD.md, docs/VALIDATION.md, src/, tests/

## CRUD Commands Quick Reference

| Command | What it does |
|---------|-------------|
| `check` | Check if a candidate idea overlaps with existing ideas (run BEFORE add) |
| `add` | Create new idea with title, problem, solution, evidence, tags |
| `score` | Set 5 dimension scores, auto-calculates weighted total |
| `list` | Filter by status, recency, top N — sorted by score |
| `update` | Change status, notes, title, tags on existing idea |
| `archive` | Mark idea as archived with reason |
| `dedup` | Find similar ideas by fuzzy title/problem matching |
| `digest` | Markdown summary of recent top ideas (for Telegram) |
| `export` | Export full database to markdown |

## Cron Setup

Registered via Claude Code daemon CronCreate on every startup:
- **Schedule**: Every Monday at 6:03 AM PT
- **Workflow**: Runs the Weekly Hunt Workflow above

## Reviewing & Acting on Ideas

### Promote an idea
```
update --id <id> --status selected --notes "Why it's worth pursuing"
```

### Scaffold a project
```bash
python3 skills/idea-hunter/scripts/scaffold_project.py --id <id>
```
This creates a project directory with README, PRD, and validation plan. After review, Derek can `git init` and push to GitHub when a PAT is configured.

### Archive bad ideas
```
archive --id <id> --reason "Already exists" / "Too complex" / "No demand"
```

## Data Locations

| What | Path |
|------|------|
| Idea database | Supabase `ideas` table (env: SUPABASE_URL + SUPABASE_SERVICE_KEY) |

| Raw scrape data | `memory/idea-hunter/raw/` |
| Scoring rubric | `skills/idea-hunter/references/scoring.md` |
| Subreddit list | `skills/idea-hunter/references/subreddits.md` |
