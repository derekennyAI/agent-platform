# Startup Instructions

You just started via the persistent daemon (launchd + tmux). Do the following:

1. Send a Telegram message to your human's chat_id: "Back online." (Keep it short.)
2. Read your memory files to get context on who you are and what's going on.
3. Adopt your persona from memory.
4. **Scheduled tasks are persistent in Supabase** — they survive restarts. Use these MCP tools to manage them:
   - `list_scheduled_tasks` — see all your crons
   - `schedule_task` — add a new recurring task
   - `update_scheduled_task` / `delete_scheduled_task` — modify or remove
   - The scheduler executor runs every minute via system crontab and fires due tasks into your tmux session.
   - Do NOT re-register crons on startup — they already exist in the database.
5. **State is persistent in Supabase** with local fallback. Use these MCP tools:
   - `get_state` / `set_state` / `delete_state` / `list_state`
   - Do NOT read/write local state files directly — the MCP tools handle write-through caching.
6. Check for pending admin_tasks assigned to you via `list_admin_tasks`.
7. Env vars are in your daemon plist: ANTHROPIC_API_KEY, SUPABASE_URL, SUPABASE_SERVICE_KEY (plus any service-specific keys).

After completing these steps, you're ready. Wait for messages.
