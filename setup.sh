#!/bin/bash
# Agent Platform — One-command setup
# Usage: ./setup.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "=========================================="
echo "  Agent Platform Setup"
echo "=========================================="
echo ""

# --- 1. Check prerequisites ---
echo "[1/7] Checking prerequisites..."

MISSING=()

if ! command -v node &>/dev/null; then
    MISSING+=("node (brew install node)")
fi

if ! command -v python3 &>/dev/null; then
    MISSING+=("python3")
fi

if ! command -v tmux &>/dev/null; then
    MISSING+=("tmux (brew install tmux)")
fi

if ! command -v claude &>/dev/null; then
    MISSING+=("claude (Claude Code CLI — see https://docs.anthropic.com/en/docs/claude-code)")
fi

if [ ${#MISSING[@]} -gt 0 ]; then
    echo -e "${RED}Missing required tools:${NC}"
    for m in "${MISSING[@]}"; do
        echo "  - $m"
    done
    exit 1
fi

NODE_VER=$(node -v | sed 's/v//' | cut -d. -f1)
if [ "$NODE_VER" -lt 18 ]; then
    echo -e "${RED}Node.js 18+ required (found $(node -v))${NC}"
    exit 1
fi

echo -e "  ${GREEN}All prerequisites found${NC}"

# --- 2. Load or create .env ---
echo "[2/7] Checking configuration..."

if [ ! -f "$SCRIPT_DIR/.env" ]; then
    cp "$SCRIPT_DIR/.env.example" "$SCRIPT_DIR/.env"
    echo -e "  ${YELLOW}Created .env from .env.example — edit it with your API keys, then re-run this script.${NC}"
    echo ""
    echo "  Required:"
    echo "    ANTHROPIC_API_KEY    — from console.anthropic.com"
    echo "    SUPABASE_URL         — your Supabase project URL"
    echo "    SUPABASE_SERVICE_KEY — from Supabase Settings > API > service_role"
    echo ""
    exit 0
fi

# Source .env
set -a
source "$SCRIPT_DIR/.env"
set +a

# Validate required vars
REQUIRED_VARS=("ANTHROPIC_API_KEY" "SUPABASE_URL" "SUPABASE_SERVICE_KEY")
for var in "${REQUIRED_VARS[@]}"; do
    val="${!var}"
    if [ -z "$val" ] || [[ "$val" == *"..."* ]]; then
        echo -e "${RED}$var is not set or still has placeholder value. Edit .env and try again.${NC}"
        exit 1
    fi
done

echo -e "  ${GREEN}Configuration loaded${NC}"

# --- 3. Install MCP server dependencies ---
echo "[3/7] Installing MCP server dependencies..."

cd "$SCRIPT_DIR/mcp-server"
npm install --silent 2>&1 | tail -1
cd "$SCRIPT_DIR"

echo -e "  ${GREEN}MCP server ready${NC}"

# --- 4. Set up database schema ---
echo "[4/7] Setting up Supabase database..."

# Check if tables already exist by querying one
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" \
    "${SUPABASE_URL}/rest/v1/harness_skills?select=id&limit=1" \
    -H "apikey: ${SUPABASE_SERVICE_KEY}" \
    -H "Authorization: Bearer ${SUPABASE_SERVICE_KEY}" 2>/dev/null)

if [ "$HTTP_CODE" = "200" ]; then
    echo -e "  ${GREEN}Tables already exist — skipping${NC}"
else
    echo -e "  ${YELLOW}Tables not found. Run the SQL in schema/create_harness_schema.sql${NC}"
    echo "  in your Supabase dashboard > SQL Editor."
    echo ""
    echo "  After running the SQL, re-run this script to continue."
    echo ""
    # Don't exit — let the rest of setup continue, agent creation will fail if tables missing
fi

# --- 5. Make scripts executable ---
echo "[5/7] Setting permissions..."

chmod +x "$SCRIPT_DIR/scripts/launcher.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/mcp-server/scheduler_executor.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/mcp-server/refresh_agent_token.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/mcp-server/refresh_all_agents.sh" 2>/dev/null || true
chmod +x "$SCRIPT_DIR/scripts/security_watch.py" 2>/dev/null || true

echo -e "  ${GREEN}Done${NC}"

# --- 6. Set up scheduler crontab ---
echo "[6/7] Setting up scheduler..."

SCHEDULER_PATH="$SCRIPT_DIR/mcp-server/scheduler_executor.sh"
CRON_LINE="* * * * * $SCHEDULER_PATH >> /tmp/scheduler.log 2>&1"

# Check if already in crontab
if crontab -l 2>/dev/null | grep -qF "scheduler_executor.sh"; then
    echo -e "  ${GREEN}Scheduler already in crontab${NC}"
else
    # Add to crontab
    (crontab -l 2>/dev/null; echo "$CRON_LINE") | crontab -
    echo -e "  ${GREEN}Added scheduler to crontab (runs every minute)${NC}"
fi

# --- 7. Prompt for first agent ---
echo "[7/7] Ready to create your first agent!"
echo ""
echo "  Run:"
echo ""
echo "    python3 skills/agent-setup/create_agent.py \\"
echo "      --name <agent-name> \\"
echo "      --persona <display-name> \\"
echo "      --human <your-name> \\"
echo "      --bot-token <telegram-bot-token> \\"
echo "      --user-id <your-telegram-user-id>"
echo ""
echo "  Need a Telegram bot? Message @BotFather on Telegram → /newbot"
echo "  Need your user ID? Message @userinfobot on Telegram"
echo ""
echo "  After creating the agent, load its default crons:"
echo ""
echo "    python3 scripts/load_crons.py --agent <agent-name>"
echo ""
echo "=========================================="
echo -e "  ${GREEN}Setup complete!${NC}"
echo "=========================================="
echo ""
