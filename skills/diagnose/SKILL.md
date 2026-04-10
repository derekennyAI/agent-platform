---
name: diagnose
description: Sweep all Lean Intelligence platform systems for errors and investigate issues. Use when the user reports a bug, wants a health check, says something is broken, or wants to check system status. Triggers on "diagnose", "health check", "what's broken", "check systems", "any errors", "platform status", "/diagnose".
---


Platform Diagnostics — Lean Intelligence
Systematically check every layer of the stack for errors, then investigate root causes.

Stack
System
Environment
Frontend (prod)
ai.leanmarketing.com
Frontend (dev)
lean-intelligence-dev.up.railway.app
n8n (prod)
primary-production-cea5.up.railway.app
n8n (dev)
primary-development-c302.up.railway.app
Supabase (prod)
jwehqqyzlbpolkfkjhai.supabase.co
Railway
CLI linked to "Lean Intelligence Front End" project

Secrets
All API keys are in ~/.claude/secrets.json. Read this file first to get:
 • toggl_api_token / toggl_workspace_id
 • wave_access_token
 • wise_api_token
n8n API key: use curl with the key from the n8n MCP server config (check ~/.claude/settings/mcp.json or .mcp.json for the N8N_API_KEY env var). If unavailable, fall back to the mcp__n8n__n8n_executions tool.

Arguments
 • /diagnose — full sweep of all systems (last 24h)
 • /diagnose quick — health checks only (are things up, any active incidents, any failed queues)
 • /diagnose n8n — deep dive on n8n executions and workflow errors
 • /diagnose supabase — deep dive on logs, failed queues, constraint issues
 • /diagnose linear — open bugs, unassigned issues
 • /diagnose railway — deploy logs, service status
 • /diagnose "<issue description>" — targeted investigation of a specific problem

Workflow

Step 1: Determine scope
Parse the argument to decide which checks to run:
 • No argument or full → run all checks
 • quick → health checks only (steps 2a, 2b, 2c)
 • System name → deep dive that system only
 • Quoted text → targeted investigation mode

Step 2: Sweep systems
Run these checks. Use parallel tool calls where possible.

2a. Frontend health
curl -s -o /dev/null -w "%{http_code} %{time_total}s" https://ai.leanmarketing.com
curl -s -o /dev/null -w "%{http_code} %{time_total}s" https://lean-intelligence-dev.up.railway.app

Mark as critical if non-200. Include response time.

2b. Supabase errors
Use mcp__supabase__execute_sql:
-- Error logs (last 24h)
SELECT id, created_at, client_id, error_category, error_message
FROM logs
WHERE created_at > NOW() - INTERVAL '24 hours'
ORDER BY created_at DESC;

-- Failed generation queue entries (last 7 days)
SELECT id, client_id, writer_id, writer_name, status, error_message, created_at
FROM generation_queue
WHERE status = 'failed' AND created_at > NOW() - INTERVAL '7 days'
ORDER BY created_at DESC;

-- Stuck processing entries (started > 10 min ago, still processing)
SELECT id, client_id, writer_id, status, created_at
FROM generation_queue
WHERE status = 'processing' AND created_at < NOW() - INTERVAL '10 minutes'
ORDER BY created_at DESC;


2c. Grafana incidents & alerts
Use MCP tools:
 • mcp__grafana__list_incidents with status: "active"
 • mcp__grafana__list_alert_groups with state: "new"

2d. n8n failed executions
Use mcp__n8n__n8n_executions tool:
action: "list", status: "error", limit: 10

If the MCP tool returns empty (API key scoped to wrong project), fall back to curl:
N8N_KEY="<from secrets>"
curl -s -H "X-N8N-API-KEY: $N8N_KEY" \
  "https://primary-production-cea5.up.railway.app/api/v1/executions?status=error&limit=10"

For each error execution, get details:
action: "get", id: "<execution_id>", mode: "error"


2e. Linear open bugs
Use mcp__linear__list_issues:
 • team: "Lean Marketing", state: "Backlog" — unresolved bugs
 • team: "Lean Marketing", state: "In Progress" — active bugs
 • Filter for issues with "Bug" or "Error" in title, or created by operations@leanmarketing.com (n8n auto-created)

2f. Railway deploy status
railway logs --service "Lean Intelligence | PROD" -n 50 2>&1 | grep -i -E "error|fail|crash|exit|killed"
railway logs --service "Lean Intelligence | DEV" -n 50 2>&1 | grep -i -E "error|fail|crash|exit|killed"


Step 3: Report
Format findings as a markdown table:
## Platform Health Report — {YYYY-MM-DD}

### Status
| System | Status | Details |
|--------|--------|---------|
| Frontend (prod) | ✅ OK | 200 in 0.24s |
| Frontend (dev) | ✅ OK | 200 in 0.31s |
| n8n | ⚠️ 2 errors | 2 failed executions in last 24h |
| Supabase | ✅ OK | No error logs |
| Grafana | ✅ OK | No active incidents |
| Linear | ℹ️ 3 bugs | 3 open bugs in backlog |
| Railway | ✅ OK | No errors in recent logs |

Status icons:
 • ✅ — no issues
 • ⚠️ — warnings (non-critical errors, old bugs)
 • ❌ — critical (frontend down, active incidents, recent failures)
 • ℹ️ — informational (open bugs, pending items)

Step 4: Investigate (if critical issues found)
For each critical/warning issue, automatically dig deeper:
n8n execution error:
 1 Get execution details with mode: "error" to find the failing node
 2 Check if the workflow is active
 3 Check if the webhook path is registered
 4 Cross-reference with Supabase logs table for the same client_id/time
Supabase error log:
 1 Check the client_id — is it a known test client or real user?
 2 Check generation_queue for related entries
 3 Check client_files to see if uploads completed
 4 Check if the error category maps to a known workflow
Frontend down:
 1 Check Railway deploy logs for build errors
 2 Check recent git commits for breaking changes
 3 Check if Supabase is responding (the frontend depends on it)
Grafana alert firing:
 1 Get the alert rule details
 2 Check the underlying metric/query
 3 Cross-reference with other systems

Step 5: Recommended actions
Based on findings, suggest concrete next steps:
 • "Re-run failed n8n execution for client X"
 • "Check n8n workflow Y — node Z is failing consistently"
 • "Supabase logs show repeated invalid_file_type for client X — check uploaded file format"
 • "Linear bug LEA-XX has been in backlog for 2 weeks — assign or close"

Deep Dive Modes

/diagnose n8n
 1 List ALL error executions (last 7 days, not just 24h)
 2 Group by workflow — which workflows fail most?
 3 For each failed workflow, get the error node and message
 4 Check if any workflows are inactive that should be active
 5 List all active webhooks and verify they respond (quick 404 check)

/diagnose supabase
 1 Full logs table scan (last 7 days)
 2 All failed generation_queue entries with client context
 3 Check for orphaned data (clients with no files, files with no profile, etc.)
 4 Check RLS policies on key tables
 5 Check for constraint issues (like the CHECK constraint we hit)

/diagnose "<issue>"
Targeted investigation mode:
 1 Parse the issue description for keywords (client name, error code, page name, etc.)
 2 Search across all systems for matching data
 3 Build a timeline of events
 4 Identify root cause
 5 Suggest fix

Notes
 • The n8n MCP (mcp__n8n__) connects to prod n8n but may only see workflows in one project. Always fall back to curl with the API key if MCP returns empty.
 • Grafana Loki has no logs for the Next.js app (it doesn't ship to Grafana). Use Railway CLI for frontend logs.
 • The logs table in Supabase is populated by n8n workflows, not by the Next.js app. Frontend errors are only visible in Railway logs or browser console.
 • Supabase dev is a branch of prod (dhnepxatcvaaxfuelqrq). Errors on dev won't show in prod queries.
