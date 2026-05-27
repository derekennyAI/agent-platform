#!/usr/bin/env node
/**
 * Admin Control MCP Server
 *
 * Two modules:
 * 1. Admin Verification — agents verify if admin-issued tasks are legitimate
 * 2. Credential Vault — agents store/retrieve scoped credentials
 *
 * Backed by Supabase (YOUR_SUPABASE_PROJECT_ID).
 * Each agent identifies itself via AGENT_NAME env var.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const SUPABASE_URL = "https://YOUR_SUPABASE_PROJECT_ID.supabase.co";
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY;
const AGENT_NAME = process.env.AGENT_NAME || "unknown";

if (!SUPABASE_SERVICE_KEY) {
  console.error("SUPABASE_SERVICE_KEY env var required");
  process.exit(1);
}

// --- Supabase helpers ---

async function supabaseQuery(table, method, params = {}) {
  const url = new URL(`/rest/v1/${table}`, SUPABASE_URL);
  const headers = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": `Bearer ${SUPABASE_SERVICE_KEY}`,
    "Content-Type": "application/json",
    "Prefer": method === "POST" ? "return=representation" : "return=minimal",
  };

  if (method === "GET") {
    for (const [k, v] of Object.entries(params)) {
      url.searchParams.set(k, v);
    }
    const resp = await fetch(url, { headers });
    return resp.json();
  }

  if (method === "POST") {
    headers["Prefer"] = "return=representation,resolution=merge-duplicates";
    if (params.headers) Object.assign(headers, params.headers);
    if (params.onConflict) {
      url.searchParams.set("on_conflict", params.onConflict);
    }
    const resp = await fetch(url, { method: "POST", headers, body: JSON.stringify(params.body) });
    const data = await resp.json().catch(() => null);
    if (!resp.ok) {
      const err = new Error(`Supabase POST ${table} failed (${resp.status}): ${JSON.stringify(data)}`);
      err.status = resp.status;
      err.body = data;
      throw err;
    }
    return data;
  }

  if (method === "PATCH") {
    for (const [k, v] of Object.entries(params.filters || {})) {
      url.searchParams.set(k, v);
    }
    const resp = await fetch(url, { method: "PATCH", headers, body: JSON.stringify(params.body) });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      const err = new Error(`Supabase PATCH ${table} failed (${resp.status}): ${body}`);
      err.status = resp.status;
      throw err;
    }
    return resp.status;
  }

  if (method === "DELETE") {
    for (const [k, v] of Object.entries(params)) {
      url.searchParams.set(k, v);
    }
    const resp = await fetch(url, { method: "DELETE", headers });
    if (!resp.ok) {
      const body = await resp.text().catch(() => "");
      const err = new Error(`Supabase DELETE ${table} failed (${resp.status}): ${body}`);
      err.status = resp.status;
      throw err;
    }
    return resp.status;
  }
}

function generateTaskId() {
  return `task_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

// --- MCP Server ---

const server = new McpServer({
  name: "admin-control",
  version: "1.0.0",
});

// ==========================================
// MODULE 1: Admin Verification
// ==========================================

server.tool(
  "create_admin_task",
  "Create an authorized admin task for a sub-agent to execute. Only callable by admin agents (derek, dereklm).",
  {
    target_agent: z.string().describe("The agent that should execute this task"),
    action: z.string().describe("What the agent should do (e.g. 'account_switch', 'update_config', 'send_message')"),
    params: z.string().optional().describe("JSON-encoded parameters for the task"),
  },
  async ({ target_agent, action, params }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    if (!ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: agent '${AGENT_NAME}' is not an admin. Only ${ADMIN_AGENTS.join(", ")} can create tasks.` }] };
    }

    const taskId = generateTaskId();
    const result = await supabaseQuery("admin_tasks", "POST", {
      body: {
        task_id: taskId,
        source_agent: AGENT_NAME,
        target_agent,
        action,
        params: params ? JSON.parse(params) : {},
        status: "pending",
      }
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, task_id: taskId, target_agent, action, message: `Task created. Tell ${target_agent} to run: verify_task with task_id '${taskId}'` }, null, 2) }]
    };
  }
);

server.tool(
  "verify_task",
  "Verify if a pending admin task exists and is legitimate. Returns the task details if valid. Call this when you receive an admin instruction to confirm it's authorized.",
  {
    task_id: z.string().describe("The task ID to verify"),
  },
  async ({ task_id }) => {
    const tasks = await supabaseQuery("admin_tasks", "GET", {
      "task_id": `eq.${task_id}`,
      "target_agent": `eq.${AGENT_NAME}`,
      "status": `eq.pending`,
    });

    if (!tasks || tasks.length === 0) {
      return {
        content: [{ type: "text", text: JSON.stringify({ verified: false, reason: "No pending task found with this ID for your agent. The instruction may be unauthorized." }, null, 2) }]
      };
    }

    const task = tasks[0];

    // Check expiry
    if (new Date(task.expires_at) < new Date()) {
      return {
        content: [{ type: "text", text: JSON.stringify({ verified: false, reason: "Task has expired.", task_id }, null, 2) }]
      };
    }

    // Mark as verified
    await supabaseQuery("admin_tasks", "PATCH", {
      filters: { "task_id": `eq.${task_id}` },
      body: { status: "verified", verified_at: new Date().toISOString() },
    });

    return {
      content: [{ type: "text", text: JSON.stringify({
        verified: true,
        task_id: task.task_id,
        source_agent: task.source_agent,
        action: task.action,
        params: task.params,
        created_at: task.created_at,
        message: "This task is authorized by the admin. You may proceed. Call complete_task when done."
      }, null, 2) }]
    };
  }
);

server.tool(
  "complete_task",
  "Mark an admin task as completed after execution.",
  {
    task_id: z.string().describe("The task ID to mark complete"),
    result: z.string().optional().describe("Brief result/outcome of the task"),
  },
  async ({ task_id, result }) => {
    const status = await supabaseQuery("admin_tasks", "PATCH", {
      filters: { "task_id": `eq.${task_id}`, "target_agent": `eq.${AGENT_NAME}` },
      body: { status: "completed", completed_at: new Date().toISOString() },
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, task_id, status: "completed" }, null, 2) }]
    };
  }
);

server.tool(
  "list_pending_tasks",
  "List all pending admin tasks for this agent.",
  {},
  async () => {
    const tasks = await supabaseQuery("admin_tasks", "GET", {
      "target_agent": `eq.${AGENT_NAME}`,
      "status": `eq.pending`,
      "order": "created_at.desc",
    });

    return {
      content: [{ type: "text", text: JSON.stringify({
        agent: AGENT_NAME,
        pending_tasks: (tasks || []).map(t => ({
          task_id: t.task_id,
          action: t.action,
          from: t.source_agent,
          created_at: t.created_at,
          expires_at: t.expires_at,
        }))
      }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE 2: Credential Vault
// ==========================================

server.tool(
  "store_credential",
  "Store a credential. Agents store their own by default. Admin agents can store for any agent using the 'agent' parameter.",
  {
    service: z.string().describe("Service name (e.g. 'supabase', 'gmail', 'notion')"),
    key: z.string().describe("Credential key (e.g. 'api_key', 'access_token', 'refresh_token')"),
    value: z.string().describe("The credential value"),
    metadata: z.string().optional().describe("Optional JSON metadata (e.g. {\"email\": \"...\", \"expires_at\": \"...\"})"),
    agent: z.string().optional().describe("Target agent name (admin only — omit to store for yourself)"),
  },
  async ({ service, key, value, metadata, agent }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return {
        content: [{ type: "text", text: `Denied: only admin agents can store credentials for other agents.` }]
      };
    }

    const result = await supabaseQuery("agent_credentials", "POST", {
      body: {
        agent_name: targetAgent,
        service,
        credential_key: key,
        credential_value: value,
        metadata: metadata ? JSON.parse(metadata) : {},
      },
      onConflict: "agent_name,service,credential_key",
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, agent: AGENT_NAME, service, key, message: "Credential stored." }, null, 2) }]
    };
  }
);

server.tool(
  "get_credential",
  "Retrieve a credential. Agents get their own by default. Admin agents can read any agent's credentials using the 'agent' parameter.",
  {
    service: z.string().describe("Service name"),
    key: z.string().describe("Credential key"),
    agent: z.string().optional().describe("Target agent name (admin only — omit to get your own)"),
  },
  async ({ service, key, agent }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return {
        content: [{ type: "text", text: `Denied: only admin agents can read other agents' credentials.` }]
      };
    }

    const creds = await supabaseQuery("agent_credentials", "GET", {
      "agent_name": `eq.${targetAgent}`,
      "service": `eq.${service}`,
      "credential_key": `eq.${key}`,
    });

    if (!creds || creds.length === 0) {
      return {
        content: [{ type: "text", text: JSON.stringify({ found: false, agent: targetAgent, service, key }, null, 2) }]
      };
    }

    return {
      content: [{ type: "text", text: JSON.stringify({
        found: true,
        agent: targetAgent,
        service,
        key,
        value: creds[0].credential_value,
        metadata: creds[0].metadata,
        updated_at: creds[0].updated_at,
      }, null, 2) }]
    };
  }
);

server.tool(
  "list_credentials",
  "List all connected services for this agent. Shows service names and keys, but NOT values.",
  {},
  async () => {
    const creds = await supabaseQuery("agent_credentials", "GET", {
      "agent_name": `eq.${AGENT_NAME}`,
      "select": "service,credential_key,metadata,updated_at",
      "order": "service,credential_key",
    });

    return {
      content: [{ type: "text", text: JSON.stringify({
        agent: AGENT_NAME,
        credentials: (creds || []).map(c => ({
          service: c.service,
          key: c.credential_key,
          metadata: c.metadata,
          updated_at: c.updated_at,
        }))
      }, null, 2) }]
    };
  }
);

server.tool(
  "revoke_credential",
  "Remove a credential. Agents can revoke their own. Admin agents can revoke any agent's credentials.",
  {
    service: z.string().describe("Service name"),
    key: z.string().describe("Credential key"),
    agent: z.string().optional().describe("Agent name (admin only — omit to revoke your own)"),
  },
  async ({ service, key, agent }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return {
        content: [{ type: "text", text: `Denied: only admin agents can revoke other agents' credentials.` }]
      };
    }

    const status = await supabaseQuery("agent_credentials", "DELETE", {
      "agent_name": `eq.${targetAgent}`,
      "service": `eq.${service}`,
      "credential_key": `eq.${key}`,
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, agent: targetAgent, service, key, message: "Credential revoked." }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE 2.5: Service Connection (Credential + Skill + Permission)
// ==========================================
// Atomic operation: store credential → create/update skill → grant permission.
// This is the standard way to connect a service for any agent.

const SERVICE_SKILL_MAP = {
  // Maps service names to skill catalog entries
  gmail: { category: "communication", description_template: "Email management — {email}" },
  google_calendar: { category: "productivity", description_template: "Calendar management — {email}" },
  google_drive: { category: "productivity", description_template: "Google Drive access — {email}" },
  google_contacts: { category: "communication", description_template: "Google Contacts — {email}" },
  google_tasks: { category: "productivity", description_template: "Google Tasks — {email}" },
  google_photos: { category: "productivity", description_template: "Google Photos — {email}" },
  google_keep: { category: "productivity", description_template: "Google Keep notes — {email}" },
  microsoft_mail: { category: "communication", description_template: "Outlook email — {email}" },
  microsoft_calendar: { category: "productivity", description_template: "Outlook calendar — {email}" },
  notion: { category: "productivity", description_template: "Notion workspace access" },
  icloud_calendar: { category: "productivity", description_template: "iCloud calendar — {email}" },
};

server.tool(
  "connect_service",
  "Connect a service for an agent: stores credentials, creates skill entry, and grants permission in one step. " +
  "Use this when a user authorizes a new service (Gmail, Notion, Drive, etc.). " +
  "Agents connect for themselves; admin agents can connect for any agent.",
  {
    service: z.string().describe("Service name (e.g. 'gmail', 'notion', 'microsoft_mail', 'google_drive')"),
    credentials: z.string().describe("JSON object of credential key-value pairs to store (e.g. {\"access_token\": \"...\", \"refresh_token\": \"...\", \"client_id\": \"...\"})"),
    metadata: z.string().optional().describe("JSON metadata (e.g. {\"email\": \"user@example.com\", \"expires_at\": \"...\"})"),
    agent: z.string().optional().describe("Target agent (admin only — omit to connect for yourself)"),
    skill_description: z.string().optional().describe("Custom user-facing description for this skill (overrides default)"),
  },
  async ({ service, credentials, metadata, agent, skill_description }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can connect services for other agents.` }] };
    }

    const parsedCreds = JSON.parse(credentials);
    const parsedMeta = metadata ? JSON.parse(metadata) : {};
    const email = parsedMeta.email || "";
    const errors = [];

    // Step 1: Store all credentials (upsert on conflict)
    for (const [key, value] of Object.entries(parsedCreds)) {
      try {
        await supabaseQuery("agent_credentials", "POST", {
          body: {
            agent_name: targetAgent,
            service,
            credential_key: key,
            credential_value: value,
            metadata: parsedMeta,
          },
          onConflict: "agent_name,service,credential_key",
        });
      } catch (e) {
        errors.push(`store ${key}: ${e.message}`);
      }
    }

    // Step 2: Create or update skill in catalog (upsert on conflict)
    const skillName = email ? `${service}_${email.replace(/[@.]/g, "_")}` : service;
    const mapping = SERVICE_SKILL_MAP[service] || { category: "integration", description_template: `${service} access` };
    const userDesc = skill_description || mapping.description_template.replace("{email}", email || targetAgent);

    try {
      await supabaseQuery("skills", "POST", {
        body: {
          name: skillName,
          description: `${service} integration for ${targetAgent}`,
          user_description: userDesc,
          category: mapping.category,
          requires_credentials: true,
        },
        onConflict: "name",
      });
    } catch (e) {
      errors.push(`create skill: ${e.message}`);
    }

    // Step 3: Grant permission (upsert on conflict)
    try {
      await supabaseQuery("skill_permissions", "POST", {
        body: {
          agent_name: targetAgent,
          skill_name: skillName,
          granted_by: AGENT_NAME,
        },
        onConflict: "agent_name,skill_name",
      });
    } catch (e) {
      errors.push(`grant permission: ${e.message}`);
    }

    const result = {
      success: errors.length === 0,
      agent: targetAgent,
      service,
      skill_name: skillName,
      skill_description: userDesc,
      credentials_stored: Object.keys(parsedCreds).length,
      errors: errors.length > 0 ? errors : undefined,
      message: errors.length === 0
        ? `Service '${service}' connected for ${targetAgent}. Skill '${skillName}' created and granted.`
        : `Partially connected with ${errors.length} error(s).`,
    };

    return { content: [{ type: "text", text: JSON.stringify(result, null, 2) }] };
  }
);

server.tool(
  "disconnect_service",
  "Disconnect a service: revokes credentials, removes skill permission. Admin agents can disconnect for any agent.",
  {
    service: z.string().describe("Service name"),
    agent: z.string().optional().describe("Target agent (admin only)"),
  },
  async ({ service, agent }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can disconnect services for other agents.` }] };
    }

    // Remove all credentials for this service
    await supabaseQuery("agent_credentials", "DELETE", {
      "agent_name": `eq.${targetAgent}`,
      "service": `eq.${service}`,
    });

    // Find and revoke associated skill permissions
    const perms = await supabaseQuery("skill_permissions", "GET", {
      "agent_name": `eq.${targetAgent}`,
      "skill_name": `like.${service}%`,
      "revoked_at": "is.null",
    });

    for (const p of (perms || [])) {
      await supabaseQuery("skill_permissions", "PATCH", {
        filters: { "agent_name": `eq.${targetAgent}`, "skill_name": `eq.${p.skill_name}` },
        body: { revoked_at: new Date().toISOString() },
      });
    }

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        agent: targetAgent,
        service,
        credentials_removed: true,
        permissions_revoked: (perms || []).length,
        message: `Service '${service}' disconnected for ${targetAgent}.`,
      }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE 3: Skills Management
// ==========================================
// Backed by two Supabase tables:
//   - skills: catalog of all available skills (name, description, category, script_path)
//   - skill_permissions: which agent can use which skill (agent_name, skill_name, granted_by)
// Uses skills (catalog) + skill_permissions (access control) tables.

import { execFile } from "child_process";
import { promisify } from "util";
const execFileAsync = promisify(execFile);

server.tool(
  "grant_skill",
  "Grant an agent permission to use a skill. The skill must exist in the skills catalog. Admin only.",
  {
    agent: z.string().describe("Target agent name"),
    skill_name: z.string().describe("Skill name from the catalog (e.g. 'toggl', 'wave', 'email_triage')"),
  },
  async ({ agent, skill_name }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    if (!ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can grant skills.` }] };
    }

    // Verify skill exists in catalog
    const catalog = await supabaseQuery("skills", "GET", {
      "name": `eq.${skill_name}`,
    });
    if (!catalog || catalog.length === 0) {
      return { content: [{ type: "text", text: `Skill '${skill_name}' not found in catalog. Use list_skill_catalog to see available skills.` }] };
    }

    // Grant permission (upsert)
    try {
      await supabaseQuery("skill_permissions", "POST", {
        body: {
          agent_name: agent,
          skill_name,
          granted_by: AGENT_NAME,
        },
        onConflict: "agent_name,skill_name",
      });
    } catch (e) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, agent, skill_name, error: e.message }, null, 2) }] };
    }

    const skill = catalog[0];

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, agent, skill_name, category: skill.category, message: "Skill granted." }, null, 2) }]
    };
  }
);

server.tool(
  "revoke_skill",
  "Revoke an agent's permission to use a skill. Admin only.",
  {
    agent: z.string().describe("Target agent name"),
    skill_name: z.string().describe("Skill name to revoke"),
  },
  async ({ agent, skill_name }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    if (!ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can revoke skills.` }] };
    }

    // Soft-revoke in skill_permissions
    await supabaseQuery("skill_permissions", "PATCH", {
      filters: { "agent_name": `eq.${agent}`, "skill_name": `eq.${skill_name}` },
      body: { revoked_at: new Date().toISOString() },
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, agent, skill_name, message: "Skill revoked." }, null, 2) }]
    };
  }
);

server.tool(
  "list_skill_catalog",
  "List all available skills in the catalog, optionally filtered by category.",
  {
    category: z.string().optional().describe("Filter by category: communication, productivity, research, integration, admin, finance, monitoring"),
  },
  async ({ category }) => {
    const params = {
      "select": "name,description,category,requires_credentials,script_path",
      "order": "category,name",
    };
    if (category) params["category"] = `eq.${category}`;

    const skills = await supabaseQuery("skills", "GET", params);

    return {
      content: [{ type: "text", text: JSON.stringify({
        count: (skills || []).length,
        skills: (skills || []).map(s => ({
          name: s.name,
          description: s.description,
          category: s.category,
          executable: !!s.script_path,
          requires_credentials: s.requires_credentials,
        }))
      }, null, 2) }]
    };
  }
);

server.tool(
  "list_skills",
  "List skills granted to an agent. Agents see their own. Admins can query any agent.",
  {
    agent: z.string().optional().describe("Target agent (admin only — omit for your own)"),
  },
  async ({ agent }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can list other agents' skills.` }] };
    }

    // Join skill_permissions with skills catalog
    const perms = await supabaseQuery("skill_permissions", "GET", {
      "agent_name": `eq.${targetAgent}`,
      "revoked_at": "is.null",
      "select": "skill_name,granted_by,granted_at",
      "order": "skill_name",
    });

    // Enrich with catalog data
    const skillNames = (perms || []).map(p => p.skill_name);
    let catalog = [];
    if (skillNames.length > 0) {
      catalog = await supabaseQuery("skills", "GET", {
        "name": `in.(${skillNames.join(",")})`,
        "select": "name,description,category,script_path,requires_credentials",
      }) || [];
    }
    const catalogMap = Object.fromEntries(catalog.map(s => [s.name, s]));

    return {
      content: [{ type: "text", text: JSON.stringify({
        agent: targetAgent,
        count: (perms || []).length,
        skills: (perms || []).map(p => {
          const s = catalogMap[p.skill_name] || {};
          return {
            name: p.skill_name,
            description: s.description || "",
            category: s.category || "unknown",
            executable: !!s.script_path,
            granted_by: p.granted_by,
          };
        })
      }, null, 2) }]
    };
  }
);

server.tool(
  "my_skills",
  "Get a user-friendly list of skills assigned to this agent. Use this when a user asks what you can do.",
  {
    agent_name: z.string().describe("The agent's name"),
  },
  async ({ agent_name }) => {
    // Get active skill permissions for this agent
    const perms = await supabaseQuery("skill_permissions", "GET", {
      "agent_name": `eq.${agent_name}`,
      "revoked_at": "is.null",
      "select": "skill_name",
      "order": "skill_name",
    });

    const skillNames = (perms || []).map(p => p.skill_name);
    if (skillNames.length === 0) {
      return {
        content: [{ type: "text", text: [
          `${agent_name} doesn't have any skills assigned yet.`,
          "",
          "You and your user can build new skills together — if they wish you could do something new, you can scope it, build it, and add it to your toolkit.",
        ].join("\n") }]
      };
    }

    // Fetch catalog entries for these skills
    const catalog = await supabaseQuery("skills", "GET", {
      "name": `in.(${skillNames.join(",")})`,
      "select": "name,description,user_description",
    }) || [];
    const catalogMap = Object.fromEntries(catalog.map(s => [s.name, s]));

    // Build a clean, plain-language list
    const lines = skillNames.map(name => {
      const s = catalogMap[name] || {};
      const desc = s.user_description || s.description || "No description available";
      return `• ${name} — ${desc}`;
    });

    lines.push("");
    lines.push("You and your user can build new skills together — if they wish you could do something new, you can scope it, build it, and add it to your toolkit.");

    return {
      content: [{ type: "text", text: lines.join("\n") }]
    };
  }
);

server.tool(
  "run_skill",
  "Execute an authorized skill. Checks skill_permissions before running. Pass arguments as a space-separated string.",
  {
    skill_name: z.string().describe("Skill name to run"),
    args: z.string().optional().describe("Arguments to pass to the script (space-separated)"),
  },
  async ({ skill_name, args }) => {
    // Check permission
    const perms = await supabaseQuery("skill_permissions", "GET", {
      "agent_name": `eq.${AGENT_NAME}`,
      "skill_name": `eq.${skill_name}`,
      "revoked_at": "is.null",
    });

    if (!perms || perms.length === 0) {
      return {
        content: [{ type: "text", text: JSON.stringify({
          authorized: false,
          agent: AGENT_NAME,
          skill_name,
          message: `Skill '${skill_name}' is not granted to agent '${AGENT_NAME}'. Contact an admin to get access.`
        }, null, 2) }]
      };
    }

    // Get script_path from skills catalog
    const catalog = await supabaseQuery("skills", "GET", {
      "name": `eq.${skill_name}`,
      "select": "script_path",
    });

    if (!catalog || catalog.length === 0 || !catalog[0].script_path) {
      return {
        content: [{ type: "text", text: JSON.stringify({
          authorized: true,
          agent: AGENT_NAME,
          skill_name,
          error: `Skill '${skill_name}' has no executable script_path. It may be a capability skill (not script-based).`
        }, null, 2) }]
      };
    }

    const scriptPath = catalog[0].script_path;
    const scriptArgs = args ? args.split(/\s+/) : [];

    try {
      const { stdout, stderr } = await execFileAsync("python3", [scriptPath, ...scriptArgs], {
        timeout: 120000,
        maxBuffer: 1024 * 1024 * 5,
        env: {
          ...process.env,
          AGENT_NAME: AGENT_NAME,
          SUPABASE_SERVICE_KEY: SUPABASE_SERVICE_KEY,
        },
      });

      return {
        content: [{ type: "text", text: JSON.stringify({
          authorized: true,
          agent: AGENT_NAME,
          skill_name,
          stdout: stdout.slice(0, 10000),
          stderr: stderr ? stderr.slice(0, 2000) : undefined,
        }, null, 2) }]
      };
    } catch (err) {
      return {
        content: [{ type: "text", text: JSON.stringify({
          authorized: true,
          agent: AGENT_NAME,
          skill_name,
          error: err.message,
          stdout: err.stdout ? err.stdout.slice(0, 5000) : undefined,
          stderr: err.stderr ? err.stderr.slice(0, 2000) : undefined,
        }, null, 2) }]
      };
    }
  }
);

// ==========================================
// MODULE 4: Persistent Scheduler (Cerebellum)
// ==========================================
// Primary store: Supabase scheduled_tasks table.
// Local JSON file kept as cache for the executor (scheduler_executor.sh reads it).
// All writes go to Supabase first, then sync to local JSON.

import { readFileSync, writeFileSync, existsSync, mkdirSync, unlinkSync, readdirSync, statSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { homedir } from "os";
import { execFileSync, execSync } from "child_process";

const __dirname = dirname(fileURLToPath(import.meta.url));
const TASKS_FILE = join(__dirname, "scheduled_tasks.json");

function loadLocalTasks() {
  if (!existsSync(TASKS_FILE)) return [];
  try {
    return JSON.parse(readFileSync(TASKS_FILE, "utf8"));
  } catch {
    return [];
  }
}

function saveLocalTasks(tasks) {
  writeFileSync(TASKS_FILE, JSON.stringify(tasks, null, 2));
}

async function syncToLocal() {
  // Pull active tasks from Supabase and write to local JSON for the executor.
  // CRITICAL: must match scheduler_executor.sh's filter (`active=eq.true`).
  // Without this, inactive rows (e.g. paused admin_poll) leak into the local
  // cache and mask legitimate active rows with stale state — the root cause of
  // the 2026-04-24 admin_poll-not-firing incident.
  try {
    const tasks = await supabaseQuery("scheduled_tasks", "GET", {
      "active": "eq.true",
      "order": "agent_name,id",
    });
    if (tasks && Array.isArray(tasks)) {
      saveLocalTasks(tasks);
    }
  } catch (e) {
    // Silent fail — executor will use stale local cache
  }
}

function generateScheduleId() {
  return `sched_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

server.tool(
  "schedule_task",
  "Create a persistent scheduled task. Survives daemon restarts. Uses standard 5-field cron (minute hour dom month dow) in local timezone. Examples: '0 9 * * 1-5' = weekdays 9am, '0 */3 * * *' = every 3 hours, '30 14 25 4 *' = one-shot Apr 25 at 2:30pm. Set recurring=false for one-shot tasks.",
  {
    schedule: z.string().describe("5-field cron expression in local time (e.g. '0 9 * * 1-5')"),
    task_description: z.string().describe("What the agent should do when this fires. Be specific — this exact text is injected into the session."),
    recurring: z.boolean().optional().describe("true (default) = repeats on schedule. false = fires once then auto-deletes."),
    agent: z.string().optional().describe("Target agent (admin only — omit to schedule for yourself)"),
    trigger: z.enum(["pull", "push"]).optional().describe("pull (default) = delivered to agent on next user turn via list_due_tasks. push = scheduler spawns a one-shot claude -p at fire time (proactive, runs unattended). Use push for reminders/reports that MUST fire at a specific time regardless of user activity."),
  },
  async ({ schedule, task_description, recurring, agent, trigger }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can schedule tasks for other agents.` }] };
    }

    const fields = schedule.trim().split(/\s+/);
    if (fields.length !== 5) {
      return { content: [{ type: "text", text: `Invalid cron expression: expected 5 fields (minute hour dom month dow), got ${fields.length}.` }] };
    }

    const taskId = generateScheduleId();
    const taskData = {
      id: taskId,
      agent_name: targetAgent,
      schedule,
      task_description,
      recurring: recurring !== false,
      trigger: trigger || "pull",
      active: true,
      created_by: AGENT_NAME,
      created_at: new Date().toISOString(),
      last_fired_at: null,
    };

    // Write to Supabase first
    const result = await supabaseQuery("scheduled_tasks", "POST", { body: taskData });

    // Sync to local JSON for executor
    await syncToLocal();

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        id: taskId,
        agent: targetAgent,
        schedule,
        recurring: recurring !== false,
        message: `Scheduled task created. It will fire via the system executor. Use delete_scheduled_task with id '${taskId}' to remove.`
      }, null, 2) }]
    };
  }
);

server.tool(
  "list_scheduled_tasks",
  "List all scheduled tasks. Agents see their own. Admins can query any agent or all agents.",
  {
    agent: z.string().optional().describe("Filter by agent name (admin only — omit for your own). Use '*' for all agents."),
    active_only: z.boolean().optional().describe("Only show active tasks (default true)"),
  },
  async ({ agent, active_only }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const showAll = agent === "*" && ADMIN_AGENTS.includes(AGENT_NAME);
    const targetAgent = showAll ? null : (agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME);

    if (agent && agent !== "*" && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can list other agents' tasks.` }] };
    }

    // Read from Supabase
    const params = { "order": "agent_name,schedule" };
    if (targetAgent) params["agent_name"] = `eq.${targetAgent}`;
    if (active_only !== false) params["active"] = "eq.true";

    const tasks = await supabaseQuery("scheduled_tasks", "GET", params) || [];

    return {
      content: [{ type: "text", text: JSON.stringify({
        agent: targetAgent || "all",
        count: tasks.length,
        tasks: tasks.map(t => ({
          id: t.id,
          agent: t.agent_name,
          schedule: t.schedule,
          description: t.task_description.slice(0, 100) + (t.task_description.length > 100 ? "..." : ""),
          recurring: t.recurring,
          active: t.active,
          last_fired: t.last_fired_at,
          created_by: t.created_by,
        }))
      }, null, 2) }]
    };
  }
);

server.tool(
  "delete_scheduled_task",
  "Delete a scheduled task by ID.",
  {
    task_id: z.string().describe("The task ID to delete"),
  },
  async ({ task_id }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];

    // Fetch the task from Supabase to verify ownership
    const tasks = await supabaseQuery("scheduled_tasks", "GET", {
      "id": `eq.${task_id}`,
    });

    if (!tasks || tasks.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, message: "Task not found." }, null, 2) }] };
    }

    if (!ADMIN_AGENTS.includes(AGENT_NAME) && tasks[0].agent_name !== AGENT_NAME) {
      return { content: [{ type: "text", text: `Denied: you can only delete your own tasks.` }] };
    }

    const removed = tasks[0];

    // Delete from Supabase
    await supabaseQuery("scheduled_tasks", "DELETE", {
      "id": `eq.${task_id}`,
    });

    // Sync to local JSON
    await syncToLocal();

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        deleted: { id: removed.id, agent: removed.agent_name, schedule: removed.schedule },
        message: "Task deleted."
      }, null, 2) }]
    };
  }
);

server.tool(
  "update_scheduled_task",
  "Update an existing scheduled task's schedule, description, or active state.",
  {
    task_id: z.string().describe("The task ID to update"),
    schedule: z.string().optional().describe("New cron expression"),
    task_description: z.string().optional().describe("New task description"),
    active: z.boolean().optional().describe("Set active/paused state"),
    trigger: z.enum(["pull", "push"]).optional().describe("Switch between pull (user-turn delivery) and push (proactive one-shot claude -p)."),
  },
  async ({ task_id, schedule, task_description, active, trigger }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];

    // Fetch from Supabase
    const tasks = await supabaseQuery("scheduled_tasks", "GET", {
      "id": `eq.${task_id}`,
    });

    if (!tasks || tasks.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, message: "Task not found." }, null, 2) }] };
    }

    const task = tasks[0];

    if (!ADMIN_AGENTS.includes(AGENT_NAME) && task.agent_name !== AGENT_NAME) {
      return { content: [{ type: "text", text: `Denied: you can only update your own tasks.` }] };
    }

    if (schedule !== undefined) {
      const fields = schedule.trim().split(/\s+/);
      if (fields.length !== 5) {
        return { content: [{ type: "text", text: `Invalid cron expression: expected 5 fields.` }] };
      }
    }

    const updates = { updated_at: new Date().toISOString() };
    if (schedule !== undefined) updates.schedule = schedule;
    if (task_description !== undefined) updates.task_description = task_description;
    if (active !== undefined) updates.active = active;
    if (trigger !== undefined) updates.trigger = trigger;

    // Update in Supabase
    await supabaseQuery("scheduled_tasks", "PATCH", {
      filters: { "id": `eq.${task_id}` },
      body: updates,
    });

    // Sync to local JSON
    await syncToLocal();

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        updated: { id: task_id, agent: task.agent_name, schedule: schedule || task.schedule, active: active !== undefined ? active : task.active },
        message: "Task updated."
      }, null, 2) }]
    };
  }
);

// ---------- Pull-based scheduler: list_due_tasks ----------
// In --channels mode, tmux send-keys into Claude Code's stdin doesn't create
// a turn. That means scheduler_executor.sh firing tasks via send-keys silently
// fails for channels agents — the tasks fill the input buffer, but nothing
// runs. This tool is the pull-side counterpart: the agent calls it on every
// turn, receives any cron-matched tasks since last call, and executes them
// inline.

function cronFieldMatches(field, value) {
  if (field === "*") return true;
  const stepMatch = field.match(/^\*\/(\d+)$/);
  if (stepMatch) return value % Number(stepMatch[1]) === 0;
  const rangeMatch = field.match(/^(\d+)-(\d+)$/);
  if (rangeMatch) {
    const start = Number(rangeMatch[1]), end = Number(rangeMatch[2]);
    return value >= start && value <= end;
  }
  for (const part of field.split(",")) {
    const partRange = part.match(/^(\d+)-(\d+)$/);
    if (partRange) {
      if (value >= Number(partRange[1]) && value <= Number(partRange[2])) return true;
    } else if (Number(part) === value) {
      return true;
    }
  }
  return false;
}

function cronMatchesAt(cronExpr, date) {
  const fields = cronExpr.trim().split(/\s+/);
  if (fields.length !== 5) return false;
  const [cMin, cHour, cDom, cMon, cDow] = fields;
  return cronFieldMatches(cMin, date.getMinutes())
      && cronFieldMatches(cHour, date.getHours())
      && cronFieldMatches(cDom, date.getDate())
      && cronFieldMatches(cMon, date.getMonth() + 1)
      && cronFieldMatches(cDow, date.getDay());
}

function hasMatchedSince(cronExpr, since, now) {
  const WINDOW_MS = 7 * 24 * 60 * 60 * 1000;
  const start = Math.max(since.getTime(), now.getTime() - WINDOW_MS);
  const d = new Date(start);
  d.setSeconds(0, 0);
  while (d.getTime() <= now.getTime()) {
    if (cronMatchesAt(cronExpr, d)) return true;
    d.setMinutes(d.getMinutes() + 1);
  }
  return false;
}

server.tool(
  "list_due_tasks",
  "Returns scheduled tasks whose cron has matched since last confirmation. On every turn, call this BEFORE responding to your user. Returns max 3 tasks per call. For each returned task, execute it silently (these are your own recurring housekeeping — session summary, admin_poll, memory compact, etc.) then call confirm_task with that task's id. Tasks not confirmed within 2 hours will re-deliver; after 3 failed deliveries they are dropped.",
  {},
  async () => {
    const now = new Date();
    const MAX_RETURN = 3;
    const GRACE_MS = 2 * 60 * 60 * 1000;
    const MAX_ATTEMPTS = 3;

    const tasks = loadLocalTasks();
    const due = [];
    const toUpdate = new Map();  // id -> patch

    for (const task of tasks) {
      if (!task.active) continue;
      if (task.agent_name !== AGENT_NAME) continue;
      if (task.trigger === "push") continue;

      const lastFiredMs = task.last_fired_at ? Date.parse(task.last_fired_at) : 0;
      const deliveredMs = task.delivered_at ? Date.parse(task.delivered_at) : 0;
      const createdMs = task.created_at ? Date.parse(task.created_at) : 0;
      const attempts = task.delivery_attempts || 0;

      // Grace: delivered recently but not yet confirmed — wait.
      if (deliveredMs > lastFiredMs && (now.getTime() - deliveredMs) < GRACE_MS) {
        continue;
      }

      // Check if cron matched since last confirmation
      const floorMs = Math.max(lastFiredMs, createdMs, now.getTime() - 7 * 24 * 60 * 60 * 1000);
      const since = new Date(floorMs);

      if (!hasMatchedSince(task.schedule, since, now)) continue;

      // Max attempts exceeded — force-drop
      if (attempts >= MAX_ATTEMPTS) {
        toUpdate.set(task.id, {
          last_fired_at: now.toISOString(),
          delivered_at: null,
          delivery_attempts: 0,
          last_drop_at: now.toISOString(),
        });
        continue;
      }

      // Deliver it
      due.push({
        id: task.id,
        schedule: task.schedule,
        task_description: task.task_description,
        recurring: task.recurring,
        delivery_attempt: attempts + 1,
      });
      toUpdate.set(task.id, {
        delivered_at: now.toISOString(),
        delivery_attempts: attempts + 1,
      });

      if (due.length >= MAX_RETURN) break;
    }

    // Apply local updates
    if (toUpdate.size > 0) {
      const updated = tasks.map(t => toUpdate.has(t.id) ? { ...t, ...toUpdate.get(t.id) } : t);
      saveLocalTasks(updated);
    }

    return {
      content: [{ type: "text", text: JSON.stringify({
        count: due.length,
        tasks: due,
        note: due.length > 0 ? "Call confirm_task(task_id) after executing each task." : undefined,
      }, null, 2) }]
    };
  }
);

server.tool(
  "confirm_task",
  "Mark a task from list_due_tasks as successfully executed. Call this AFTER you've done the work described in the task, so it won't re-deliver. Pass the task's id (from list_due_tasks output).",
  {
    task_id: z.string().describe("The task id returned by list_due_tasks"),
  },
  async ({ task_id }) => {
    const tasks = loadLocalTasks();
    const task = tasks.find(t => t.id === task_id);
    if (!task) {
      return { content: [{ type: "text", text: JSON.stringify({ ok: false, error: "task not found" }) }] };
    }
    if (task.agent_name !== AGENT_NAME) {
      return { content: [{ type: "text", text: JSON.stringify({ ok: false, error: "not your task" }) }] };
    }

    const nowIso = new Date().toISOString();
    const updated = tasks.map(t => t.id === task_id ? {
      ...t,
      last_fired_at: nowIso,
      delivered_at: null,
      delivery_attempts: 0,
      active: t.recurring === false ? false : t.active,  // one-shot auto-deactivate
    } : t);
    saveLocalTasks(updated);

    // Supabase sync (best-effort)
    try {
      const body = { last_fired_at: nowIso };
      if (task.recurring === false) body.active = false;
      await supabaseQuery("scheduled_tasks", "PATCH", {
        filters: { "id": `eq.${task_id}` },
        body,
      });
    } catch {}

    return { content: [{ type: "text", text: JSON.stringify({ ok: true, task_id, confirmed_at: nowIso }) }] };
  }
);

// ---------- Pull-based heartbeats: list_due_heartbeats + confirm_heartbeat ----------
// Heartbeats are signal watchers (email monitors, lead alerts, etc.). They ran
// via send-keys too, same broken-in-channels-mode issue. Convert to pull.
// Returns max 1 per call to bound per-turn latency (email API calls are slow).

server.tool(
  "list_due_heartbeats",
  "Returns up to 1 heartbeat that needs to be checked now. Call this on each user turn AFTER list_due_tasks. If it returns a heartbeat, read its description + check_config, perform the check (e.g. query Gmail, Supabase), alert the user via Telegram if triggered, then call confirm_heartbeat with the id. Heartbeats cycle every 10 minutes; tasks queue if agent doesn't run for a while but are capped so single turn isn't overloaded.",
  {},
  async () => {
    const CYCLE_MS = 10 * 60 * 1000;  // 10 minutes
    const MAX_RETURN = 1;
    const now = new Date();

    // Fetch active heartbeats for this agent from Supabase
    const hbs = await supabaseQuery("heartbeats", "GET", {
      "agent_name": `eq.${AGENT_NAME}`,
      "active": "eq.true",
      "order": "last_checked_at.asc.nullsfirst",
    }) || [];

    const due = [];
    for (const hb of hbs) {
      const last = hb.last_checked_at ? Date.parse(hb.last_checked_at) : 0;
      if (now.getTime() - last < CYCLE_MS) continue;
      due.push({
        id: hb.id,
        watch_type: hb.watch_type,
        description: hb.description,
        check_config: hb.check_config,
        last_triggered_at: hb.last_triggered_at,
        last_checked_at: hb.last_checked_at,
      });
      if (due.length >= MAX_RETURN) break;
    }

    return {
      content: [{ type: "text", text: JSON.stringify({
        count: due.length,
        heartbeats: due,
        note: due.length > 0 ? "Do the check for each heartbeat, then call confirm_heartbeat(id, triggered, note)." : undefined,
      }, null, 2) }]
    };
  }
);

server.tool(
  "confirm_heartbeat",
  "Mark a heartbeat as checked. Call after performing the watch check. If the heartbeat condition was met (signal found), pass triggered=true — this updates last_triggered_at so follow-up logic knows. Pass triggered=false for a normal no-signal check. Optional note for logging.",
  {
    heartbeat_id: z.string().describe("The heartbeat id from list_due_heartbeats"),
    triggered: z.boolean().describe("true if the watch condition was met (alert already sent by you), false if no signal"),
    note: z.string().optional().describe("Brief check summary for audit (e.g. 'scanned 12 emails, 0 matched')"),
  },
  async ({ heartbeat_id, triggered, note }) => {
    const nowIso = new Date().toISOString();
    const body = { last_checked_at: nowIso };
    if (triggered) body.last_triggered_at = nowIso;

    try {
      await supabaseQuery("heartbeats", "PATCH", {
        filters: { "id": `eq.${heartbeat_id}`, "agent_name": `eq.${AGENT_NAME}` },
        body,
      });
    } catch (e) {
      return { content: [{ type: "text", text: JSON.stringify({ ok: false, error: String(e) }) }] };
    }

    return { content: [{ type: "text", text: JSON.stringify({
      ok: true, heartbeat_id, checked_at: nowIso, triggered, note: note || null,
    }) }] };
  }
);

// ==========================================
// MODULE 4b: Agent State (Supabase-backed with local cache)
// ==========================================
// Read/write idempotency markers and state tracking.
// Primary: Supabase agent_state table
// Cache: local JSON files at ~/AGENT_NAME/.state/KEY.json
// All writes go to both. Reads try Supabase first, fall back to local.

const STATE_DIR = join(homedir(), AGENT_NAME, ".state");
try { mkdirSync(STATE_DIR, { recursive: true }); } catch {}

function writeLocalState(key, value) {
  try {
    writeFileSync(join(STATE_DIR, `${key}.json`), JSON.stringify(value, null, 2));
  } catch {}
}

function readLocalState(key) {
  try {
    const p = join(STATE_DIR, `${key}.json`);
    if (existsSync(p)) return JSON.parse(readFileSync(p, "utf8"));
  } catch {}
  return null;
}

function deleteLocalState(key) {
  try {
    const p = join(STATE_DIR, `${key}.json`);
    if (existsSync(p)) unlinkSync(p);
  } catch {}
}

server.tool(
  "get_state",
  "Get a state value for the current agent. Used for idempotency markers, tracking state, etc. Reads from Supabase, falls back to local cache.",
  {
    key: z.string().describe("State key (e.g. 'pill_reminder', 'usage_alerts_sent', 'liability_accepted')"),
  },
  async ({ key }) => {
    // Try Supabase first
    try {
      const rows = await supabaseQuery("agent_state", "GET", {
        "agent_name": `eq.${AGENT_NAME}`,
        "key": `eq.${key}`,
      }) || [];

      if (rows.length > 0) {
        // Update local cache
        writeLocalState(key, rows[0].value);
        return { content: [{ type: "text", text: JSON.stringify({
          found: true, key, agent: AGENT_NAME,
          value: rows[0].value, updated_at: rows[0].updated_at, source: "supabase",
        }, null, 2) }] };
      }
    } catch {}

    // Fall back to local
    const local = readLocalState(key);
    if (local !== null) {
      return { content: [{ type: "text", text: JSON.stringify({
        found: true, key, agent: AGENT_NAME,
        value: local, source: "local_cache",
      }, null, 2) }] };
    }

    return { content: [{ type: "text", text: JSON.stringify({ found: false, key, agent: AGENT_NAME }) }] };
  }
);

server.tool(
  "set_state",
  "Set a state value for the current agent. Upserts — creates if new, updates if exists. Writes to both Supabase and local cache.",
  {
    key: z.string().describe("State key"),
    value: z.any().describe("State value (any JSON-serializable object)"),
  },
  async ({ key, value }) => {
    const storeValue = typeof value === "object" ? value : { value };

    // Write to Supabase
    try {
      await supabaseQuery("agent_state", "POST", {
        body: {
          agent_name: AGENT_NAME, key,
          value: storeValue,
          updated_at: new Date().toISOString(),
        },
        headers: { "Prefer": "resolution=merge-duplicates" },
      });
    } catch {}

    // Write to local cache
    writeLocalState(key, storeValue);

    return { content: [{ type: "text", text: JSON.stringify({
      success: true, key, agent: AGENT_NAME, message: "State saved to DB + local cache.",
    }) }] };
  }
);

server.tool(
  "delete_state",
  "Delete a state key for the current agent. Removes from both Supabase and local cache.",
  {
    key: z.string().describe("State key to delete"),
  },
  async ({ key }) => {
    // Delete from Supabase
    try {
      await supabaseQuery("agent_state", "DELETE", {
        "agent_name": `eq.${AGENT_NAME}`,
        "key": `eq.${key}`,
      });
    } catch {}

    // Delete local cache
    deleteLocalState(key);

    return { content: [{ type: "text", text: JSON.stringify({
      success: true, key, agent: AGENT_NAME, message: "State deleted from DB + local cache.",
    }) }] };
  }
);

server.tool(
  "list_state",
  "List all state keys for the current agent.",
  {},
  async () => {
    let rows = [];
    try {
      rows = await supabaseQuery("agent_state", "GET", {
        "agent_name": `eq.${AGENT_NAME}`,
        "select": "key,updated_at",
        "order": "key",
      }) || [];
    } catch {}

    // If Supabase failed, list from local cache
    if (rows.length === 0) {
      try {
        const files = readdirSync(STATE_DIR).filter(f => f.endsWith(".json"));
        rows = files.map(f => ({ key: f.replace(".json", ""), updated_at: null }));
      } catch {}
    }

    return { content: [{ type: "text", text: JSON.stringify({
      agent: AGENT_NAME, count: rows.length,
      keys: rows.map(r => ({ key: r.key, updated_at: r.updated_at })),
    }, null, 2) }] };
  }
);

// ==========================================
// MODULE 5: Analytics & Logging
// ==========================================
// Agents write interaction logs and infra events to Supabase.

server.tool(
  "log_interaction",
  "Log an interaction/conversation summary to the central DB. Call after conversations go idle.",
  {
    categories: z.array(z.string()).describe("Request categories (e.g. ['email', 'reminder', 'research'])"),
    request_summary: z.string().optional().describe("Brief summary of what was requested"),
    outcome: z.enum(["handled", "stuck", "partial", "escalated"]).describe("How the interaction ended"),
    skill_used: z.string().optional().describe("Primary skill used"),
    skill_gap: z.string().optional().describe("Capability the user wanted but doesn't exist yet"),
    duration_ms: z.number().optional().describe("Approximate duration in milliseconds"),
    satisfaction: z.enum(["positive", "neutral", "negative"]).optional().describe("Inferred user satisfaction"),
  },
  async ({ categories, request_summary, outcome, skill_used, skill_gap, duration_ms, satisfaction }) => {
    const result = await supabaseQuery("interaction_logs", "POST", {
      body: {
        agent_name: AGENT_NAME,
        categories,
        request_summary,
        outcome,
        skill_used,
        skill_gap,
        duration_ms,
        satisfaction,
        metadata: {},
      }
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, agent: AGENT_NAME, message: "Interaction logged." }, null, 2) }]
    };
  }
);

server.tool(
  "log_infra_event",
  "Log an infrastructure event (errors, warnings, status changes). Admin agents only.",
  {
    level: z.enum(["INFO", "WARNING", "ERROR", "CRITICAL"]).describe("Severity level"),
    component: z.string().describe("System component (e.g. 'scheduler', 'token_refresh', 'daemon')"),
    message: z.string().describe("Event description"),
    trace_id: z.string().optional().describe("Trace ID for correlation"),
    metadata: z.string().optional().describe("Optional JSON metadata"),
  },
  async ({ level, component, message, trace_id, metadata }) => {
    const result = await supabaseQuery("infra_events", "POST", {
      body: {
        level,
        component,
        message,
        trace_id,
        metadata: metadata ? JSON.parse(metadata) : {},
      }
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, level, component, message: "Event logged." }, null, 2) }]
    };
  }
);

server.tool(
  "start_session",
  "Register a new agent session in the central DB. Call at conversation start.",
  {
    model_used: z.string().optional().describe("Model being used (e.g. 'claude-sonnet-4-6')"),
  },
  async ({ model_used }) => {
    const result = await supabaseQuery("agent_sessions", "POST", {
      body: {
        agent_name: AGENT_NAME,
        model_used: model_used || null,
      }
    });

    const sessionId = result && result[0] ? result[0].id : null;

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, session_id: sessionId, agent: AGENT_NAME, message: "Session started." }, null, 2) }]
    };
  }
);

server.tool(
  "end_session",
  "End an agent session. Call when conversation ends or agent restarts.",
  {
    session_id: z.number().describe("The session ID to end"),
    messages_in: z.number().optional().describe("Total messages received"),
    messages_out: z.number().optional().describe("Total messages sent"),
    topics: z.array(z.string()).optional().describe("Topics covered in this session"),
    ended_reason: z.enum(["restart", "crash", "context_limit", "idle_timeout", "manual"]).optional().describe("Why the session ended"),
  },
  async ({ session_id, messages_in, messages_out, topics, ended_reason }) => {
    await supabaseQuery("agent_sessions", "PATCH", {
      filters: { "id": `eq.${session_id}` },
      body: {
        ended_at: new Date().toISOString(),
        messages_in,
        messages_out,
        topics,
        ended_reason,
      },
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, session_id, message: "Session ended." }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE 6: Agent Registry
// ==========================================

server.tool(
  "get_agent_info",
  "Get agent info from the central registry. Agents see their own info. Admins can query any agent.",
  {
    agent: z.string().optional().describe("Agent name (admin only — omit for your own)"),
  },
  async ({ agent }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can query other agents.` }] };
    }

    const agents = await supabaseQuery("agents", "GET", {
      "name": `eq.${targetAgent}`,
    });

    if (!agents || agents.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({ found: false, agent: targetAgent }, null, 2) }] };
    }

    const a = agents[0];
    return {
      content: [{ type: "text", text: JSON.stringify({
        found: true,
        agent: {
          name: a.name,
          persona: a.persona,
          human: a.human,
          status: a.status,
          model: a.model,
          timezone: a.timezone,
          billing_type: a.billing_type,
        }
      }, null, 2) }]
    };
  }
);

server.tool(
  "list_agents",
  "List all agents in the registry. Admin only.",
  {},
  async () => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    if (!ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can list all agents.` }] };
    }

    const agents = await supabaseQuery("agents", "GET", {
      "select": "name,persona,human,status,model,billing_type",
      "order": "name",
    });

    return {
      content: [{ type: "text", text: JSON.stringify({
        count: (agents || []).length,
        agents: agents || [],
      }, null, 2) }]
    };
  }
);

server.tool(
  "update_agent_status",
  "Update an agent's status in the registry. Admin only.",
  {
    agent: z.string().describe("Agent name"),
    status: z.enum(["active", "idle", "blocked", "offline", "decommissioned"]).describe("New status"),
  },
  async ({ agent, status }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    if (!ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can update agent status.` }] };
    }

    await supabaseQuery("agents", "PATCH", {
      filters: { "name": `eq.${agent}` },
      body: { status, updated_at: new Date().toISOString() },
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ success: true, agent, status, message: "Agent status updated." }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE 7: Heartbeat (Always-On Watches)
// ==========================================
// Heartbeats are persistent "watches" — things the agent monitors for on a cycle.
// Unlike scheduled tasks (which DO things on a schedule), heartbeats WATCH for
// signals and only act when something matches.
// Backed by Supabase `heartbeats` table. Executor fires a single "check your
// heartbeats" message per agent per cycle (every 10 min).

function generateHeartbeatId() {
  return `hb_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`;
}

server.tool(
  "add_heartbeat",
  "Add a heartbeat — a persistent watch that checks for something on a regular cycle. Unlike scheduled tasks which DO things, heartbeats WATCH for signals (new email, lead, review, threshold, etc.) and alert only when triggered. Runs every 10 minutes automatically.",
  {
    watch_type: z.string().describe("Type of watch: email, lead, review, inventory, uptime, webhook, custom"),
    description: z.string().describe("Human-readable description of what to watch for (e.g. 'New lead from enny.ai contact form')"),
    check_config: z.string().optional().describe("JSON config for the check — depends on watch_type. Email: {from, subject_contains, to}. Lead: {source}. Custom: {instructions}. Omit for simple watches."),
    notify_method: z.string().optional().describe("How to notify: telegram (default), email"),
    agent: z.string().optional().describe("Target agent (admin only — omit for yourself)"),
  },
  async ({ watch_type, description, check_config, notify_method, agent }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const targetAgent = agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME;

    if (agent && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can add heartbeats for other agents.` }] };
    }

    let parsedConfig = {};
    if (check_config) {
      try {
        parsedConfig = JSON.parse(check_config);
      } catch {
        return { content: [{ type: "text", text: `Invalid check_config: must be valid JSON.` }] };
      }
    }

    const hbId = generateHeartbeatId();
    const hbData = {
      id: hbId,
      agent_name: targetAgent,
      watch_type,
      description,
      check_config: parsedConfig,
      notify_method: notify_method || "telegram",
      active: true,
      created_by: AGENT_NAME,
      created_at: new Date().toISOString(),
    };

    await supabaseQuery("heartbeats", "POST", { body: hbData });

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        id: hbId,
        agent: targetAgent,
        watch_type,
        description,
        notify_method: notify_method || "telegram",
        message: `Heartbeat added. I'll check for this every ~10 minutes and alert when triggered. Use delete_heartbeat('${hbId}') to remove.`
      }, null, 2) }]
    };
  }
);

server.tool(
  "list_heartbeats",
  "List all heartbeats (active watches). Agents see their own. Admins can query any agent or all.",
  {
    agent: z.string().optional().describe("Filter by agent (admin only). Use '*' for all agents."),
    active_only: z.boolean().optional().describe("Only show active heartbeats (default true)"),
  },
  async ({ agent, active_only }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];
    const showAll = agent === "*" && ADMIN_AGENTS.includes(AGENT_NAME);
    const targetAgent = showAll ? null : (agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME);

    if (agent && agent !== "*" && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can list other agents' heartbeats.` }] };
    }

    const params = { "order": "agent_name,created_at" };
    if (targetAgent) params["agent_name"] = `eq.${targetAgent}`;
    if (active_only !== false) params["active"] = "eq.true";

    const heartbeats = await supabaseQuery("heartbeats", "GET", params) || [];

    return {
      content: [{ type: "text", text: JSON.stringify({
        agent: targetAgent || "all",
        count: heartbeats.length,
        heartbeats: heartbeats.map(h => ({
          id: h.id,
          agent: h.agent_name,
          watch_type: h.watch_type,
          description: h.description,
          check_config: h.check_config,
          notify_method: h.notify_method,
          active: h.active,
          last_checked: h.last_checked_at,
          last_triggered: h.last_triggered_at,
          created_by: h.created_by,
        }))
      }, null, 2) }]
    };
  }
);

server.tool(
  "update_heartbeat",
  "Update a heartbeat's description, config, or watch type.",
  {
    heartbeat_id: z.string().describe("The heartbeat ID to update"),
    description: z.string().optional().describe("New description"),
    check_config: z.string().optional().describe("New JSON config"),
    watch_type: z.string().optional().describe("New watch type"),
    notify_method: z.string().optional().describe("New notify method"),
  },
  async ({ heartbeat_id, description, check_config, watch_type, notify_method }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];

    const hbs = await supabaseQuery("heartbeats", "GET", { "id": `eq.${heartbeat_id}` });
    if (!hbs || hbs.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, message: "Heartbeat not found." }, null, 2) }] };
    }

    if (!ADMIN_AGENTS.includes(AGENT_NAME) && hbs[0].agent_name !== AGENT_NAME) {
      return { content: [{ type: "text", text: `Denied: you can only update your own heartbeats.` }] };
    }

    const updates = {};
    if (description !== undefined) updates.description = description;
    if (watch_type !== undefined) updates.watch_type = watch_type;
    if (notify_method !== undefined) updates.notify_method = notify_method;
    if (check_config !== undefined) {
      try {
        updates.check_config = JSON.parse(check_config);
      } catch {
        return { content: [{ type: "text", text: `Invalid check_config: must be valid JSON.` }] };
      }
    }

    await supabaseQuery("heartbeats", "PATCH", {
      filters: { "id": `eq.${heartbeat_id}` },
      body: updates,
    });

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        updated: { id: heartbeat_id, ...updates },
        message: "Heartbeat updated."
      }, null, 2) }]
    };
  }
);

server.tool(
  "delete_heartbeat",
  "Delete a heartbeat by ID.",
  {
    heartbeat_id: z.string().describe("The heartbeat ID to delete"),
  },
  async ({ heartbeat_id }) => {
    const ADMIN_AGENTS = ["admin", "dereklm"];

    const hbs = await supabaseQuery("heartbeats", "GET", { "id": `eq.${heartbeat_id}` });
    if (!hbs || hbs.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, message: "Heartbeat not found." }, null, 2) }] };
    }

    if (!ADMIN_AGENTS.includes(AGENT_NAME) && hbs[0].agent_name !== AGENT_NAME) {
      return { content: [{ type: "text", text: `Denied: you can only delete your own heartbeats.` }] };
    }

    const removed = hbs[0];
    await supabaseQuery("heartbeats", "DELETE", { "id": `eq.${heartbeat_id}` });

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        deleted: { id: removed.id, agent: removed.agent_name, description: removed.description },
        message: "Heartbeat deleted."
      }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE 8: Platform Operations (admin-only)
// ==========================================
// Derek uses these as his "operator levers" — restart/pause/resume agents,
// inspect fleet health, tail logs, diagnose stuck agents, trigger deploys.
// All admin-only (derek, dereklm).

const REGISTRY_FILE = "/Users/YOUR_MAC_USERNAME/derek/agent-infra/agents.json";
const TMUX_BIN = "/opt/homebrew/bin/tmux";

// Tripwire: agents.json keeps getting re-templatized (placeholders leaking back
// from sync_agent_platform.sh's sanitize step). Catch any read where the
// content contains the placeholder pattern, substitute in-memory, and ALSO
// rewrite the file so the next reader sees clean state. Loud-log every
// recurrence so we can eventually pin the writer.
// Incidents: 2026-05-09 morning, 2026-05-10 17:42 PT. Both broke platform_status
// and triggered fix_mcp_scheduler floods.
const REGISTRY_PLACEHOLDER_SUBS = [
  [/YOUR_MAC_USERNAME/g, "YOUR_MAC_USERNAME"],
  [/YOUR_TELEGRAM_CHAT_ID/g, "YOUR_TELEGRAM_CHAT_ID"],
];

function loadRegistry() {
  try {
    let raw = readFileSync(REGISTRY_FILE, "utf8");
    let needsRepair = false;
    for (const [pattern] of REGISTRY_PLACEHOLDER_SUBS) {
      if (pattern.test(raw)) { needsRepair = true; break; }
      pattern.lastIndex = 0; // reset regex state for next call
    }
    if (needsRepair) {
      const before = raw.length;
      for (const [pattern, replacement] of REGISTRY_PLACEHOLDER_SUBS) {
        raw = raw.replace(pattern, replacement);
      }
      // Write the repaired content back so other readers (Python scripts,
      // shell jq lookups) see a clean file too.
      try { writeFileSync(REGISTRY_FILE, raw); } catch (_) { /* read-only fs: continue with in-memory repair */ }
      // Loud telemetry — every recurrence should be visible.
      try {
        const stamp = new Date().toISOString();
        process.stderr.write(`[${stamp}] [admin-mcp] WARNING agents.json contained template placeholders — auto-repaired (${before} bytes). Investigate the writer.\n`);
      } catch (_) {}
    }
    return JSON.parse(raw).agents || {};
  } catch {
    return {};
  }
}

function tmuxSessionAlive(session) {
  try {
    execFileSync(TMUX_BIN, ["has-session", "-t", session], { stdio: "ignore" });
    return true;
  } catch {
    return false;
  }
}

function tmuxCapturePane(session, lines = 40) {
  try {
    return execFileSync(TMUX_BIN, ["capture-pane", "-t", session, "-p", "-S", `-${lines}`], { encoding: "utf8" });
  } catch {
    return "";
  }
}

function tmuxIdleSeconds(session) {
  try {
    const out = execFileSync(TMUX_BIN, ["display-message", "-t", session, "-p", "#{session_activity}"], { encoding: "utf8" }).trim();
    const activity = parseInt(out, 10);
    if (!activity) return -1;
    return Math.floor(Date.now() / 1000) - activity;
  } catch {
    return -1;
  }
}

function tailFile(path, lines = 50) {
  try {
    return execFileSync("/usr/bin/tail", ["-n", String(lines), path], { encoding: "utf8" });
  } catch {
    return "";
  }
}

function fileMtime(path) {
  try {
    return statSync(path).mtime.toISOString();
  } catch {
    return null;
  }
}

function launchctlLabel(agentName) {
  const reg = loadRegistry();
  return reg[agentName]?.daemon_label || null;
}

function launchctlUserTarget(label) {
  const uid = execSync("/usr/bin/id -u", { encoding: "utf8" }).trim();
  return `gui/${uid}/${label}`;
}

function assertAdmin() {
  const ADMIN_AGENTS = ["admin", "dereklm"];
  if (!ADMIN_AGENTS.includes(AGENT_NAME)) {
    throw new Error(`Denied: agent '${AGENT_NAME}' is not an admin. Only ${ADMIN_AGENTS.join(", ")} can call platform ops.`);
  }
}

server.tool(
  "platform_status",
  "Fleet-wide snapshot: tmux session state, .ready gate, daemon log mtime, credentials file presence, onboarded flag, recent scheduler activity. Admin-only.",
  {
    agent: z.string().optional().describe("Filter to a single agent (omit for full fleet)"),
  },
  async ({ agent }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const reg = loadRegistry();
    const agents = agent ? (reg[agent] ? { [agent]: reg[agent] } : {}) : reg;
    const rows = [];

    for (const [name, cfg] of Object.entries(agents)) {
      const ws = cfg.workspace || `/Users/YOUR_MAC_USERNAME/${name}`;
      const readyFile = `${ws}/.ready`;
      const liabFile = `${ws}/liability_accepted.json`;
      const daemonLog = name === "derek" ? "/Users/YOUR_MAC_USERNAME/.claude/daemon.log" : `${ws}/daemon.log`;
      const credsFile = cfg.config_dir ? `${cfg.config_dir}/.credentials.json` : null;

      rows.push({
        agent: name,
        persona: cfg.persona,
        role: cfg.role,
        tmux_alive: tmuxSessionAlive(cfg.tmux_session || `${name}-agent`),
        ready: existsSync(readyFile),
        onboarded: existsSync(liabFile),
        daemon_log_mtime: fileMtime(daemonLog),
        credentials_present: credsFile ? existsSync(credsFile) : null,
        model: cfg.model,
        has_pro_token: cfg.has_pro_token === true,
        auto_restart: cfg.auto_restart !== false,
        idle_seconds: tmuxSessionAlive(cfg.tmux_session || `${name}-agent`)
          ? tmuxIdleSeconds(cfg.tmux_session || `${name}-agent`)
          : null,
      });
    }

    return {
      content: [{ type: "text", text: JSON.stringify({
        fleet_size: rows.length,
        generated_at: new Date().toISOString(),
        agents: rows,
      }, null, 2) }]
    };
  }
);

server.tool(
  "restart_agent",
  "Restart a sub-agent via launchctl kickstart. Respects auto_restart flag in registry. Admin-only.",
  {
    target_agent: z.string().describe("The agent to restart"),
    force: z.boolean().optional().describe("Bypass auto_restart=false guard (use with caution)"),
  },
  async ({ target_agent, force }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const reg = loadRegistry();
    const cfg = reg[target_agent];
    if (!cfg) return { content: [{ type: "text", text: `Unknown agent: ${target_agent}` }] };
    if (cfg.auto_restart === false && !force) {
      return { content: [{ type: "text", text: `Agent ${target_agent} has auto_restart=false. Pass force=true to override.` }] };
    }

    const label = cfg.daemon_label;
    try {
      const target = launchctlUserTarget(label);
      execSync(`/bin/launchctl kickstart -k ${target}`, { encoding: "utf8" });
      return {
        content: [{ type: "text", text: JSON.stringify({
          success: true,
          agent: target_agent,
          action: "kickstart",
          label,
          message: "Daemon kicked. tmux session will be recreated in ~5-10s. Call platform_status to confirm.",
        }, null, 2) }]
      };
    } catch (err) {
      return { content: [{ type: "text", text: `kickstart failed: ${err.message}` }] };
    }
  }
);

server.tool(
  "pause_agent",
  "Stop a sub-agent's daemon (launchctl bootout). Agent stays down until resume_agent. Admin-only.",
  { target_agent: z.string() },
  async ({ target_agent }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const reg = loadRegistry();
    const cfg = reg[target_agent];
    if (!cfg) return { content: [{ type: "text", text: `Unknown agent: ${target_agent}` }] };

    try {
      const target = launchctlUserTarget(cfg.daemon_label);
      execSync(`/bin/launchctl bootout ${target}`, { encoding: "utf8", stdio: "pipe" });
    } catch {
      // bootout often exits non-zero even on success; don't treat as fatal
    }

    // Kill tmux session too so restart_agent later is clean
    try {
      execFileSync(TMUX_BIN, ["kill-session", "-t", cfg.tmux_session || `${target_agent}-agent`], { stdio: "ignore" });
    } catch {
      // ignore
    }

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        agent: target_agent,
        action: "paused",
        message: "Daemon booted out. Call resume_agent when ready to bring it back.",
      }, null, 2) }]
    };
  }
);

server.tool(
  "resume_agent",
  "Start a paused sub-agent's daemon (launchctl bootstrap). Admin-only.",
  { target_agent: z.string() },
  async ({ target_agent }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const reg = loadRegistry();
    const cfg = reg[target_agent];
    if (!cfg) return { content: [{ type: "text", text: `Unknown agent: ${target_agent}` }] };

    try {
      const uid = execSync("/usr/bin/id -u", { encoding: "utf8" }).trim();
      execSync(`/bin/launchctl bootstrap gui/${uid} "${cfg.plist}"`, { encoding: "utf8" });
      return {
        content: [{ type: "text", text: JSON.stringify({
          success: true, agent: target_agent, action: "resumed",
          message: "Daemon bootstrapped. tmux session will come up in ~5-10s.",
        }, null, 2) }]
      };
    } catch (err) {
      return { content: [{ type: "text", text: `bootstrap failed: ${err.message}` }] };
    }
  }
);

server.tool(
  "tail_agent_log",
  "Return last N lines of an agent's daemon log. Admin-only.",
  {
    target_agent: z.string(),
    lines: z.number().optional().describe("Number of lines (default 80, max 500)"),
  },
  async ({ target_agent, lines }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const reg = loadRegistry();
    const cfg = reg[target_agent];
    if (!cfg) return { content: [{ type: "text", text: `Unknown agent: ${target_agent}` }] };

    const n = Math.min(Math.max(lines || 80, 1), 500);
    const ws = cfg.workspace || `/Users/YOUR_MAC_USERNAME/${target_agent}`;
    const logFile = target_agent === "derek" ? "/Users/YOUR_MAC_USERNAME/.claude/daemon.log" : `${ws}/daemon.log`;
    const content = tailFile(logFile, n);

    return {
      content: [{ type: "text", text: JSON.stringify({
        agent: target_agent, log_file: logFile, lines_returned: content.split("\n").length, tail: content,
      }, null, 2) }]
    };
  }
);

server.tool(
  "diagnose_agent",
  "One-shot triage: tmux pane tail, daemon log tail, .ready gate, credentials state, recent scheduled tasks fired. Use when an agent seems stuck. Admin-only.",
  { target_agent: z.string() },
  async ({ target_agent }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const reg = loadRegistry();
    const cfg = reg[target_agent];
    if (!cfg) return { content: [{ type: "text", text: `Unknown agent: ${target_agent}` }] };

    const ws = cfg.workspace || `/Users/YOUR_MAC_USERNAME/${target_agent}`;
    const session = cfg.tmux_session || `${target_agent}-agent`;
    const daemonLog = target_agent === "derek" ? "/Users/YOUR_MAC_USERNAME/.claude/daemon.log" : `${ws}/daemon.log`;

    const tasks = await supabaseQuery("scheduled_tasks", "GET", {
      "agent_name": `eq.${target_agent}`,
      "order": "last_fired_at.desc.nullslast",
      "limit": "5",
    });

    const report = {
      agent: target_agent,
      persona: cfg.persona,
      tmux_alive: tmuxSessionAlive(session),
      tmux_idle_seconds: tmuxSessionAlive(session) ? tmuxIdleSeconds(session) : null,
      ready_file: existsSync(`${ws}/.ready`),
      onboarded: existsSync(`${ws}/liability_accepted.json`),
      credentials_file_present: cfg.config_dir ? existsSync(`${cfg.config_dir}/.credentials.json`) : null,
      daemon_log_mtime: fileMtime(daemonLog),
      daemon_log_tail: tailFile(daemonLog, 30),
      tmux_pane_tail: tmuxSessionAlive(session) ? tmuxCapturePane(session, 30) : null,
      recent_scheduled_tasks: (tasks || []).map(t => ({
        id: t.id, schedule: t.schedule, active: t.active, last_fired_at: t.last_fired_at, description: t.task_description?.slice(0, 80),
      })),
    };

    const symptoms = [];
    if (!report.tmux_alive) symptoms.push("tmux_session_dead");
    if (!report.ready_file) symptoms.push("not_ready");
    if (report.credentials_file_present === false && cfg.has_pro_token) symptoms.push("missing_credentials");
    if (report.tmux_alive && report.tmux_idle_seconds > 7200) symptoms.push("long_idle");
    if (report.tmux_pane_tail && /Bypass permissions/i.test(report.tmux_pane_tail)) symptoms.push("stuck_on_bypass_warning");
    if (report.tmux_pane_tail && /API Error|rate limit|not authenticated/i.test(report.tmux_pane_tail)) symptoms.push("auth_or_api_error");
    report.symptoms = symptoms;

    return { content: [{ type: "text", text: JSON.stringify(report, null, 2) }] };
  }
);

server.tool(
  "deploy_configs",
  "Run the agent-infra deploy.sh script. Pushes CLAUDE.md, startup-instructions, launchers to live workspaces. Admin-only.",
  {
    diff_only: z.boolean().optional().describe("Run with --diff flag — show what would change, don't deploy"),
  },
  async ({ diff_only }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const args = ["/Users/YOUR_MAC_USERNAME/derek/agent-infra/deploy.sh"];
    if (diff_only) args.push("--diff");

    try {
      const out = execFileSync("/bin/bash", args, { encoding: "utf8", maxBuffer: 4 * 1024 * 1024 });
      return {
        content: [{ type: "text", text: JSON.stringify({
          success: true, diff_only: !!diff_only, output: out.slice(-6000),
        }, null, 2) }]
      };
    } catch (err) {
      return { content: [{ type: "text", text: JSON.stringify({
        success: false, error: err.message, output: (err.stdout || "").toString().slice(-4000),
      }, null, 2) }] };
    }
  }
);

// ==========================================
// MODULE 9: Broadcast & Agent Messaging
// ==========================================

server.tool(
  "broadcast_to_subs",
  "Create one admin_task per sub-agent in registry. Use for fleet-wide instructions (e.g. reload_config, refresh_memory_index). Admin-only.",
  {
    action: z.string().describe("Action name (e.g. 'reload_config', 'acknowledge_broadcast')"),
    params: z.string().optional().describe("JSON-encoded params applied to all targets"),
    exclude: z.string().optional().describe("Comma-separated agent names to exclude"),
  },
  async ({ action, params, exclude }) => {
    try {
      assertAdmin();
    } catch (e) {
      return { content: [{ type: "text", text: e.message }] };
    }

    const reg = loadRegistry();
    const excluded = new Set((exclude || "").split(",").map(s => s.trim()).filter(Boolean));
    const subs = Object.entries(reg).filter(([n, c]) => c.role === "sub" && !excluded.has(n)).map(([n]) => n);
    const parsedParams = params ? JSON.parse(params) : {};
    const results = [];

    for (const target of subs) {
      const taskId = generateTaskId();
      await supabaseQuery("admin_tasks", "POST", {
        body: {
          task_id: taskId,
          source_agent: AGENT_NAME,
          target_agent: target,
          action,
          params: parsedParams,
          status: "pending",
        }
      });
      results.push({ target, task_id: taskId });
    }

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true, action, broadcast_count: results.length, tasks: results,
      }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE: Memory (Phase 2.2) — DB-backed agent memories
// ==========================================
// Schema lives in /Users/YOUR_MAC_USERNAME/derek/agent-infra/schema/memory_tables.sql.
// Writes go through these tools; a projector cron materializes them to
// ~/<agent>/memory/*.md every 15 min. Agents still Read local files at session
// start — the DB is the source of truth, files are a read-only cache.

const MEMORY_TYPES = ["soul", "user", "feedback", "project", "reference", "failed", "session"];
const ADMIN_AGENTS_MEM = ["admin", "dereklm"];

function slugify(s) {
  // Underscore convention: matches the existing ~/<agent>/memory/ filename
  // scheme (feedback_pill_confirmation.md, user_contacts_nicknames.md, etc.).
  // Keep this in sync with project_memories_to_files.py:slugify.
  return String(s || "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "")
    .slice(0, 80);
}

function memoryId(agent, type, name) {
  return `${agent}/${type}/${slugify(name)}`;
}

function projectedFileName(type, name, agent) {
  // Soul uses `<slug(name)>_soul.md` — the persona slug, not the agent key.
  // This keeps CLAUDE.md paths like memory/derek_soul.md or memory/overseer_soul.md
  // stable even when the persona name differs from the registry agent key.
  if (type === "soul") return `${slugify(name)}_soul.md`;
  return `${type}_${slugify(name)}.md`;
}

function projectedFilePath(agent, type, name) {
  const workspace = agent === "derek" ? "derekPersonal" : agent;
  return `/Users/YOUR_MAC_USERNAME/${workspace}/memory/${projectedFileName(type, name, agent)}`;
}

server.tool(
  "store_memory",
  "Store or update a durable memory in the fleet's agent_memories table. Agents store their own by default; admin agents (admin, dereklm) can pass 'agent' to write for others. References require a 'source' URL/path (DB-enforced). The file projection at ~/<agent>/memory/ appears within 15 min via the projector cron.",
  {
    type: z.enum(MEMORY_TYPES).describe("Memory type: soul, user, feedback, project, reference, failed, session"),
    name: z.string().describe("Short identifier (1-200 chars). Used in the slug; stable across updates."),
    description: z.string().describe("One-line description used for memory-index rendering and relevance matching (1-500 chars)."),
    content: z.string().describe("Full memory body, markdown allowed."),
    source: z.string().optional().describe("URL or file path. REQUIRED for type='reference' — DB rejects reference rows without it."),
    tags: z.array(z.string()).optional().describe("Optional tags for grouping / filtering."),
    agent: z.string().optional().describe("Target agent (admin only — omit to store for yourself)."),
  },
  async ({ type, name, description, content, source, tags, agent }) => {
    if (agent && !ADMIN_AGENTS_MEM.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can store memories for other agents.` }] };
    }
    const targetAgent = agent && ADMIN_AGENTS_MEM.includes(AGENT_NAME) ? agent : AGENT_NAME;
    const id = memoryId(targetAgent, type, name);

    const body = {
      id,
      agent_name: targetAgent,
      type,
      name,
      description,
      content,
      source: source ?? null,
      tags: tags ?? [],
      origin_session_id: process.env.CLAUDE_SESSION_ID ?? null,
    };

    const result = await supabaseQuery("agent_memories", "POST", { body, onConflict: "id" });

    if (Array.isArray(result)) {
      return {
        content: [{ type: "text", text: JSON.stringify({
          success: true, id, agent: targetAgent, type, name,
          projected_file: projectedFilePath(targetAgent, type, name),
          message: "Memory stored. File will be projected to ~/<agent>/memory/ within 15 min.",
        }, null, 2) }]
      };
    }
    return {
      content: [{ type: "text", text: JSON.stringify({
        success: false, id, agent: targetAgent, type, name,
        error: result.message || result, hint: result.hint || null,
      }, null, 2) }]
    };
  }
);

server.tool(
  "get_memory",
  "Fetch a single memory by id or by (type, name). Agents get their own by default; admin agents can pass 'agent' to read another agent's.",
  {
    id: z.string().optional().describe("Full slug (e.g. 'admin/feedback/no-verbose-summaries'). If given, takes precedence."),
    type: z.enum(MEMORY_TYPES).optional().describe("Memory type (used with 'name' if 'id' not provided)."),
    name: z.string().optional().describe("Memory name (used with 'type' if 'id' not provided)."),
    agent: z.string().optional().describe("Target agent (admin only — omit to fetch your own)."),
  },
  async ({ id, type, name, agent }) => {
    if (agent && !ADMIN_AGENTS_MEM.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can read other agents' memories.` }] };
    }
    const targetAgent = agent && ADMIN_AGENTS_MEM.includes(AGENT_NAME) ? agent : AGENT_NAME;

    const params = { select: "*" };
    if (id) {
      params.id = `eq.${id}`;
    } else if (type && name) {
      params.agent_name = `eq.${targetAgent}`;
      params.type = `eq.${type}`;
      params.name = `eq.${name}`;
    } else {
      return { content: [{ type: "text", text: JSON.stringify({ found: false, error: "Provide either 'id' or both 'type' and 'name'." }, null, 2) }] };
    }

    const rows = await supabaseQuery("agent_memories", "GET", params);
    if (!Array.isArray(rows) || rows.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({ found: false, agent: targetAgent, id, type, name }, null, 2) }] };
    }
    return { content: [{ type: "text", text: JSON.stringify({ found: true, memory: rows[0] }, null, 2) }] };
  }
);

server.tool(
  "list_memories",
  "List memories for an agent, optionally filtered by type or tags. Superseded memories are hidden by default.",
  {
    type: z.enum(MEMORY_TYPES).optional().describe("Filter by memory type."),
    tags: z.array(z.string()).optional().describe("Filter by tags (memories containing ALL listed tags)."),
    include_superseded: z.boolean().optional().describe("Default false — superseded memories are hidden."),
    agent: z.string().optional().describe("Target agent (admin only — omit to list your own)."),
  },
  async ({ type, tags, include_superseded, agent }) => {
    if (agent && !ADMIN_AGENTS_MEM.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can list other agents' memories.` }] };
    }
    const targetAgent = agent && ADMIN_AGENTS_MEM.includes(AGENT_NAME) ? agent : AGENT_NAME;

    const params = {
      agent_name: `eq.${targetAgent}`,
      select: "id,type,name,description,source,tags,superseded_by,created_at,updated_at,verified_at",
      order: "type,name",
    };
    if (type) params.type = `eq.${type}`;
    if (tags && tags.length) params.tags = `cs.{${tags.map(t => `"${t.replace(/"/g, "")}"`).join(",")}}`;
    if (!include_superseded) params.superseded_by = "is.null";

    const rows = await supabaseQuery("agent_memories", "GET", params);
    return {
      content: [{ type: "text", text: JSON.stringify({
        agent: targetAgent,
        count: Array.isArray(rows) ? rows.length : 0,
        memories: Array.isArray(rows) ? rows : [],
      }, null, 2) }]
    };
  }
);

server.tool(
  "supersede_memory",
  "Mark an old memory as superseded by a newer one. Sets old.superseded_by = new_id atomically. The projector cron will drop the old file on its next run.",
  {
    old_id: z.string().describe("Full slug of the memory being replaced."),
    new_id: z.string().describe("Full slug of the replacement memory (must already exist — call store_memory first)."),
    reason: z.string().optional().describe("Optional short note appended to the old memory's description."),
  },
  async ({ old_id, new_id, reason }) => {
    const existing = await supabaseQuery("agent_memories", "GET", { id: `eq.${new_id}`, select: "id" });
    if (!Array.isArray(existing) || existing.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({ success: false, error: `new_id ${new_id} does not exist — store_memory first.` }, null, 2) }] };
    }

    const patchBody = { superseded_by: new_id };
    if (reason) {
      const oldRow = await supabaseQuery("agent_memories", "GET", { id: `eq.${old_id}`, select: "description" });
      if (Array.isArray(oldRow) && oldRow.length) {
        patchBody.description = `${oldRow[0].description} [superseded: ${reason}]`.slice(0, 500);
      }
    }
    const status = await supabaseQuery("agent_memories", "PATCH", {
      filters: { id: `eq.${old_id}` },
      body: patchBody,
    });
    return {
      content: [{ type: "text", text: JSON.stringify({
        success: status >= 200 && status < 300,
        old_id, new_id, http_status: status,
      }, null, 2) }]
    };
  }
);

server.tool(
  "search_memories",
  "Search memories by substring across name, description, and content. Agent-scoped by default; admin agents can pass agent='*' to search the whole fleet.",
  {
    query: z.string().describe("Substring to search for (case-insensitive)."),
    type: z.enum(MEMORY_TYPES).optional().describe("Optional: restrict to one memory type."),
    agent: z.string().optional().describe("Target agent, or '*' for fleet-wide (admin only). Omit for your own."),
    limit: z.number().optional().describe("Max results (default 20)."),
  },
  async ({ query, type, agent, limit }) => {
    const wantFleet = agent === "*";
    if ((agent && agent !== "*") && !ADMIN_AGENTS_MEM.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can search other agents' memories.` }] };
    }
    if (wantFleet && !ADMIN_AGENTS_MEM.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can search fleet-wide.` }] };
    }

    const esc = query.replace(/[,()]/g, " ").replace(/\s+/g, "%");
    const params = {
      or: `(name.ilike.*${esc}*,description.ilike.*${esc}*,content.ilike.*${esc}*)`,
      select: "id,agent_name,type,name,description,tags,updated_at",
      superseded_by: "is.null",
      order: "updated_at.desc",
      limit: String(limit || 20),
    };
    if (!wantFleet) params.agent_name = `eq.${agent || AGENT_NAME}`;
    if (type) params.type = `eq.${type}`;

    const rows = await supabaseQuery("agent_memories", "GET", params);
    return {
      content: [{ type: "text", text: JSON.stringify({
        query, scope: wantFleet ? "fleet" : (agent || AGENT_NAME),
        count: Array.isArray(rows) ? rows.length : 0,
        matches: Array.isArray(rows) ? rows : [],
      }, null, 2) }]
    };
  }
);

// ==========================================
// MODULE: Skill proposals (Phase 3) — agent-initiated skill creation
// ==========================================
// Agents propose new skills (or refinements to existing ones). Proposals land
// as admin_tasks with action='propose_skill' or 'refine_skill'. Admin picks
// them up on its poll, validates + security-scans the draft, routes to Farlen
// for Telegram approval, and on approval writes SKILL.md + skills row +
// grant_skill. See plan glowing-rolling-plum.md Phase 3 and runbook sections.

const SKILL_NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/;
const SECRET_PATTERNS = [
  { name: "github_pat",          re: /ghp_[A-Za-z0-9]{36}/ },
  { name: "anthropic_oauth",     re: /sk-ant-[A-Za-z0-9_-]{20,}/ },
  { name: "openai_project",      re: /sk-proj-[A-Za-z0-9_-]{20,}/ },
  { name: "notion_integration",  re: /ntn_[A-Za-z0-9_]{40,}/ },
  { name: "aws_access_key",      re: /AKIA[0-9A-Z]{16}/ },
  { name: "supabase_pat",        re: /sbp_[a-f0-9]{40}/ },
  { name: "jwt_looking",         re: /eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}/ },
];

server.tool(
  "propose_skill",
  "Propose a new skill for admin review and Farlen approval. Call this after a task with 5+ tool calls, a non-obvious workflow, or a tricky error recovery that's worth capturing as reusable. Admin validates the draft, runs a light security scan, routes to Farlen for Telegram approval, and on approval writes SKILL.md + registers it + grants it to you. Returns a task_id to track status via list_pending_tasks.",
  {
    skill_name: z.string().describe("Short slug for the skill. 2-64 chars, lowercase alphanumeric + hyphen (no leading/trailing hyphen). Becomes the file stem at /Users/YOUR_MAC_USERNAME/derek/skills/<slug>/SKILL.md."),
    draft_skill_md: z.string().describe("Full SKILL.md content: YAML frontmatter (name, description, category, version, author, requires_credentials, example_uses) then markdown body with at least ## Usage Conditions, ## Steps, ## Known Failure Modes, ## Verification sections."),
    reason: z.string().describe("Why this skill — what repeat pattern, tricky workflow, or error-recovery insight is worth capturing? Farlen reads this when approving."),
    example_uses: z.array(z.string()).optional().describe("2-3 concrete invocation examples with context (helps discovery + eval harness)."),
  },
  async ({ skill_name, draft_skill_md, reason, example_uses }) => {
    if (!SKILL_NAME_RE.test(skill_name)) {
      return { content: [{ type: "text", text: JSON.stringify({
        success: false,
        error: "Invalid skill_name: must be 2-64 chars, lowercase alphanumeric + hyphen, no leading/trailing hyphen.",
      }, null, 2) }] };
    }
    if (!draft_skill_md.startsWith("---\n")) {
      return { content: [{ type: "text", text: JSON.stringify({
        success: false,
        error: "draft_skill_md must start with '---' followed by a YAML frontmatter block (name, description, type etc.).",
      }, null, 2) }] };
    }
    for (const { name: pname, re } of SECRET_PATTERNS) {
      if (re.test(draft_skill_md)) {
        return { content: [{ type: "text", text: JSON.stringify({
          success: false,
          error: `Security scan rejected: draft contains a token resembling ${pname}. Reference credentials via get_credential (vault) — never hardcode tokens in skill files.`,
        }, null, 2) }] };
      }
    }

    const existing = await supabaseQuery("skills", "GET", { name: `eq.${skill_name}`, select: "name,origin" });
    if (Array.isArray(existing) && existing.length > 0) {
      return { content: [{ type: "text", text: JSON.stringify({
        success: false,
        error: `Skill '${skill_name}' already exists (origin=${existing[0].origin}). Use refine_skill to propose changes to it.`,
      }, null, 2) }] };
    }

    const taskId = generateTaskId();
    await supabaseQuery("admin_tasks", "POST", {
      body: {
        task_id: taskId,
        source_agent: AGENT_NAME,
        target_agent: "admin",
        action: "propose_skill",
        params: {
          skill_name,
          draft_skill_md,
          reason,
          example_uses: example_uses || [],
        },
        status: "pending",
      },
    });

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        task_id: taskId,
        skill_name,
        status: "pending",
        message: "Proposal submitted. Admin will pick it up on its next poll, run basic validation, and route to Farlen for Telegram approval. On approval the skill is registered and granted to you automatically — check list_pending_tasks to see when the task completes.",
      }, null, 2) }]
    };
  }
);

server.tool(
  "refine_skill",
  "Propose a refinement (find-and-replace patch) to an existing skill after discovering something worth capturing during use. Patches touching only 'Known Failure Modes', 'Verification', or whitespace may be auto-applied by admin; patches touching 'Steps' or 'Usage Conditions' require Farlen approval.",
  {
    skill_name: z.string().describe("Slug of the existing skill to refine."),
    patch_find: z.string().describe("Text block from the current SKILL.md to replace. Whitespace-tolerant fuzzy match — include 2-3 lines of context if the same short phrase appears multiple times."),
    patch_replace: z.string().describe("Replacement text. Keep edits surgical; don't rewrite the whole skill."),
    reason: z.string().describe("Why this refinement — what did you learn while using the skill?"),
  },
  async ({ skill_name, patch_find, patch_replace, reason }) => {
    const skills = await supabaseQuery("skills", "GET", { name: `eq.${skill_name}`, select: "name" });
    if (!Array.isArray(skills) || skills.length === 0) {
      return { content: [{ type: "text", text: JSON.stringify({
        success: false,
        error: `Skill '${skill_name}' not found in the catalog. Use propose_skill for new skills.`,
      }, null, 2) }] };
    }

    // Same secret scan on the replacement — don't let refinement sneak secrets in.
    for (const { name: pname, re } of SECRET_PATTERNS) {
      if (re.test(patch_replace)) {
        return { content: [{ type: "text", text: JSON.stringify({
          success: false,
          error: `Security scan rejected: patch_replace contains a token resembling ${pname}. Use get_credential instead.`,
        }, null, 2) }] };
      }
    }

    const taskId = generateTaskId();
    await supabaseQuery("admin_tasks", "POST", {
      body: {
        task_id: taskId,
        source_agent: AGENT_NAME,
        target_agent: "admin",
        action: "refine_skill",
        params: {
          skill_name,
          patch_find,
          patch_replace,
          reason,
        },
        status: "pending",
      },
    });

    return {
      content: [{ type: "text", text: JSON.stringify({
        success: true,
        task_id: taskId,
        skill_name,
        status: "pending",
        message: "Refinement proposed. Admin classifies the patch scope: safe (auto-apply) vs gated (Farlen-approved). Track via list_pending_tasks.",
      }, null, 2) }]
    };
  }
);

server.tool(
  "list_skill_proposals",
  "Admin-only. Returns all pending propose_skill and refine_skill admin_tasks with a short preview of each draft/patch. Feeds the approval queue that admin reviews on every poll.",
  {},
  async () => {
    const ADMIN_AGENTS_S = ["admin", "dereklm"];
    if (!ADMIN_AGENTS_S.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: list_skill_proposals is admin-only (you are '${AGENT_NAME}').` }] };
    }

    const rows = await supabaseQuery("admin_tasks", "GET", {
      "target_agent": "eq.admin",
      "action": "in.(propose_skill,refine_skill)",
      "status": "eq.pending",
      "order": "created_at.desc",
      "select": "task_id,source_agent,action,params,created_at,expires_at",
    });

    const proposals = (rows || []).map(t => {
      const p = t.params || {};
      const base = {
        task_id: t.task_id,
        action: t.action,
        from: t.source_agent,
        skill_name: p.skill_name,
        reason: p.reason,
        created_at: t.created_at,
        expires_at: t.expires_at,
      };
      if (t.action === "propose_skill") {
        const draft = (p.draft_skill_md || "");
        base.draft_preview = draft.split("\n").slice(0, 25).join("\n");
        base.draft_length = draft.length;
        base.example_uses = p.example_uses || [];
      } else if (t.action === "refine_skill") {
        base.patch_preview = {
          find: (p.patch_find || "").slice(0, 240),
          replace: (p.patch_replace || "").slice(0, 240),
        };
      }
      return base;
    });

    return {
      content: [{ type: "text", text: JSON.stringify({ count: proposals.length, proposals }, null, 2) }]
    };
  }
);

// --- Start ---

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
