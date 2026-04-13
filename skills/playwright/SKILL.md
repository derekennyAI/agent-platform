---
name: playwright
description: "Run headless Chromium via Playwright to visit websites, fill forms, click buttons, take screenshots, and extract rendered content. Use this — NOT the browser relay tool — when web_fetch fails due to JavaScript rendering, bot protection, or dynamic content. Works on flight search sites, contact forms, SPAs, and any page that blocks curl. Triggered by: 'use playwright', 'open in browser', 'screenshot this site', 'fill out the form on', 'can you visit', or when web_fetch returns a captcha/empty page."
---

# Playwright Skill

Run a real headless Chromium browser via exec. This is NOT the browser relay tool — no Chrome extension needed.

**This skill uses exec only.** The workflow is: write a JSON actions file → run `node browser.js` → read the output.

## Security constraints (always enforced)

- Isolated browser profile — no cookies shared with your real Chrome
- `file://` and `data:` URLs blocked at multiple levels
- Only structured actions allowed — no arbitrary JavaScript execution
- All navigations logged to `memory/browser-audit.log`
- Screenshots saved to `memory/screenshots/`

## How to use

### 1. Write an actions file

Create a JSON array of steps at a temp path, e.g. `/tmp/derek-actions.json`:

```json
[
  { "action": "navigate", "url": "https://example.com" },
  { "action": "screenshot", "path": "example-home.png" },
  { "action": "get_text", "selector": "h1" }
]
```

### 2. Run it

```bash
node skills/playwright/scripts/browser.js /tmp/derek-actions.json
```

> The script sets `PLAYWRIGHT_BROWSERS_PATH` automatically — no extra env vars needed.

Output is a JSON array with results for each step.

### 3. Read results

The script prints JSON to stdout. Parse it to get text, URLs, screenshot paths.

---

## Available actions

| Action | Required fields | Optional | Description |
|--------|----------------|----------|-------------|
| `navigate` | `url` | — | Load a URL (https:// only) |
| `click` | `selector` | — | Click an element |
| `fill` | `selector`, `value` | — | Type into an input |
| `select` | `selector`, `value` | — | Choose a dropdown option |
| `get_text` | `selector` | — | Extract text from element |
| `get_url` | — | — | Get current page URL |
| `screenshot` | — | `path`, `fullPage` | Save screenshot to memory/screenshots/ |
| `wait` | — | `ms` (max 10000) | Pause |
| `wait_for` | `selector` | `timeout` | Wait for element to appear |
| `hover` | `selector` | — | Hover over element |
| `press_key` | `key` | — | Press a keyboard key (e.g. "Enter", "Tab") |
| `scroll` | — | `distance` | Scroll down by pixels |

## Selectors

Use standard CSS selectors or text-based locators:
- `#id`, `.class`, `button`, `input[name="email"]`
- `text=Submit` — matches by visible text
- `role=button[name="Send"]` — ARIA role

## Common patterns

### Fill and submit a form
```json
[
  { "action": "navigate", "url": "https://site.com/contact" },
  { "action": "fill", "selector": "#name", "value": "Agent" },
  { "action": "fill", "selector": "#email", "value": "agent@example.com" },
  { "action": "fill", "selector": "#message", "value": "Hello from Agent!" },
  { "action": "screenshot", "path": "before-submit.png" },
  { "action": "click", "selector": "button[type=submit]" },
  { "action": "wait", "ms": 2000 },
  { "action": "screenshot", "path": "after-submit.png" },
  { "action": "get_text", "selector": "body" }
]
```

### Search a site
```json
[
  { "action": "navigate", "url": "https://site.com" },
  { "action": "fill", "selector": "input[type=search]", "value": "query" },
  { "action": "press_key", "key": "Enter" },
  { "action": "wait_for", "selector": ".results" },
  { "action": "get_text", "selector": ".results" }
]
```

### Take a full-page screenshot
```json
[
  { "action": "navigate", "url": "https://site.com" },
  { "action": "screenshot", "path": "full.png", "fullPage": true }
]
```

## Blocked / not available

- `file://` and `data:` URLs → aborted
- `page.evaluate()` / arbitrary JS → not exposed
- Saving files outside `memory/screenshots/` → path sanitized automatically
- Actions not in the list above → process exits with error

## Notes

- Exec must be approved when running `node browser.js ...`
- If a selector doesn't match, the step errors — check page structure first with a `screenshot` or `get_text` on `body`
- Dynamic pages: add a `wait` or `wait_for` step after navigation before interacting
- The isolated profile persists at `/tmp/derek-browser-isolated/` across runs (cookies from Derek's browsing accumulate there, never touching your real Chrome)
