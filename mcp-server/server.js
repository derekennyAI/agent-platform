#!/usr/bin/env node
/**
 * Admin Control MCP Server
 *
 * Two modules:
 * 1. Admin Verification — agents verify if admin-issued tasks are legitimate
 * 2. Credential Vault — agents store/retrieve scoped credentials
 *
 * Backed by Supabase (your-project).
 * Each agent identifies itself via AGENT_NAME env var.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { z } from "zod";

const SUPABASE_URL = process.env.SUPABASE_URL || "https://your-project.supabase.co";
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
    if (params.onConflict) {
      url.searchParams.set("on_conflict", params.onConflict);
    }
    const resp = await fetch(url, { method: "POST", headers, body: JSON.stringify(params.body) });
    return resp.json();
  }

  if (method === "PATCH") {
    for (const [k, v] of Object.entries(params.filters || {})) {
      url.searchParams.set(k, v);
    }
    const resp = await fetch(url, { method: "PATCH", headers, body: JSON.stringify(params.body) });
    return resp.status;
  }

  if (method === "DELETE") {
    for (const [k, v] of Object.entries(params)) {
      url.searchParams.set(k, v);
    }
    const resp = await fetch(url, { method: "DELETE", headers });
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    metadata: z.string().optional().describe("JSON metadata (e.g. {\"email\": \"vera@leanmarketing.com\", \"expires_at\": \"...\"})"),
    agent: z.string().optional().describe("Target agent (admin only — omit to connect for yourself)"),
    skill_description: z.string().optional().describe("Custom user-facing description for this skill (overrides default)"),
  },
  async ({ service, credentials, metadata, agent, skill_description }) => {
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const result = await supabaseQuery("skill_permissions", "POST", {
      body: {
        agent_name: agent,
        skill_name,
        granted_by: AGENT_NAME,
      },
      onConflict: "agent_name,skill_name",
    });

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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
// Primary store: Supabase harness_scheduled_tasks table.
// Local JSON file kept as cache for the executor (scheduler_executor.sh reads it).
// All writes go to Supabase first, then sync to local JSON.

import { readFileSync, writeFileSync, existsSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

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
  // Pull all tasks from Supabase and write to local JSON for the executor
  try {
    const tasks = await supabaseQuery("harness_scheduled_tasks", "GET", {
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
  },
  async ({ schedule, task_description, recurring, agent }) => {
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
      active: true,
      created_by: AGENT_NAME,
      created_at: new Date().toISOString(),
      last_fired_at: null,
    };

    // Write to Supabase first
    const result = await supabaseQuery("harness_scheduled_tasks", "POST", { body: taskData });

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
    const ADMIN_AGENTS = ["derek", "dereklm"];
    const showAll = agent === "*" && ADMIN_AGENTS.includes(AGENT_NAME);
    const targetAgent = showAll ? null : (agent && ADMIN_AGENTS.includes(AGENT_NAME) ? agent : AGENT_NAME);

    if (agent && agent !== "*" && !ADMIN_AGENTS.includes(AGENT_NAME)) {
      return { content: [{ type: "text", text: `Denied: only admin agents can list other agents' tasks.` }] };
    }

    // Read from Supabase
    const params = { "order": "agent_name,schedule" };
    if (targetAgent) params["agent_name"] = `eq.${targetAgent}`;
    if (active_only !== false) params["active"] = "eq.true";

    const tasks = await supabaseQuery("harness_scheduled_tasks", "GET", params) || [];

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
    const ADMIN_AGENTS = ["derek", "dereklm"];

    // Fetch the task from Supabase to verify ownership
    const tasks = await supabaseQuery("harness_scheduled_tasks", "GET", {
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
    await supabaseQuery("harness_scheduled_tasks", "DELETE", {
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
  },
  async ({ task_id, schedule, task_description, active }) => {
    const ADMIN_AGENTS = ["derek", "dereklm"];

    // Fetch from Supabase
    const tasks = await supabaseQuery("harness_scheduled_tasks", "GET", {
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

    // Update in Supabase
    await supabaseQuery("harness_scheduled_tasks", "PATCH", {
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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
    const ADMIN_AGENTS = ["derek", "dereklm"];
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

// --- Start ---

async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}

main().catch(console.error);
