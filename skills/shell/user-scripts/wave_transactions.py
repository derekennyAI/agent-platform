#!/usr/bin/env python3
"""Scrape transactions from Wave web app using Playwright.

Logs in as YOUR_BUSINESS_EMAIL, handles email OTP verification,
stores session cookies to avoid re-verification, then exports
transactions for the given date range.

Usage:
    python3 wave_transactions.py --start 2026-03-01 --end 2026-03-26
    python3 wave_transactions.py --start 2026-01-01 --end 2026-03-26 --csv-out /tmp/wave_tx.csv
    python3 wave_transactions.py --reauth    # force fresh login + OTP
"""

import argparse
import base64
import csv
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

_SESSION_FILE = Path("/Users/YOUR_MAC_USERNAME/.claude/wave_session.json")
WAVE_NEXT = "https://next.waveapps.com"
# ennyAI Wave business UUID (same as wave_enny_report.py BUSINESS_ID decoded)
ENNY_BIZ_UUID = "43d6584f-1482-489a-848b-19068697fff7"
PLAYWRIGHT_BROWSERS = str(Path.home() / ".cache/ms-playwright")
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


# ── Gmail helpers ──────────────────────────────────────────────────────────────

_AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
_AGENT_WORKSPACE = Path(f"/Users/YOUR_MAC_USERNAME/{_AGENT_NAME}")
_AGENT_CONFIG = _AGENT_WORKSPACE / ".config" / _AGENT_NAME
_AGENT_ACCOUNTS = _AGENT_CONFIG / "accounts"


def _find_disk_token():
    """Find best token file for this agent."""
    if _AGENT_ACCOUNTS.exists():
        for d in sorted(_AGENT_ACCOUNTS.iterdir()):
            if d.is_dir() and (d / "google-token.json").exists():
                return d / "google-token.json"
    root = _AGENT_CONFIG / "google-token.json"
    return root if root.exists() else None


def _find_disk_creds():
    """Find best credentials file for this agent."""
    if _AGENT_ACCOUNTS.exists():
        for d in sorted(_AGENT_ACCOUNTS.iterdir()):
            if d.is_dir() and (d / "google-credentials.json").exists():
                return d / "google-credentials.json"
    root = _AGENT_CONFIG / "google-credentials.json"
    return root if root.exists() else None


def _load_google_token():
    """Load Google OAuth token — vault first (agent-scoped), disk fallback."""
    from vault_client import get_credential as _vault_get
    try:
        raw = _vault_get("gmail", "oauth_token", agent=_AGENT_NAME)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    p = _find_disk_token()
    if p and p.exists():
        with open(p) as f:
            return json.load(f)
    raise RuntimeError(f"No Google token found for agent '{_AGENT_NAME}'")


def _load_google_creds():
    """Load Google OAuth credentials — vault first (agent-scoped), disk fallback."""
    from vault_client import get_credential as _vault_get
    try:
        raw = _vault_get("gmail", "oauth_credentials", agent=_AGENT_NAME)
        if raw:
            return json.loads(raw)
    except Exception:
        pass
    p = _find_disk_creds()
    if p and p.exists():
        with open(p) as f:
            return json.load(f)
    raise RuntimeError(f"No Google credentials found for agent '{_AGENT_NAME}'")


def _gmail_token():
    token_data = _load_google_token()
    creds = _load_google_creds()
    token_path = _find_disk_token()
    return token_data, token_path, creds


def _refresh_gmail(token_data, token_path, creds):
    client = creds.get("installed", creds)
    data = urllib.parse.urlencode({
        "client_id": client["client_id"],
        "client_secret": client["client_secret"],
        "refresh_token": token_data["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
    with urllib.request.urlopen(req) as r:
        new_token = json.loads(r.read())
    new_token["refresh_token"] = token_data["refresh_token"]
    token_path.write_text(json.dumps(new_token, indent=2))
    return new_token["access_token"]


def _gmail_get(path, access_token):
    req = urllib.request.Request(f"{GMAIL_API}{path}")
    req.add_header("Authorization", f"Bearer {access_token}")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 401:
            return None
        raise


def get_wave_otp(after_ts: int, max_wait: int = 60) -> str | None:
    """Poll YOUR_BUSINESS_EMAIL Gmail for a Wave verification code sent after after_ts.
    Returns the 6-digit code or None if not found within max_wait seconds."""
    token_data, token_path, creds = _gmail_token()
    access_token = token_data.get("access_token", "")

    deadline = time.time() + max_wait
    while time.time() < deadline:
        # Search for Wave verification emails
        result = _gmail_get(
            f"/users/me/messages?q=from:noreply@waveapps.com+subject:verify&maxResults=5",
            access_token
        )
        if result is None:
            access_token = _refresh_gmail(token_data, token_path, creds)
            result = _gmail_get(
                f"/users/me/messages?q=from:noreply@waveapps.com+subject:verify&maxResults=5",
                access_token
            )

        messages = (result or {}).get("messages", [])
        for msg_ref in messages:
            msg = _gmail_get(f"/users/me/messages/{msg_ref['id']}", access_token)
            if not msg:
                continue
            # Check timestamp
            internal_date = int(msg.get("internalDate", 0)) // 1000
            if internal_date < after_ts:
                continue
            # Extract body
            body = _extract_gmail_body(msg)
            # Look for 6-digit code
            codes = re.findall(r'\b(\d{6})\b', body)
            if codes:
                print("[wave] Found OTP code in email", file=sys.stderr)
                return codes[0]

        print(f"[wave] Waiting for OTP email... ({int(deadline - time.time())}s left)", file=sys.stderr)
        time.sleep(5)

    return None


def _extract_gmail_body(msg: dict) -> str:
    """Extract plain text or HTML body from Gmail message."""
    payload = msg.get("payload", {})

    def get_parts(part):
        parts = []
        mime = part.get("mimeType", "")
        if mime in ("text/plain", "text/html"):
            data = part.get("body", {}).get("data", "")
            if data:
                parts.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace"))
        for sub in part.get("parts", []):
            parts.extend(get_parts(sub))
        return parts

    texts = get_parts(payload)
    return " ".join(texts)


# ── Session cookie management ──────────────────────────────────────────────────

def save_session(cookies: list):
    _SESSION_FILE.write_text(json.dumps({"cookies": cookies, "saved": time.time()}, indent=2))
    print(f"[wave] Session saved ({len(cookies)} cookies)", file=sys.stderr)


def load_session() -> list | None:
    if not _SESSION_FILE.exists():
        return None
    try:
        data = json.loads(_SESSION_FILE.read_text())
        # Expire session after 7 days
        if time.time() - data.get("saved", 0) > 7 * 86400:
            print("[wave] Stored session expired", file=sys.stderr)
            return None
        return data.get("cookies", [])
    except Exception:
        return None


# ── Main Playwright scraper ────────────────────────────────────────────────────

def scrape_transactions(start: date, end: date, csv_out: str = None,
                        screenshot_dir: str = None, reauth: bool = False):
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    secrets = load_secrets()
    email = secrets["wave_email"]
    password = secrets["wave_password"]

    def snap(name):
        if screenshot_dir:
            p = Path(screenshot_dir) / f"{name}.png"
            page.screenshot(path=str(p))
            print(f"[screenshot] {p}", file=sys.stderr)

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        # --- Try stored session first ---
        logged_in = False
        if not reauth:
            cookies = load_session()
            if cookies:
                print("[wave] Loading stored session cookies...", file=sys.stderr)
                ctx.add_cookies(cookies)
                # Test if session is still valid — use networkidle so SPA fully loads
                page.goto(f"{WAVE_NEXT}/dashboard", wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PWTimeout:
                    pass
                # Give SPA time to redirect to a business-specific URL
                page.wait_for_timeout(3000)
                snap("01_session_test")
                if "login" not in page.url and "auth" not in page.url:
                    print(f"[wave] Session valid: {page.url}", file=sys.stderr)
                    logged_in = True
                else:
                    print("[wave] Stored session expired, re-logging in", file=sys.stderr)

        # --- Full login flow ---
        if not logged_in:
            login_ts = int(time.time())
            print("[wave] Logging in...", file=sys.stderr)
            page.goto(f"{WAVE_NEXT}/login", wait_until="domcontentloaded", timeout=30000)
            snap("02_login_page")

            # Fill and submit form
            page.wait_for_selector('input[type="email"], input[name="username"]', timeout=10000)
            page.fill('input[type="email"], input[name="username"]', email)
            page.fill('input[type="password"]', password)
            snap("03_login_filled")
            page.click('button[type="submit"]')

            # Wait for navigation — Wave SPA can take a while to settle.
            # We wait for the URL to leave login, but fall back to checking page content.
            try:
                page.wait_for_url(lambda url: "login" not in url, timeout=20000)
            except PWTimeout:
                pass

            # Give SPA extra time to complete routing (handles auth/verify → onboarding transition)
            page.wait_for_timeout(4000)

            snap("04_after_submit")
            print(f"[wave] After submit: {page.url}", file=sys.stderr)

            # auth/verify: only treat as a blocking step if an OTP input is visible
            if "verify" in page.url.lower():
                snap("05_verify_page")
                otp_input = page.query_selector(
                    'input[autocomplete="one-time-code"], input[type="number"], '
                    'input[type="text"][maxlength="6"]'
                )
                if otp_input:
                    # Try TOTP first (authenticator app), fall back to email OTP
                    otp = None
                    totp_secret = secrets.get("wave_totp_secret")
                    if totp_secret:
                        try:
                            import pyotp
                            totp = pyotp.TOTP(totp_secret)
                            otp = totp.now()
                            print(f"[wave] Generated TOTP code from saved secret", file=sys.stderr)
                        except Exception as e:
                            print(f"[wave] TOTP generation failed: {e}", file=sys.stderr)

                    if not otp:
                        print("[wave] No TOTP secret — polling Gmail for email OTP...", file=sys.stderr)
                        otp = get_wave_otp(after_ts=login_ts, max_wait=90)

                    if not otp:
                        print("[error] No OTP available (TOTP failed and no email OTP received)", file=sys.stderr)
                        browser.close()
                        return []
                    otp_input.fill(otp)
                    snap("06_otp_filled")
                    submit_btn = page.query_selector('button[type="submit"]')
                    if submit_btn:
                        submit_btn.click()
                    else:
                        page.keyboard.press("Enter")
                    try:
                        page.wait_for_url(lambda url: "verify" not in url, timeout=15000)
                    except PWTimeout:
                        pass
                    snap("07_after_otp")
                    print(f"[wave] After OTP: {page.url}", file=sys.stderr)
                else:
                    print(f"[wave] auth/verify with no input — still routing ({page.url})", file=sys.stderr)
                    # wait a bit more
                    page.wait_for_timeout(4000)
                    print(f"[wave] URL after extra wait: {page.url}", file=sys.stderr)

            # Confirm login — check URL and also whether the login form is still visible
            login_form_visible = bool(page.query_selector('input[type="password"]'))
            if ("login" in page.url or "verify" in page.url) and login_form_visible:
                snap("08_login_failed")
                print(f"[error] Login failed, still at: {page.url}", file=sys.stderr)
                browser.close()
                return []
            elif "login" in page.url and not login_form_visible:
                # URL says login but form is gone — SPA navigated, URL will update shortly
                print(f"[wave] URL still shows login but form gone — waiting for URL update...", file=sys.stderr)
                page.wait_for_timeout(3000)
                print(f"[wave] URL after wait: {page.url}", file=sys.stderr)

            # Complete onboarding if needed (new account)
            if "onboarding" in page.url:
                print("[wave] Completing onboarding...", file=sys.stderr)
                if not _complete_onboarding(page, snap):
                    print("[error] Onboarding failed", file=sys.stderr)
                    browser.close()
                    return []

            # Save session cookies
            all_cookies = ctx.cookies()
            save_session(all_cookies)
            logged_in = True

        # --- Navigate to Accounting > Transactions ---
        print("[wave] Navigating to transactions...", file=sys.stderr)

        # Wait for SPA to settle (may redirect to /onboarding after login)
        page.wait_for_timeout(3000)
        current_url = page.url
        print(f"[wave] Current URL: {current_url}", file=sys.stderr)

        # Handle onboarding that may occur after session restore or login
        if "onboarding" in current_url:
            print("[wave] Completing onboarding...", file=sys.stderr)
            _complete_onboarding(page, snap)
            page.wait_for_timeout(2000)
            current_url = page.url
            print(f"[wave] After onboarding: {current_url}", file=sys.stderr)

        # Wave Next: look for ennyAI business in business switcher,
        # then navigate to that business's transactions
        biz_slug = _find_enny_biz_slug(page)
        if biz_slug:
            tx_url = f"{WAVE_NEXT}/{biz_slug}/transactions"
            print(f"[wave] Found ennyAI slug: {biz_slug}", file=sys.stderr)
        else:
            print("[error] Could not find any business slug — cannot build transactions URL", file=sys.stderr)
            snap("no_slug")
            browser.close()
            return []

        print(f"[wave] Going to: {tx_url}", file=sys.stderr)
        page.goto(tx_url, wait_until="domcontentloaded", timeout=30000)
        snap("09_tx_page")

        # Wait for network to become idle (data loaded from API)
        try:
            page.wait_for_load_state("networkidle", timeout=30000)
            print("[wave] Network idle on TX page", file=sys.stderr)
        except PWTimeout:
            print("[wave] Network didn't become idle, continuing anyway", file=sys.stderr)

        snap("09b_after_networkidle")
        print(f"[wave] TX URL: {page.url}", file=sys.stderr)

        # If redirected away, look for transactions nav link
        if "transaction" not in page.url.lower():
            # Try clicking Accounting nav item to reveal submenu, then find Transactions link
            acct_li = page.query_selector("li.wv-app-menu__item:has-text('Accounting')")
            if acct_li:
                acct_li.click()
                page.wait_for_timeout(1000)
            link = page.query_selector("a:has-text('Transactions'), a[href*='/transactions']")
            if link:
                href = link.get_attribute("href") or ""
                if href.startswith("/"):
                    href = f"{WAVE_NEXT}{href}"
                print(f"[wave] Found tx link: {href}", file=sys.stderr)
                page.goto(href, wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=30000)
                except PWTimeout:
                    pass
                snap("10_tx_via_link")

        snap("11_final_tx_page")
        print(f"[wave] Final TX URL: {page.url}", file=sys.stderr)

        # Log visible business name / headings to confirm we're in the right account
        try:
            for sel in ["h1", "h2", "[class*='BusinessName']", "[class*='business-name']",
                        "[class*='org-name']", "nav [class*='name']"]:
                el = page.query_selector(sel)
                if el:
                    print(f"[wave] Page heading ({sel}): {el.inner_text().strip()[:80]}", file=sys.stderr)
                    break
        except Exception:
            pass

        # Dismiss any 2FA / security prompts that appear on the page
        _dismiss_security_prompts(page)

        snap("11b_after_security_dismiss")
        print(f"[wave] URL after security dismiss: {page.url}", file=sys.stderr)

        # Wait for any loading skeletons / spinners to disappear
        try:
            page.wait_for_selector(
                "[class*='skeleton'], [class*='Skeleton'], [class*='loading-placeholder']",
                state="hidden", timeout=20000
            )
            print("[wave] Loading skeleton gone", file=sys.stderr)
        except PWTimeout:
            print("[wave] Skeleton wait timed out (may already be gone)", file=sys.stderr)

        # Wait for transaction rows to appear
        tx_row_sel = (
            "table tbody tr, "
            "[data-testid*='transaction-row'], [data-testid*='TransactionRow'], "
            "[class*='transaction-row'], [class*='TransactionRow'], "
            "[class*='wv-table__row'], [class*='wv-list-item'], "
            "[class*='list-item'], [class*='ListItem']"
        )
        try:
            page.wait_for_selector(tx_row_sel, timeout=25000)
            print("[wave] Transaction rows visible", file=sys.stderr)
        except PWTimeout:
            snap("12_timeout")
            print("[wave] Timed out waiting for transaction rows", file=sys.stderr)
            # Dump page text to diagnose
            body_text = page.inner_text("body")
            print(f"[wave] Page body (first 3000 chars):\n{body_text[:3000]}", file=sys.stderr)

        snap("12_rows_ready")

        # --- Apply date filter via URL query params (Wave supports this) ---
        current_url = page.url
        if "?" not in current_url:
            # Try adding date range to URL
            date_url = (f"{current_url}?start={start.strftime('%Y-%m-%d')}"
                       f"&end={end.strftime('%Y-%m-%d')}")
            print(f"[wave] Trying date URL: {date_url}", file=sys.stderr)
            page.goto(date_url, wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeout:
                pass
            try:
                page.wait_for_selector(tx_row_sel, timeout=20000)
            except PWTimeout:
                pass
            snap("12b_date_filtered")
            print(f"[wave] Date-filtered URL: {page.url}", file=sys.stderr)

        # --- Try CSV export ---
        transactions = _try_export(page, start, end, csv_out, snap)

        if not transactions:
            # Fall back to table scraping
            snap("13_fallback_scrape")
            transactions = _scrape_table(page, start, end)

        browser.close()

    return transactions


def _select_first_dropdown_option(page, label_text: str) -> bool:
    """Click a labeled dropdown and select the first option."""
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        # Try native <select> first
        sel_el = page.query_selector(f'select[aria-label*="{label_text}" i]')
        if not sel_el:
            # Find by nearby label text
            sel_el = page.evaluate_handle(
                f"""() => {{
                    const labels = [...document.querySelectorAll('label, p, div')];
                    const lbl = labels.find(l => l.textContent.includes('{label_text}'));
                    if (lbl) {{
                        const container = lbl.closest('div, fieldset');
                        if (container) return container.querySelector('select');
                    }}
                    return null;
                }}"""
            )

        # Try select_option on native <select>
        if sel_el:
            try:
                options = page.eval_on_selector(
                    f'select',
                    "s => s ? [...s.options].slice(1).map(o => o.value) : []"
                )
            except Exception:
                options = []

        # Custom dropdown: click the trigger, then click first visible option
        # Find all select-like elements by their placeholder text
        trigger = page.query_selector(f'[class*="select" i]:has-text("{label_text}")')
        if not trigger:
            # Try the parent div of the placeholder text
            trigger = page.evaluate_handle(
                f"""() => {{
                    const els = [...document.querySelectorAll('div, span, button')];
                    return els.find(e => e.textContent.trim() === 'Select your {label_text.lower()}') || null;
                }}"""
            )

        if trigger:
            trigger.click()
            page.wait_for_timeout(500)
            # Click first option in the list that appeared
            option = page.query_selector('[role="option"], [class*="option" i]:not([aria-disabled="true"])')
            if option:
                option.click()
                return True

        # Last resort: find any select and try select_option
        selects = page.query_selector_all("select")
        for s in selects:
            placeholder_val = s.evaluate("el => el.options[0]?.text || ''")
            if label_text.lower() in placeholder_val.lower():
                s.select_option(index=1)
                return True

    except Exception as e:
        print(f"[wave] Dropdown select failed for '{label_text}': {e}", file=sys.stderr)
    return False


def _setup_2fa_if_needed(page, snap) -> bool:
    """Complete Wave 2FA setup flow. Uses saved TOTP secret if available, or extracts new one."""

    # Check if a 2FA modal or setup page is present
    qr_indicator = page.query_selector(
        ".wv-modal__window, h1, h2, h3"
    )
    if qr_indicator:
        txt = qr_indicator.inner_text().strip().lower()
        if not any(k in txt for k in ("two-factor", "authenticator", "one-time", "one last step", "scan")):
            return False
    else:
        return False

    snap("2fa_01_detected")
    print("[wave] 2FA setup detected", file=sys.stderr)

    try:
        import pyotp
        secrets = load_secrets()

        # Step A: Always extract the CURRENT session's TOTP secret from the QR page.
        # Wave generates a new secret per enrollment attempt, so saved secrets go stale.
        secret = None

        # If we're on the QR code step (step 2), extract the secret first
        page_text = page.inner_text("body")
        if "scan this qr" in page_text.lower() or "scan the qr" in page_text.lower() or "setup key" in page_text.lower():
            print("[wave] On QR code step — extracting secret", file=sys.stderr)

            # If QR code failed to load, click Retry
            retry = page.query_selector("button:has-text('Retry'), a:has-text('Retry')")
            if retry:
                print("[wave] QR code failed to load — retrying", file=sys.stderr)
                retry.click()
                page.wait_for_timeout(3000)

            # Click "Manually enter setup key" link to reveal the text secret
            manual = page.query_selector(
                "a:has-text('Manually enter setup key'), a:has-text('Manually enter'), "
                "a:has-text('enter setup key'), a:has-text('manual')"
            )
            if manual:
                manual.click(force=True, timeout=5000)
                page.wait_for_timeout(1500)
                snap("2fa_02_manual_key")

            # Extract secret from page
            for sel in ["code", "input[readonly]", "[class*='secret']", "[class*='key']", "kbd", "pre", "span"]:
                el = page.query_selector(sel)
                if el:
                    try:
                        val = el.input_value()
                    except Exception:
                        val = el.inner_text().strip()
                    val = re.sub(r'\s+', '', val).upper()
                    if len(val) >= 16 and all(c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567=" for c in val):
                        secret = val.rstrip("=")
                        break

            if not secret:
                text = page.inner_text("body")
                matches = re.findall(r'\b([A-Z2-7]{16,})\b', text)
                for m in matches:
                    if len(m) >= 16:
                        secret = m
                        break

            if secret:
                secrets["wave_totp_secret"] = secret
                _SECRETS.write_text(json.dumps(secrets, indent=2))
                print(f"[wave] Extracted & saved TOTP secret ({len(secret)} chars)", file=sys.stderr)
            else:
                print("[wave] Could not extract TOTP secret from QR page", file=sys.stderr)

            # Click Continue to advance to verification step
            cont = page.query_selector("button:has-text('Continue'):not([disabled])")
            if cont:
                cont.click(force=True)
                page.wait_for_timeout(2000)
                snap("2fa_02_past_qr")
        else:
            print(f"[wave] Not on QR page — using saved secret", file=sys.stderr)

        # Fall back to saved secret if we couldn't extract
        if not secret:
            secret = secrets.get("wave_totp_secret")

        if not secret:
            print("[wave] No TOTP secret available, skipping 2FA", file=sys.stderr)
            skip = page.query_selector("a:has-text('Skip for now')")
            if skip:
                skip.click(force=True, timeout=3000)
                page.wait_for_timeout(2000)
            return False

        # Step B: Enter the TOTP code (6 individual boxes or single input)
        totp = pyotp.TOTP(secret)
        code = totp.now()
        print("[wave] Generated TOTP code", file=sys.stderr)

        snap("2fa_pre_code_entry")
        # Wave's 6-box OTP uses type="tel" with autocomplete="one-time-code"
        otp_inputs = page.query_selector_all('input[autocomplete="one-time-code"], input[type="tel"]')
        # Filter to visible inputs inside the modal
        otp_inputs = [inp for inp in otp_inputs if inp.is_visible()]
        print(f"[wave] OTP inputs visible: {len(otp_inputs)}", file=sys.stderr)

        if len(otp_inputs) >= 6:
            for i, digit in enumerate(code[:6]):
                otp_inputs[i].click()
                otp_inputs[i].type(digit, delay=50)
        elif otp_inputs:
            otp_inputs[0].click()
            otp_inputs[0].type(code, delay=50)
        else:
            # Fallback: type into whatever is focused
            page.keyboard.type(code)
        page.wait_for_timeout(1500)
        snap("2fa_code_entered")
        print(f"[wave] After code entry, URL: {page.url}", file=sys.stderr)

        # Step C: May auto-advance — click Continue/Verify if still on code page
        for btn_sel in [
            "button:has-text('Continue'):not([disabled])",
            "button:has-text('Verify'):not([disabled])",
            "button[type='submit']:not([disabled])",
        ]:
            btn = page.query_selector(btn_sel)
            if btn:
                btn.click()
                page.wait_for_timeout(3000)
                snap("2fa_after_verify")
                break

        # Step D: Recovery keys page
        # Wave requires clicking "Download" to enable the "Continue" button.
        page.wait_for_timeout(1000)
        page_text = page.inner_text("body").lower()
        if "recovery" in page_text or "save recovery" in page_text:
            snap("2fa_recovery_keys")
            print("[wave] Recovery keys page — downloading to enable Continue", file=sys.stderr)
            # Click Download to enable the Continue button
            # Set up download handler to avoid hanging on headless file dialog
            with page.expect_download(timeout=8000) as dl_info:
                dl_btn = page.query_selector("button:has-text('Download')")
                if dl_btn:
                    dl_btn.click()
            page.wait_for_timeout(1500)

            # Continue should now be enabled
            cont = page.query_selector("button:has-text('Continue')")
            if cont:
                print("[wave] Clicking Continue on recovery keys", file=sys.stderr)
                cont.click(force=True)
                page.wait_for_timeout(2000)
                snap("2fa_recovery_done")

            # "You're all set" confirmation screen — click Done
            page_text2 = page.inner_text("body").lower()
            if "you're all set" in page_text2 or "all set with two-factor" in page_text2:
                print("[wave] 2FA all-set screen — clicking Done", file=sys.stderr)
                done = page.query_selector("button:has-text('Done')")
                if done:
                    done.click()
                    page.wait_for_timeout(3000)
                    snap("2fa_all_done")

        # After 2FA step, the gateway page ("One last step") needs to be cleared.
        # If 2FA modal is still open (QR error, etc.), close it via Cancel first,
        # then click "Skip for now" on the underlying gateway page.
        page.wait_for_timeout(1500)
        page_text3 = page.inner_text("body").lower()
        if "one last step" in page_text3 or ("two-factor" in page_text3 and "set it up" in page_text3):
            # Close modal if open (Cancel button inside the modal)
            modal = page.query_selector('[role="dialog"][aria-modal="true"]')
            if modal:
                cancel = modal.query_selector("button:has-text('Cancel')")
                if cancel:
                    print("[wave] Closing 2FA modal with Cancel", file=sys.stderr)
                    cancel.click()
                    page.wait_for_timeout(2000)
                    snap("2fa_modal_cancelled")

            # Now click Skip for now on the gateway
            skip = page.query_selector("a:has-text('Skip for now')")
            if skip and skip.is_visible():
                print("[wave] Clicking Skip for now on 2FA gateway", file=sys.stderr)
                skip.click()
                page.wait_for_timeout(3000)
                snap("2fa_gateway_skipped")

        print(f"[wave] 2FA complete, URL: {page.url}", file=sys.stderr)
        return True

    except Exception as e:
        print(f"[wave] 2FA error: {e}", file=sys.stderr)

    return False


def _complete_onboarding(page, snap) -> bool:
    """Complete Wave's new-account onboarding wizard."""
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        max_steps = 4
        for step in range(max_steps):
            if "onboarding" not in page.url:
                break

            snap(f"onboard_{step:02d}_start")
            print(f"[wave] Onboarding step {step + 1}, URL: {page.url}", file=sys.stderr)

            # Fill text inputs
            for placeholder, value in [
                ("First name", "Derek"),
                ("Last name", "Newman"),
                ("business name", "Derek Newman"),
            ]:
                el = page.query_selector(f'input[placeholder*="{placeholder}" i]')
                if el:
                    current = el.input_value()
                    if not current:
                        el.fill(value)

            # Handle Wave's custom .wv-select dropdowns (industry, etc.)
            wv_selects = page.query_selector_all(".wv-select__toggle")
            for toggle in wv_selects:
                # Check if already selected (label != placeholder)
                label = toggle.query_selector(".wv-select__label")
                if label:
                    txt = label.inner_text().strip()
                    if txt.startswith("Select "):  # still placeholder
                        toggle.click()
                        page.wait_for_timeout(600)
                        # Click first real option
                        first_opt = page.query_selector(".wv-select__menu__option")
                        if first_opt:
                            first_opt.click()
                            page.wait_for_timeout(400)

            # Handle native <select> dropdowns (business type)
            page.evaluate("""
                () => {
                    const selects = document.querySelectorAll('select');
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLSelectElement.prototype, 'value'
                    ).set;
                    selects.forEach(s => {
                        if (!s.value && s.options.length > 1) {
                            setter.call(s, s.options[1].value);
                            s.dispatchEvent(new Event('input', { bubbles: true }));
                            s.dispatchEvent(new Event('change', { bubbles: true }));
                        }
                    });
                }
            """)
            page.wait_for_timeout(500)

            # Handle Wave radio buttons (label.wv-radio wraps hidden input)
            radio_labels = page.query_selector_all("label.wv-radio")
            if radio_labels:
                # Click first radio (customer count: 0-1)
                radio_labels[0].click()
                page.wait_for_timeout(200)
                # Find the last group (paid software Yes/No) — click "No"
                for rl in reversed(radio_labels):
                    txt = rl.inner_text().strip()
                    if txt in ("No", "Yes"):
                        rl.click()
                        page.wait_for_timeout(200)
                        break

            snap(f"onboard_{step:02d}_filled")

            # Handle checkboxes
            checkboxes = page.query_selector_all('input[type="checkbox"]:not(:checked)')
            for cb in checkboxes[:2]:
                try:
                    cb.click()
                    page.wait_for_timeout(200)
                except Exception:
                    pass

            # Handle tile-selection steps ("What would you like to do?")
            # Tiles are large clickable cards — click first one to enable Next
            tiles = page.query_selector_all(
                "[class*='tile'], [class*='card'], [role='checkbox'], "
                "[class*='option-card'], [class*='feature']"
            )
            if not tiles:
                # Fallback: look for div buttons with icon+text structure
                tiles = page.query_selector_all("button[class*='wv-']:not([type='submit'])")
            if tiles:
                try:
                    tiles[0].click()
                    page.wait_for_timeout(400)
                except Exception:
                    pass

            # Click Next/Continue/Submit — wait for it to be enabled
            page.wait_for_timeout(500)
            for btn_sel in [
                'button:has-text("Next"):not([disabled])',
                'button:has-text("Continue"):not([disabled])',
                'button:has-text("Get started"):not([disabled])',
                'button:has-text("Skip"):not([disabled])',
                'button[type="submit"]:not([disabled])',
            ]:
                btn = page.query_selector(btn_sel)
                if btn:
                    print(f"[wave] Clicking: {btn_sel}", file=sys.stderr)
                    btn.click()
                    # Wait for loading to finish — button disappears or URL changes
                    try:
                        page.wait_for_function(
                            f"""() => {{
                                const btn = document.querySelector('{btn_sel.replace("'", "\\'")}');
                                return !btn || !btn.disabled;
                            }}""",
                            timeout=12000
                        )
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)
                    snap(f"onboard_{step:02d}_after_click")
                    break

        # Handle 2FA/security setup — complete it so we have persistent access
        _setup_2fa_if_needed(page, snap)

        print(f"[wave] Onboarding done, now at: {page.url}", file=sys.stderr)
        return True

    except Exception as e:
        print(f"[wave] Onboarding error: {e}", file=sys.stderr)
        return True  # continue anyway


def _find_enny_biz_slug(page) -> str | None:
    """Find the ennyAI business slug via the Wave business switcher or page links.

    Wave business slugs are UUIDs like 5c07ede9-c618-4f34-a08d-83e75cbf5801.
    """
    from playwright.sync_api import TimeoutError as PWTimeout

    # UUID pattern
    UUID_RE = re.compile(r'[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}')

    try:
        # 0. Try the known ennyAI business UUID directly
        test_url = f"{WAVE_NEXT}/{ENNY_BIZ_UUID}/dashboard"
        print(f"[wave] Testing known ennyAI slug: {ENNY_BIZ_UUID}", file=sys.stderr)
        page.goto(test_url, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)
        # If we're still on that URL and it didn't redirect to /login or 404,
        # check the page text for an error indicator
        if ENNY_BIZ_UUID in page.url and "404" not in page.inner_text("body")[:100]:
            print(f"[wave] ennyAI slug works: {ENNY_BIZ_UUID}", file=sys.stderr)
            return ENNY_BIZ_UUID
        print(f"[wave] Known ennyAI slug failed (URL: {page.url}), trying discovery...", file=sys.stderr)
        # Return to dashboard for discovery
        page.goto(f"{WAVE_NEXT}/dashboard", wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2000)

        # 1. Check current URL first
        m = UUID_RE.search(page.url)
        if m:
            slug = m.group()
            print(f"[wave] Slug from current URL: {slug}", file=sys.stderr)
            return slug

        # 2. Look for ennyAI-specific links (exact name match)
        enny_selectors = [
            "a:has-text('ennyAI')",
            "button:has-text('ennyAI')",
            "a:has-text('enny')",
            "[data-testid*='ennyai' i]",
        ]
        for sel in enny_selectors:
            el = page.query_selector(sel)
            if el:
                href = el.get_attribute("href") or ""
                m = UUID_RE.search(href)
                if m:
                    print(f"[wave] Found ennyAI UUID in link ({sel}): {m.group()}", file=sys.stderr)
                    return m.group()
                # Click and grab URL
                print(f"[wave] Clicking ennyAI selector: {sel}", file=sys.stderr)
                el.click()
                page.wait_for_timeout(2000)
                m = UUID_RE.search(page.url)
                if m:
                    print(f"[wave] Slug from URL after click: {m.group()}", file=sys.stderr)
                    return m.group()

        # 3. Try business switcher to reveal the list
        for switcher_sel in [
            "[data-testid*='business-switcher']",
            "[aria-label*='business' i]",
            "[class*='BusinessSwitcher']",
            "[class*='business-switcher']",
        ]:
            btn = page.query_selector(switcher_sel)
            if btn:
                btn.click()
                page.wait_for_timeout(1000)
                # Re-check for ennyAI
                for sel in enny_selectors:
                    el = page.query_selector(sel)
                    if el:
                        href = el.get_attribute("href") or ""
                        m = UUID_RE.search(href)
                        if m:
                            return m.group()
                        el.click()
                        page.wait_for_timeout(2000)
                        m = UUID_RE.search(page.url)
                        if m:
                            return m.group()
                page.keyboard.press("Escape")
                break

        # 4. Scan ALL links on the page for any UUID slug
        all_links = page.query_selector_all("a[href]")
        slugs_found = []
        for link in all_links:
            href = link.get_attribute("href") or ""
            m = UUID_RE.search(href)
            if m:
                slugs_found.append((m.group(), href))

        if slugs_found:
            print(f"[wave] UUID slugs found on page: {slugs_found[:5]}", file=sys.stderr)
            # Prefer links that go to accounting or dashboard
            for slug, href in slugs_found:
                if any(k in href for k in ("/accounting", "/dashboard")):
                    return slug
            # Fall back to first UUID found
            return slugs_found[0][0]

        # 5. Navigate to root and see where Wave redirects
        print("[wave] No slug found — trying root redirect", file=sys.stderr)
        page.goto(WAVE_NEXT, wait_until="domcontentloaded", timeout=20000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
        m = UUID_RE.search(page.url)
        if m:
            print(f"[wave] Slug from root redirect: {m.group()}", file=sys.stderr)
            return m.group()
        # Also scan links after redirect
        for link in page.query_selector_all("a[href]"):
            href = link.get_attribute("href") or ""
            m = UUID_RE.search(href)
            if m:
                return m.group()

    except Exception as e:
        print(f"[wave] Business slug search failed: {e}", file=sys.stderr)
    return None


def _find_biz_slug(page, current_url: str) -> str | None:
    """Try to extract the business slug from the current page URL or links."""
    # next.waveapps.com/<slug>/... pattern
    EXCLUDED = {"login", "auth", "dashboard", "onboarding", "accounting", "settings"}
    m = re.search(r'next\.waveapps\.com/([^/?]+)/', current_url)
    if m and m.group(1) not in EXCLUDED:
        return m.group(1)

    # Look for nav links with a business slug
    links = page.query_selector_all("a[href*='accounting'], a[href*='transactions']")
    for link in links:
        href = link.get_attribute("href") or ""
        m = re.search(r'/([^/?]+)/accounting', href)
        if m:
            return m.group(1)

    return None


def _dismiss_security_prompts(page):
    """Complete 2FA setup or dismiss security prompts that block accounting access."""
    # Try to complete 2FA fully (using saved secret or new setup)
    secrets = load_secrets()
    if secrets.get("wave_totp_secret"):
        # 2FA already set up — just pass through any prompt
        for sel in ["a:has-text('Skip for now')", "a:has-text('Skip')"]:
            el = page.query_selector(sel)
            if el:
                try:
                    el.click(force=True, timeout=3000)
                except Exception:
                    page.evaluate("el => el.click()", el)
                page.wait_for_timeout(2000)
                return

    # Check for 2FA setup page
    h1 = page.query_selector("h1, h2")
    if h1:
        txt = h1.inner_text().strip().lower()
        if "two-factor" in txt or "authenticator" in txt or "one last step" in txt:
            _setup_2fa_if_needed(page, lambda name: None)
            return

    # Generic skip/dismiss
    for sel in ["a:has-text('Skip for now')", "a:has-text('Skip')"]:
        el = page.query_selector(sel)
        if el:
            print(f"[wave] Dismissing prompt: {sel}", file=sys.stderr)
            try:
                el.click(force=True, timeout=3000)
            except Exception:
                page.evaluate("el => el.click()", el)
            page.wait_for_timeout(2000)
            return


def _try_export(page, start: date, end: date, csv_out: str | None, snap) -> list:
    """Try to use Wave's CSV export feature."""
    from playwright.sync_api import TimeoutError as PWTimeout

    # Wave's export may be behind a menu. Try top-level export buttons first,
    # then look inside menus (⋮ / kebab / overflow menus).
    export_btns = [
        "button:has-text('Export')",
        "a:has-text('Export')",
        "button:has-text('Download CSV')",
        "a:has-text('Download CSV')",
        "[aria-label*='Export' i]",
        "[aria-label*='Download' i]",
        "button:has-text('CSV')",
    ]

    def try_download(btn_sel):
        btn = page.query_selector(btn_sel)
        if not btn or not btn.is_visible():
            return None
        print(f"[wave] Trying export: {btn_sel}", file=sys.stderr)
        try:
            with page.expect_download(timeout=20000) as dl_info:
                btn.click()
            download = dl_info.value
            tmp_path = f"/tmp/wave_tx_raw_{start}_{end}.csv"
            download.save_as(tmp_path)
            print(f"[wave] Downloaded to {tmp_path}", file=sys.stderr)
            snap("export_success")
            if csv_out:
                import shutil
                shutil.copy(tmp_path, csv_out)
            return _parse_csv(tmp_path, start, end)
        except Exception as e:
            print(f"[wave] Export {btn_sel} failed: {e}", file=sys.stderr)
            return None

    # Try top-level buttons
    for sel in export_btns:
        result = try_download(sel)
        if result is not None:
            return result

    # Try opening overflow / action menus that might contain Export
    for menu_sel in [
        "button[aria-haspopup='menu']",
        "button[aria-label*='more' i]",
        "button[aria-label*='action' i]",
        "[class*='kebab'], [class*='overflow'], [class*='more-options']",
    ]:
        menu_btn = page.query_selector(menu_sel)
        if menu_btn and menu_btn.is_visible():
            try:
                menu_btn.click()
                page.wait_for_timeout(800)
                snap("export_menu_opened")
                for sel in export_btns:
                    result = try_download(sel)
                    if result is not None:
                        return result
                # Close the menu
                page.keyboard.press("Escape")
            except Exception:
                pass

    return []


def _click_load_more(page) -> bool:
    """Click the 'Load more' button if present. Returns True if clicked."""
    from playwright.sync_api import TimeoutError as PWTimeout
    selectors = [
        "button:has-text('Load more')",
        "button:has-text('load more')",
        "button:has-text('Show more')",
        "[class*='load-more']",
        "[class*='loadMore']",
        "[data-testid*='load-more']",
    ]
    for sel in selectors:
        btn = page.query_selector(sel)
        if btn and btn.is_visible():
            try:
                btn.scroll_into_view_if_needed()
                btn.click()
                time.sleep(1.5)
                return True
            except Exception:
                pass
    return False


def _earliest_visible_date(page) -> "date | None":
    """Return the earliest transaction date currently visible in the table."""
    date_cells = page.query_selector_all("td.transactions-list-v2__row__date-cell")
    earliest = None
    for cell in date_cells:
        d = _parse_date(cell.inner_text().strip())
        if d and (earliest is None or d < earliest):
            earliest = d
    return earliest


def _scrape_table(page, start: date, end: date) -> list:
    """Scrape transaction rows from Wave's transactions-list-v2 table, clicking Load more until exhausted."""
    rows = []

    # Click Load more until all transactions in the requested range are visible.
    # Stop early if the earliest visible date has already passed before `start`
    # (clicking more would only load transactions outside the range).
    max_loads = 50
    for i in range(max_loads):
        earliest = _earliest_visible_date(page)
        if earliest is not None and earliest < start:
            print(f"[wave] Earliest visible {earliest} < start {start} — stopping load-more", file=sys.stderr)
            break
        clicked = _click_load_more(page)
        if not clicked:
            break
        print(f"[wave] Loaded more (pass {i+1})", file=sys.stderr)

    row_els = page.query_selector_all("table tbody tr")
    print(f"[wave] Scraping {len(row_els)} table rows", file=sys.stderr)

    if not row_els:
        main = page.query_selector("main, [role='main']")
        if main:
            print(f"[wave] Main content:\n{main.inner_text()[:2000]}", file=sys.stderr)
        return []

    for row_el in row_els:
        try:
            # Wave transactions-list-v2 layout: 8 cells
            # [0]=checkbox [1]=matched [2]=date [3]=description [4]=account [5]=category [6]=amount [7]=actions
            date_cell = row_el.query_selector("td.transactions-list-v2__row__date-cell")
            desc_cell = row_el.query_selector("td.transactions-list-v2__row__description-cell")
            acct_cell = row_el.query_selector("td.transactions-list-v2__row__account-cell")
            cat_cell  = row_el.query_selector("td.transactions-list-v2__row__category-cell")
            amt_cell  = row_el.query_selector("td.transactions-list-v2__row__amount-cell")

            if not date_cell or not amt_cell:
                continue

            date_str = date_cell.inner_text().strip()
            tx_date = _parse_date(date_str)
            if not tx_date or tx_date < start or tx_date > end:
                continue

            desc = desc_cell.inner_text().strip() if desc_cell else ""
            account = acct_cell.inner_text().strip() if acct_cell else ""
            category = cat_cell.inner_text().strip() if cat_cell else ""

            # Amount sign: income rows have class --positive on the <strong>
            # expense rows have no --positive class
            amt_str = amt_cell.inner_text().strip()
            amount = _try_parse_amount(amt_str) or 0.0
            is_positive = bool(
                amt_cell.query_selector("strong.transactions-list-v2__row__amount--positive")
            )
            if not is_positive:
                amount = -abs(amount)  # expense = negative

            rows.append({
                "date": str(tx_date),
                "description": desc,
                "account": account,
                "category": category,
                "debit": max(0.0, -amount),
                "credit": max(0.0, amount),
                "amount": amount,
            })
        except Exception as e:
            print(f"[wave] Row parse error: {e}", file=sys.stderr)
            continue

    return rows


def _is_amount(s: str) -> bool:
    return bool(re.match(r'^-?\$?[\d,]+\.?\d*$', s.strip().replace(',', '')))


def _try_parse_amount(s: str) -> float | None:
    s = s.strip().replace(",", "").replace("$", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return None


def _parse_date(s: str) -> date | None:
    for fmt in ("%b %d, %Y", "%B %d, %Y", "%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s.strip(), fmt).date()
        except ValueError:
            pass
    return None


def _parse_csv(csv_path: str, start: date, end: date) -> list:
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            tx_date_str = (
                row.get("Transaction Date") or row.get("Date") or
                row.get("date") or ""
            ).strip()
            tx_date = _parse_date(tx_date_str)
            if not tx_date or tx_date < start or tx_date > end:
                continue

            def amt(key):
                return _parse_amount(row.get(key) or "0")

            debit = amt("Debit (Expense)") or amt("Debit") or amt("debit")
            credit = amt("Credit (Income)") or amt("Credit") or amt("credit")
            desc = (row.get("Description") or row.get("description") or "").strip()
            account = (row.get("Account") or row.get("account") or "").strip()

            rows.append({
                "date": str(tx_date),
                "description": desc,
                "account": account,
                "debit": debit,
                "credit": credit,
                "amount": credit - debit,
            })
    return rows


def _parse_amount(s: str) -> float:
    if not s:
        return 0.0
    s = s.strip().replace(",", "").replace("$", "").replace("(", "-").replace(")", "")
    try:
        return float(s)
    except ValueError:
        return 0.0


def aggregate(transactions: list) -> dict:
    income = sum(t["credit"] for t in transactions)
    expenses = sum(t["debit"] for t in transactions)
    return {
        "income": income,
        "expenses": expenses,
        "net": income - expenses,
        "count": len(transactions),
        "transactions": transactions,
    }


def main():
    parser = argparse.ArgumentParser(description="Scrape Wave transactions via Playwright")
    parser.add_argument("--start", required=True, help="Start date YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="End date YYYY-MM-DD")
    parser.add_argument("--csv-out", help="Save raw CSV to this path")
    parser.add_argument("--screenshots", help="Directory to save debug screenshots")
    parser.add_argument("--reauth", action="store_true", help="Force fresh login")
    parser.add_argument("--json", action="store_true", help="Output JSON summary")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)

    if args.screenshots:
        Path(args.screenshots).mkdir(parents=True, exist_ok=True)

    txns = scrape_transactions(
        start, end,
        csv_out=args.csv_out,
        screenshot_dir=args.screenshots,
        reauth=args.reauth,
    )
    summary = aggregate(txns)

    if args.json:
        print(json.dumps(summary, indent=2))
    else:
        print(f"Period: {start} – {end}")
        print(f"Transactions: {summary['count']}")
        print(f"Income:    ${summary['income']:,.2f}")
        print(f"Expenses:  ${summary['expenses']:,.2f}")
        print(f"Net:       ${summary['net']:,.2f}")
        if txns:
            print("\nSample (first 10):")
            for t in sorted(txns, key=lambda x: x["date"])[:10]:
                sign = "+" if t["amount"] >= 0 else ""
                print(f"  {t['date']}  {t['description'][:40]:<40}  {sign}${t['amount']:,.2f}")


if __name__ == "__main__":
    main()
