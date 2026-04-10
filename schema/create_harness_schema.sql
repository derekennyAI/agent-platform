-- Agent Harness Central DB Schema
-- Supabase project: your-project

-- 1. AGENTS — central registry of all agents
CREATE TABLE IF NOT EXISTS agents (
  id SERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  persona TEXT NOT NULL,
  human TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'idle', 'blocked', 'offline', 'decommissioned')),
  model TEXT NOT NULL DEFAULT 'claude-sonnet-4-6',
  timezone TEXT NOT NULL DEFAULT 'America/Los_Angeles',
  telegram_bot TEXT,
  telegram_chat_id TEXT,
  workspace_path TEXT,
  tmux_session TEXT,
  daemon_label TEXT,
  billing_type TEXT DEFAULT 'max' CHECK (billing_type IN ('max', 'pro', 'api')),
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW()
);

-- 2. SCHEDULED_TASKS — replaces scheduled_tasks.json
CREATE TABLE IF NOT EXISTS scheduled_tasks (
  id TEXT PRIMARY KEY,
  agent_name TEXT NOT NULL REFERENCES agents(name),
  schedule TEXT NOT NULL,
  task_description TEXT NOT NULL,
  recurring BOOLEAN DEFAULT true,
  active BOOLEAN DEFAULT true,
  created_by TEXT NOT NULL,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ,
  last_fired_at TIMESTAMPTZ
);

-- 3. SKILLS — catalog of available capabilities
CREATE TABLE IF NOT EXISTS skills (
  id SERIAL PRIMARY KEY,
  name TEXT UNIQUE NOT NULL,
  description TEXT NOT NULL,
  user_description TEXT,
  category TEXT NOT NULL CHECK (category IN ('communication', 'productivity', 'research', 'integration', 'admin', 'finance', 'monitoring')),
  requires_credentials BOOLEAN DEFAULT false,
  credential_services TEXT[] DEFAULT ARRAY[]::TEXT[],
  script_path TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 4. SKILL_PERMISSIONS — which agent can use which skill
CREATE TABLE IF NOT EXISTS skill_permissions (
  id SERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL REFERENCES agents(name),
  skill_name TEXT NOT NULL REFERENCES skills(name),
  granted_by TEXT NOT NULL,
  granted_at TIMESTAMPTZ DEFAULT NOW(),
  revoked_at TIMESTAMPTZ,
  UNIQUE(agent_name, skill_name)
);

-- 5. AGENT_SESSIONS — session metadata
CREATE TABLE IF NOT EXISTS agent_sessions (
  id SERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL REFERENCES agents(name),
  started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  ended_at TIMESTAMPTZ,
  messages_in INTEGER DEFAULT 0,
  messages_out INTEGER DEFAULT 0,
  topics TEXT[] DEFAULT ARRAY[]::TEXT[],
  model_used TEXT,
  ended_reason TEXT CHECK (ended_reason IN ('restart', 'crash', 'context_limit', 'idle_timeout', 'manual'))
);

-- 6. INTERACTION_LOGS — structured metadata from every conversation
CREATE TABLE IF NOT EXISTS interaction_logs (
  id SERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL REFERENCES agents(name),
  session_id INTEGER REFERENCES agent_sessions(id),
  timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  categories TEXT[] DEFAULT ARRAY[]::TEXT[],
  request_summary TEXT,
  outcome TEXT CHECK (outcome IN ('handled', 'stuck', 'partial', 'escalated')),
  skill_used TEXT,
  skill_gap TEXT,
  duration_ms INTEGER,
  satisfaction TEXT CHECK (satisfaction IN ('positive', 'neutral', 'negative')),
  metadata JSONB DEFAULT '{}'::jsonb
);

-- 7. INFRA_EVENTS — centralized infra logging
CREATE TABLE IF NOT EXISTS infra_events (
  id SERIAL PRIMARY KEY,
  timestamp TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  level TEXT NOT NULL CHECK (level IN ('INFO', 'WARNING', 'ERROR', 'CRITICAL')),
  component TEXT NOT NULL,
  trace_id TEXT,
  message TEXT NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb
);

-- 8. AGENT_CREDENTIALS — vault for scoped secrets
CREATE TABLE IF NOT EXISTS agent_credentials (
  id SERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL,
  service TEXT NOT NULL,
  credential_key TEXT NOT NULL,
  credential_value TEXT NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(agent_name, service, credential_key)
);

-- 9. AGENT_STATE — idempotency markers and state tracking (Phase 3)
CREATE TABLE IF NOT EXISTS agent_state (
  id SERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL,
  key TEXT NOT NULL,
  value JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ DEFAULT NOW(),
  UNIQUE(agent_name, key)
);

-- 10. AGENT_ANALYTICS — usage and event logs (Phase 4)
CREATE TABLE IF NOT EXISTS agent_analytics (
  id SERIAL PRIMARY KEY,
  agent_name TEXT NOT NULL,
  event TEXT NOT NULL,
  metadata JSONB DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ DEFAULT NOW()
);

-- 11. ADMIN_TASKS — inter-agent task queue
CREATE TABLE IF NOT EXISTS admin_tasks (
  id UUID DEFAULT gen_random_uuid() PRIMARY KEY,
  agent_name TEXT NOT NULL,
  created_by TEXT NOT NULL,
  task_description TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'completed', 'failed')),
  result TEXT,
  created_at TIMESTAMPTZ DEFAULT NOW(),
  completed_at TIMESTAMPTZ
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_agent ON scheduled_tasks(agent_name);
CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_active ON scheduled_tasks(active) WHERE active = true;
CREATE INDEX IF NOT EXISTS idx_skill_permissions_agent ON skill_permissions(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_sessions_agent ON agent_sessions(agent_name);
CREATE INDEX IF NOT EXISTS idx_interaction_logs_agent ON interaction_logs(agent_name);
CREATE INDEX IF NOT EXISTS idx_interaction_logs_ts ON interaction_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_infra_events_level ON infra_events(level);
CREATE INDEX IF NOT EXISTS idx_infra_events_component ON infra_events(component);
CREATE INDEX IF NOT EXISTS idx_infra_events_ts ON infra_events(timestamp);
CREATE INDEX IF NOT EXISTS idx_agent_credentials_lookup ON agent_credentials(agent_name, service);
CREATE INDEX IF NOT EXISTS idx_agent_state_agent ON agent_state(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_analytics_agent ON agent_analytics(agent_name);
CREATE INDEX IF NOT EXISTS idx_agent_analytics_ts ON agent_analytics(created_at);
CREATE INDEX IF NOT EXISTS idx_admin_tasks_agent ON admin_tasks(agent_name, status);
