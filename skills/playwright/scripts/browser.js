#!/usr/bin/env node
// Point playwright at the browser cache
const fs = require('fs');
const hostPath = process.env.HOME + '/.cache/ms-playwright';
process.env.PLAYWRIGHT_BROWSERS_PATH = fs.existsSync('/opt/pw-browsers') ? '/opt/pw-browsers' : hostPath;

/**
 * Constrained Playwright browser for Derek agent.
 *
 * Usage: node browser.js <actions.json>
 *
 * Action file is a JSON array of steps:
 * [
 *   { "action": "navigate", "url": "https://example.com" },
 *   { "action": "click",    "selector": "button.submit" },
 *   { "action": "fill",     "selector": "#email", "value": "test@example.com" },
 *   { "action": "select",   "selector": "#dropdown", "value": "option1" },
 *   { "action": "get_text", "selector": ".result" },
 *   { "action": "wait",     "ms": 1000 },
 *   { "action": "screenshot", "path": "out.png" }
 * ]
 *
 * Security constraints:
 *  - Isolated browser profile (no real cookies)
 *  - file:// URLs blocked
 *  - No page.evaluate() exposed
 *  - All navigations logged to workspace
 */

const { chromium } = require('playwright');
const path = require('path');
const os = require('os');

// Resolve relative to skill dir so it works in sandbox or real workspace
const SKILL_DIR = path.resolve(__dirname, '..');
// Write screenshots to workspace memory dir (accessible to gateway for media sending)
const WORKSPACE = process.env.DEREK_WORKSPACE || path.resolve(SKILL_DIR, '../..');
const SCREENSHOT_DIR = path.join(WORKSPACE, 'memory', 'screenshots');
const LOG_FILE = path.join(os.tmpdir(), 'derek-browser-audit.log');
const PROFILE_DIR = path.join(os.tmpdir(), 'derek-browser-isolated');

// Only these actions are allowed — no arbitrary JS execution
const ALLOWED_ACTIONS = new Set([
  'navigate', 'click', 'fill', 'select', 'get_text',
  'wait', 'screenshot', 'scroll', 'hover', 'press_key',
  'get_url', 'wait_for'
]);

function log(msg) {
  const ts = new Date().toISOString();
  const line = `[${ts}] ${msg}\n`;
  process.stderr.write(line);
  try {
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, line);
  } catch (_) {}
}

function blockFileUrls(url) {
  if (url.startsWith('file://') || url.startsWith('data:')) {
    throw new Error(`Blocked URL scheme: ${url}`);
  }
}

async function run(actionsFile) {
  let actions;
  try {
    actions = JSON.parse(fs.readFileSync(actionsFile, 'utf8'));
  } catch (e) {
    console.error(`Failed to parse actions file: ${e.message}`);
    process.exit(1);
  }

  if (!Array.isArray(actions)) {
    console.error('Actions must be a JSON array');
    process.exit(1);
  }

  // Validate all actions before launching browser
  for (const step of actions) {
    if (!ALLOWED_ACTIONS.has(step.action)) {
      console.error(`Blocked action: ${step.action}`);
      process.exit(1);
    }
    if (step.action === 'navigate') {
      blockFileUrls(step.url || '');
    }
  }

  fs.mkdirSync(SCREENSHOT_DIR, { recursive: true });
  fs.mkdirSync(PROFILE_DIR, { recursive: true });

  const browser = await chromium.launchPersistentContext(PROFILE_DIR, {
    headless: true,
  });

  // Block file:// and data: at route level
  await browser.route(/^(file|data):/, route => {
    log(`BLOCKED route: ${route.request().url()}`);
    route.abort();
  });

  const page = await browser.newPage();
  const results = [];

  try {
    for (const step of actions) {
      // Redact fill values from logs to avoid leaking passwords/secrets
      const logStep = step.action === 'fill'
        ? { ...step, value: '***' }
        : step;
      log(`ACTION: ${JSON.stringify(logStep)}`);

      switch (step.action) {
        case 'navigate': {
          blockFileUrls(step.url);
          await page.goto(step.url, { waitUntil: 'domcontentloaded', timeout: 30000 });
          const title = await page.title();
          results.push({ action: 'navigate', url: step.url, title });
          break;
        }

        case 'click': {
          await page.locator(step.selector).first().click({ timeout: 10000 });
          results.push({ action: 'click', selector: step.selector, ok: true });
          break;
        }

        case 'fill': {
          await page.locator(step.selector).first().fill(step.value || '', { timeout: 10000 });
          results.push({ action: 'fill', selector: step.selector, ok: true });
          break;
        }

        case 'select': {
          await page.locator(step.selector).first().selectOption(step.value, { timeout: 10000 });
          results.push({ action: 'select', selector: step.selector, value: step.value, ok: true });
          break;
        }

        case 'get_text': {
          const el = page.locator(step.selector).first();
          const text = await el.textContent({ timeout: 10000 });
          results.push({ action: 'get_text', selector: step.selector, text: (text || '').trim().slice(0, 2000) });
          break;
        }

        case 'wait': {
          await page.waitForTimeout(Math.min(step.ms || 1000, 10000));
          results.push({ action: 'wait', ms: step.ms });
          break;
        }

        case 'wait_for': {
          await page.locator(step.selector).first().waitFor({ timeout: step.timeout || 10000 });
          results.push({ action: 'wait_for', selector: step.selector, ok: true });
          break;
        }

        case 'screenshot': {
          const fname = step.path
            ? path.basename(step.path).replace(/[^a-z0-9_.-]/gi, '_')
            : `screenshot-${Date.now()}.png`;
          const fullPath = path.join(SCREENSHOT_DIR, fname);
          await page.screenshot({ path: fullPath, fullPage: !!step.fullPage });
          results.push({ action: 'screenshot', path: fullPath });
          break;
        }

        case 'scroll': {
          await page.mouse.wheel(0, step.distance || 500);
          results.push({ action: 'scroll', ok: true });
          break;
        }

        case 'hover': {
          await page.locator(step.selector).first().hover({ timeout: 10000 });
          results.push({ action: 'hover', selector: step.selector, ok: true });
          break;
        }

        case 'press_key': {
          await page.keyboard.press(step.key || 'Enter');
          results.push({ action: 'press_key', key: step.key, ok: true });
          break;
        }

        case 'get_url': {
          results.push({ action: 'get_url', url: page.url() });
          break;
        }

        default:
          results.push({ action: step.action, error: 'unknown action' });
      }
    }
  } catch (e) {
    log(`ERROR: ${e.message}`);
    results.push({ error: e.message });
  } finally {
    await browser.close();
  }

  console.log(JSON.stringify(results, null, 2));
}

const actionsFile = process.argv[2];
if (!actionsFile) {
  console.error('Usage: node browser.js <actions.json>');
  process.exit(1);
}

run(actionsFile).catch(e => {
  console.error(e.message);
  process.exit(1);
});
