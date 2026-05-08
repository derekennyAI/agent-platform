#!/usr/bin/env python3
"""Scrape transactions from Quicken Classic web app using Playwright.

Logs in as YOUR_EMAIL, handles MFA via Gmail/SMS,
stores session cookies to avoid re-verification, then exports
credit card and account transaction data.

Usage:
    python3 quicken_transactions.py --accounts          # list all accounts
    python3 quicken_transactions.py --account "Chase"   # transactions for specific account
    python3 quicken_transactions.py --all               # all accounts' transactions
    python3 quicken_transactions.py --reauth            # force fresh login
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

_SESSION_FILE = Path("/Users/YOUR_MAC_USERNAME/.claude/quicken_session.json")
_SCREENSHOT_DIR = Path("/tmp/quicken_screenshots")
QUICKEN_APP = "https://app.quicken.com"
QUICKEN_SIGNIN = "https://signin.quicken.com"
PLAYWRIGHT_BROWSERS = str(Path.home() / ".cache/ms-playwright")
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


# ── Gmail helpers (reuse pattern from wave_transactions.py) ───────────────────

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


def _extract_gmail_body(msg: dict) -> str:
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
    return " ".join(get_parts(payload))


def get_quicken_mfa_code(after_ts: int, max_wait: int = 90) -> str | None:
    """Poll Gmail for a Quicken MFA verification code."""
    token_data, token_path, creds = _gmail_token()
    access_token = token_data.get("access_token", "")

    deadline = time.time() + max_wait
    while time.time() < deadline:
        result = _gmail_get(
            "/users/me/messages?q=from:quicken+subject:code+OR+subject:verify+OR+subject:security&maxResults=5",
            access_token
        )
        if result is None:
            access_token = _refresh_gmail(token_data, token_path, creds)
            result = _gmail_get(
                "/users/me/messages?q=from:quicken+subject:code+OR+subject:verify+OR+subject:security&maxResults=5",
                access_token
            )

        messages = (result or {}).get("messages", [])
        for msg_ref in messages:
            msg = _gmail_get(f"/users/me/messages/{msg_ref['id']}", access_token)
            if not msg:
                continue
            internal_date = int(msg.get("internalDate", 0)) // 1000
            if internal_date < after_ts:
                continue
            body = _extract_gmail_body(msg)
            codes = re.findall(r'\b(\d{4,8})\b', body)
            if codes:
                print(f"[quicken] Found MFA code in email", file=sys.stderr)
                return codes[0]

        print(f"[quicken] Waiting for MFA email... ({int(deadline - time.time())}s left)", file=sys.stderr)
        time.sleep(5)

    return None


# ── Session management ────────────────────────────────────────────────────────

def save_session(cookies: list):
    _SESSION_FILE.write_text(json.dumps({"cookies": cookies, "saved": time.time()}, indent=2))
    print(f"[quicken] Session saved ({len(cookies)} cookies)", file=sys.stderr)


def load_session() -> list | None:
    if not _SESSION_FILE.exists():
        return None
    try:
        data = json.loads(_SESSION_FILE.read_text())
        if time.time() - data.get("saved", 0) > 7 * 86400:
            print("[quicken] Stored session expired", file=sys.stderr)
            return None
        return data.get("cookies", [])
    except Exception:
        return None


# ── Playwright scraper ────────────────────────────────────────────────────────

def scrape_quicken(mode="accounts", account_filter=None, reauth=False, screenshot=True):
    """
    mode: 'accounts' = list accounts, 'transactions' = get transactions
    account_filter: substring match on account name (None = all)
    """
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS

    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    secrets = load_secrets()
    email = secrets["quicken_email"]
    password = secrets["quicken_password"]

    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    def snap(name):
        if screenshot:
            p = _SCREENSHOT_DIR / f"{name}.png"
            page.screenshot(path=str(p))
            print(f"[screenshot] {p}", file=sys.stderr)

    results = {"accounts": [], "transactions": []}

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

        # --- Try stored session ---
        logged_in = False
        if not reauth:
            cookies = load_session()
            if cookies:
                print("[quicken] Loading stored session cookies...", file=sys.stderr)
                ctx.add_cookies(cookies)
                page.goto(f"{QUICKEN_APP}", wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=20000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(3000)
                snap("01_session_test")
                if "signin" not in page.url and "login" not in page.url and "auth" not in page.url:
                    print(f"[quicken] Session valid: {page.url}", file=sys.stderr)
                    logged_in = True
                else:
                    print("[quicken] Stored session expired, re-logging in", file=sys.stderr)

        # --- Full login flow ---
        if not logged_in:
            login_ts = int(time.time())
            print("[quicken] Logging in...", file=sys.stderr)
            page.goto(f"{QUICKEN_APP}", wait_until="domcontentloaded", timeout=30000)
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except PWTimeout:
                pass
            page.wait_for_timeout(3000)
            snap("02_login_page")

            print(f"[quicken] Current URL: {page.url}", file=sys.stderr)

            # Quicken landing page has a "SIGN IN" button that opens a modal
            # First click the sign-in button
            signin_btn = page.query_selector(
                'a[href*="signin"], a[href*="login"], '
                'button:has-text("Sign In"), button:has-text("SIGN IN"), '
                'a:has-text("Sign In"), a:has-text("SIGN IN")'
            )
            if signin_btn:
                print("[quicken] Clicking Sign In button...", file=sys.stderr)
                signin_btn.click()
                page.wait_for_timeout(3000)
                snap("02b_after_signin_click")

            # The sign-in modal may load in an iframe from signin.quicken.com
            # Check for iframes first
            email_sel = 'input[type="email"], input[type="text"], input[name="email"], input[name="username"]'
            login_frame = None

            # Check main page first
            email_input = None
            for inp in page.query_selector_all(email_sel):
                if inp.is_visible():
                    email_input = inp
                    break

            if not email_input:
                # Check iframes
                frames = page.frames
                print(f"[quicken] Found {len(frames)} frames: {[f.url for f in frames]}", file=sys.stderr)
                for frame in frames:
                    if frame == page.main_frame:
                        continue
                    try:
                        inputs = frame.query_selector_all(email_sel)
                        for inp in inputs:
                            if inp.is_visible():
                                email_input = inp
                                login_frame = frame
                                print(f"[quicken] Found email input in iframe: {frame.url}", file=sys.stderr)
                                break
                    except Exception as e:
                        print(f"[quicken] Frame error: {e}", file=sys.stderr)
                    if email_input:
                        break

            if not email_input:
                # Last try: go directly to signin.quicken.com
                print("[quicken] No email field in frames — trying signin.quicken.com directly", file=sys.stderr)
                page.goto("https://signin.quicken.com", wait_until="domcontentloaded", timeout=30000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(3000)
                snap("02d_signin_direct")
                print(f"[quicken] Direct signin URL: {page.url}", file=sys.stderr)
                try:
                    page.wait_for_selector(email_sel, timeout=10000)
                    for inp in page.query_selector_all(email_sel):
                        if inp.is_visible():
                            email_input = inp
                            break
                except PWTimeout:
                    pass

            if not email_input:
                print("[quicken] Cannot find email field anywhere", file=sys.stderr)
                snap("02e_stuck")
                # Dump all inputs for debugging
                all_inputs = page.query_selector_all("input")
                for i, inp in enumerate(all_inputs):
                    itype = inp.get_attribute("type") or "?"
                    iname = inp.get_attribute("name") or "?"
                    iid = inp.get_attribute("id") or "?"
                    vis = inp.is_visible()
                    print(f"  input[{i}]: type={itype} name={iname} id={iid} visible={vis}", file=sys.stderr)
                browser.close()
                return results

            # Use the frame context if input was in an iframe
            form_ctx = login_frame if login_frame else page

            email_input.fill(email)

            snap("03_email_filled")

            # Click Continue/Next — Quicken uses a two-step flow (email then password)
            continue_btn = form_ctx.query_selector(
                'button[type="submit"], button:has-text("Continue"), '
                'button:has-text("Next"), button:has-text("Sign In")'
            )
            if continue_btn:
                continue_btn.click()
                page.wait_for_timeout(3000)
                snap("03b_after_email_submit")

            # Now look for password field — check both main page and iframe
            pw_sel = 'input[type="password"], input[name="password"]'
            pw_field = None

            # After email submit, page might have navigated — recheck frames
            for ctx_check in [page] + [f for f in page.frames if f != page.main_frame]:
                try:
                    pf = ctx_check.query_selector(pw_sel)
                    if pf and pf.is_visible():
                        pw_field = pf
                        form_ctx = ctx_check
                        break
                except Exception:
                    pass

            if not pw_field:
                try:
                    page.wait_for_selector(pw_sel, timeout=10000)
                    pw_field = page.query_selector(pw_sel)
                except PWTimeout:
                    print("[quicken] No password field appeared", file=sys.stderr)
                    snap("03c_no_password")

            if pw_field and pw_field.is_visible():
                pw_field.fill(password)
                snap("04_password_filled")

                submit = form_ctx.query_selector(
                    'button[type="submit"], button:has-text("Sign In"), '
                    'button:has-text("Log In"), button:has-text("Continue")'
                )
                if submit:
                    submit.click()
                else:
                    page.keyboard.press("Enter")
            else:
                print("[quicken] Password field not visible — trying Enter", file=sys.stderr)
                page.keyboard.press("Enter")

            try:
                page.wait_for_url(lambda url: "signin" not in url and "login" not in url, timeout=20000)
            except PWTimeout:
                pass

            page.wait_for_timeout(4000)
            snap("05_after_login")
            print(f"[quicken] After login: {page.url}", file=sys.stderr)

            # Handle MFA if needed
            page_text = page.text_content("body") or ""
            if any(kw in page.url.lower() for kw in ["verify", "mfa", "challenge", "code"]) or \
               any(kw in page_text.lower() for kw in ["verification code", "security code", "enter code", "enter the code"]):
                print("[quicken] MFA challenge detected", file=sys.stderr)
                snap("06_mfa_page")

                # Look for MFA input
                mfa_input = page.query_selector(
                    'input[autocomplete="one-time-code"], input[type="tel"], '
                    'input[type="number"], input[type="text"][maxlength="6"], '
                    'input[type="text"][maxlength="8"]'
                )

                if mfa_input:
                    # Try email MFA first
                    # Check if there's an "email" option to select
                    email_option = page.query_selector('button:has-text("Email"), label:has-text("Email"), a:has-text("Email")')
                    if email_option:
                        email_option.click()
                        page.wait_for_timeout(2000)

                    mfa_code = get_quicken_mfa_code(after_ts=login_ts, max_wait=90)
                    if not mfa_code:
                        print("[quicken] No MFA code received — cannot proceed", file=sys.stderr)
                        snap("06b_mfa_timeout")
                        browser.close()
                        return results

                    mfa_input.fill(mfa_code)
                    snap("07_mfa_filled")
                    submit = page.query_selector('button[type="submit"], button:has-text("Verify"), button:has-text("Submit"), button:has-text("Continue")')
                    if submit:
                        submit.click()
                    else:
                        page.keyboard.press("Enter")

                    try:
                        page.wait_for_url(lambda url: "verify" not in url and "mfa" not in url and "challenge" not in url, timeout=15000)
                    except PWTimeout:
                        pass
                    page.wait_for_timeout(3000)
                    snap("08_after_mfa")

            # Save session
            save_session(ctx.cookies())
            logged_in = True
            print(f"[quicken] Logged in at: {page.url}", file=sys.stderr)

        if not logged_in:
            print("[quicken] Failed to log in", file=sys.stderr)
            browser.close()
            return results

        # ── Navigate and extract data ─────────────────────────────────────────

        snap("10_logged_in")
        page.wait_for_timeout(2000)

        # Get the dashboard/main page content
        print(f"[quicken] Dashboard URL: {page.url}", file=sys.stderr)
        snap("11_dashboard")

        if mode == "accounts" or mode == "transactions":
            # Navigate to accounts
            print("[quicken] Looking for accounts section...", file=sys.stderr)

            # Try clicking Accounts in nav
            accounts_link = page.query_selector(
                'a[href*="account"], nav a:has-text("Accounts"), '
                'a:has-text("Accounts"), button:has-text("Accounts")'
            )
            if accounts_link:
                accounts_link.click()
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except PWTimeout:
                    pass
            snap("12_accounts_page")
            print(f"[quicken] Accounts URL: {page.url}", file=sys.stderr)

            # Extract account information from the page
            page_text = page.text_content("body") or ""

            # Try to get structured account data
            # Quicken web uses React — try to extract from the rendered DOM
            account_elements = page.query_selector_all(
                '[class*="account"], [class*="Account"], '
                '[data-testid*="account"], li[class*="row"], tr[class*="row"]'
            )

            if account_elements:
                for el in account_elements:
                    text = el.text_content() or ""
                    if text.strip():
                        results["accounts"].append(text.strip())
            else:
                # Fallback: grab page text for analysis
                results["accounts"].append(page_text)

            snap("13_accounts_data")

            # If we want transactions, look for credit card accounts and download CSVs
            if mode == "transactions":
                # Try to find and click on credit card / specific account
                # First, let's try the direct CSV download approach
                print("[quicken] Looking for transaction download options...", file=sys.stderr)

                # Look for clickable account links
                account_links = page.query_selector_all('a[href*="account"], a[href*="register"], a[href*="transaction"]')
                print(f"[quicken] Found {len(account_links)} account links", file=sys.stderr)

                for link in account_links:
                    link_text = (link.text_content() or "").strip()
                    link_href = link.get_attribute("href") or ""
                    if link_text:
                        print(f"[quicken] Account link: {link_text} -> {link_href}", file=sys.stderr)

                    # Filter to requested account
                    if account_filter and account_filter.lower() not in link_text.lower():
                        continue

                    if link_text and ("credit" in link_text.lower() or "card" in link_text.lower() or
                                      "chase" in link_text.lower() or account_filter):
                        print(f"[quicken] Opening account: {link_text}", file=sys.stderr)
                        link.click()
                        page.wait_for_timeout(3000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=10000)
                        except PWTimeout:
                            pass
                        snap(f"14_account_{link_text[:20]}")

                        # Look for download/export button
                        download_btn = page.query_selector(
                            'button:has-text("Download"), button:has-text("Export"), '
                            'button:has-text("CSV"), a:has-text("Download"), '
                            '[aria-label*="download"], [aria-label*="export"]'
                        )
                        if download_btn:
                            print(f"[quicken] Found download button", file=sys.stderr)
                            with page.expect_download(timeout=15000) as download_info:
                                download_btn.click()
                            download = download_info.value
                            csv_path = f"/tmp/quicken_{link_text[:30].replace(' ', '_')}.csv"
                            download.save_as(csv_path)
                            print(f"[quicken] Downloaded CSV: {csv_path}", file=sys.stderr)

                            # Parse the CSV
                            with open(csv_path, 'r') as f:
                                reader = csv.DictReader(f)
                                for row in reader:
                                    results["transactions"].append(row)
                        else:
                            # Scrape transaction table from DOM
                            print("[quicken] No download button — scraping DOM", file=sys.stderr)
                            tx_rows = page.query_selector_all(
                                'tr[class*="transaction"], tr[class*="row"], '
                                '[class*="TransactionRow"], [class*="transaction-row"]'
                            )
                            for row in tx_rows:
                                results["transactions"].append(row.text_content().strip())

                            # Also grab any visible table
                            tables = page.query_selector_all('table')
                            for table in tables:
                                results["transactions"].append(table.text_content().strip())

                            if not results["transactions"]:
                                # Last resort: grab all visible text from the register
                                register = page.query_selector('[class*="register"], [class*="Register"], main, [role="main"]')
                                if register:
                                    results["transactions"].append(register.text_content().strip())

                        snap(f"15_transactions_{link_text[:20]}")

                        # Go back to accounts list
                        page.go_back()
                        page.wait_for_timeout(2000)

                # If no specific account links found, grab whatever's on the page
                if not results["transactions"]:
                    print("[quicken] No specific account links found — grabbing page content", file=sys.stderr)
                    main_content = page.query_selector('main, [role="main"], #app, #root, .app')
                    if main_content:
                        results["transactions"].append(main_content.text_content().strip())
                    else:
                        results["transactions"].append(page_text)

        # Save final session state
        save_session(ctx.cookies())
        browser.close()

    return results


def main():
    parser = argparse.ArgumentParser(description="Quicken Classic web scraper")
    parser.add_argument("--accounts", action="store_true", help="List all accounts")
    parser.add_argument("--account", type=str, help="Get transactions for specific account (name substring)")
    parser.add_argument("--all", action="store_true", help="Get all transactions")
    parser.add_argument("--reauth", action="store_true", help="Force fresh login")
    parser.add_argument("--no-screenshot", action="store_true", help="Disable screenshots")

    args = parser.parse_args()

    if args.accounts:
        result = scrape_quicken(mode="accounts", reauth=args.reauth, screenshot=not args.no_screenshot)
        print(json.dumps(result, indent=2, default=str))
    elif args.account:
        result = scrape_quicken(mode="transactions", account_filter=args.account,
                                reauth=args.reauth, screenshot=not args.no_screenshot)
        print(json.dumps(result, indent=2, default=str))
    elif args.all:
        result = scrape_quicken(mode="transactions", reauth=args.reauth,
                                screenshot=not args.no_screenshot)
        print(json.dumps(result, indent=2, default=str))
    else:
        # Default: list accounts
        result = scrape_quicken(mode="accounts", reauth=args.reauth, screenshot=not args.no_screenshot)
        print(json.dumps(result, indent=2, default=str))


if __name__ == "__main__":
    main()
