# Roadmap

## Phase 1 — GitHub Backup (Done)
All platform code, configs, templates, and skills pushed to a forkable GitHub repo.

## Phase 2 — Scheduled Tasks to Supabase
Move scheduled tasks from the local `scheduled_tasks.json` file into a `scheduled_tasks` Supabase table. Update `scheduler_executor.sh` to read from Supabase instead of the local file. This means automations survive machine migrations automatically.

**Table schema** (add to `schema/`):
- `id` (text, primary key)
- `agent_name` (text)
- `schedule` (text — cron expression)
- `task_description` (text)
- `isolated` (boolean)
- `timeout_minutes` (integer)
- `enabled` (boolean)
- `created_at` / `updated_at` (timestamptz)

## Phase 3 — State Files to Supabase
Move small state files into Supabase. These track "what already happened" so agents don't repeat themselves:
- Lead tracking state (which leads were already processed)
- Usage alert state (which alerts were already sent)
- Reminder state (which reminders already fired)
- Any other idempotency markers

**Table**: `agent_state` with columns `agent_name`, `key`, `value` (jsonb), `updated_at`.

## Phase 4 — Analytics to Supabase
Sync analytics/usage logs so you can see trends across all agents even if the host machine is offline. Lower priority since this is historical data, not operational.

**Table**: `agent_analytics` with columns `agent_name`, `event`, `metadata` (jsonb), `created_at`.

## Phase 5 — Docker Containerization (Future)
True filesystem sandboxing via Docker containers per agent. Each agent runs in its own container with only its workspace mounted. Currently agents rely on convention-based workspace isolation (`~/agent-name/`); Docker makes it enforced.

## Phase 6 — Multi-Machine Support (Future)
With all state in Supabase and code on GitHub, running agents on multiple machines becomes possible. Needs: shared credential vault, distributed scheduler, agent routing.
