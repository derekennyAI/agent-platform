#!/usr/bin/env python3
"""One-shot Quicken credit card analysis — logs in, grabs CC transactions, analyzes spending."""

import json
import os
import re
import sys
import time
from pathlib import Path

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

_SCREENSHOT_DIR = Path("/tmp/quicken_screenshots")
QUICKEN_APP = "https://app.quicken.com"
PLAYWRIGHT_BROWSERS = str(Path.home() / ".cache/ms-playwright")


def main():
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    secrets = load_secrets()
    email = secrets["quicken_email"]
    password = secrets["quicken_password"]
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    snap_counter = [0]
    def snap(name):
        snap_counter[0] += 1
        p = _SCREENSHOT_DIR / f"{snap_counter[0]:02d}_{name}.png"
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

        # ── Login ─────────────────────────────────────────────────────────────
        print("[quicken] Loading app...", file=sys.stderr)
        page.goto(QUICKEN_APP, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
        snap("landing")

        # Click Sign In
        signin_btn = page.query_selector(
            'a:has-text("SIGN IN"), a:has-text("Sign In"), '
            'button:has-text("SIGN IN"), button:has-text("Sign In")'
        )
        if signin_btn:
            signin_btn.click()
            page.wait_for_timeout(3000)

        # Find email input in iframe
        email_sel = 'input[type="email"], input[type="text"]'
        login_frame = None
        email_input = None

        for frame in page.frames:
            if frame == page.main_frame:
                continue
            if "signin.quicken.com" in frame.url:
                try:
                    inputs = frame.query_selector_all(email_sel)
                    for inp in inputs:
                        if inp.is_visible():
                            email_input = inp
                            login_frame = frame
                            break
                except Exception:
                    pass
            if email_input:
                break

        if not email_input:
            print("[quicken] Cannot find email field", file=sys.stderr)
            snap("no_email")
            browser.close()
            return

        email_input.fill(email)
        snap("email_filled")

        # Click Continue
        continue_btn = login_frame.query_selector(
            'button[type="submit"], button:has-text("Continue")'
        )
        if continue_btn:
            continue_btn.click()
        page.wait_for_timeout(3000)
        snap("after_email")

        # Fill password
        pw_sel = 'input[type="password"]'
        pw_field = None
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                pf = frame.query_selector(pw_sel)
                if pf and pf.is_visible():
                    pw_field = pf
                    login_frame = frame
                    break
            except Exception:
                pass

        if not pw_field:
            pw_field = page.query_selector(pw_sel)

        if pw_field:
            pw_field.fill(password)
            snap("password_filled")
            submit = login_frame.query_selector(
                'button[type="submit"], button:has-text("Sign In")'
            ) if login_frame else page.query_selector('button[type="submit"]')
            if submit:
                submit.click()
            else:
                page.keyboard.press("Enter")
        else:
            print("[quicken] No password field found", file=sys.stderr)
            snap("no_password")
            browser.close()
            return

        # Wait for login to complete
        try:
            page.wait_for_timeout(5000)
            # Wait for SPA to settle — look for dashboard content
            page.wait_for_selector('nav, [class*="sidebar"], [class*="navigation"]', timeout=20000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
        snap("logged_in")
        print(f"[quicken] Logged in at: {page.url}", file=sys.stderr)

        # ── Intercept API calls to get structured data ────────────────────────
        # The SPA makes API calls — let's intercept them
        api_responses = []

        def capture_response(response):
            url = response.url
            if any(kw in url for kw in ["api", "transaction", "account", "register"]):
                try:
                    body = response.json()
                    api_responses.append({"url": url, "data": body})
                except Exception:
                    pass

        page.on("response", capture_response)

        # ── Navigate to Transactions ──────────────────────────────────────────
        print("[quicken] Navigating to Transactions...", file=sys.stderr)

        # Click Transactions in the sidebar nav
        tx_link = page.query_selector(
            'a[href*="transaction"], a:has-text("Transactions"), '
            'nav a:has-text("Transactions")'
        )
        if tx_link:
            tx_link.click()
        else:
            # Try direct navigation
            page.goto(f"{QUICKEN_APP}/transactions", wait_until="domcontentloaded", timeout=30000)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
        snap("transactions_page")
        print(f"[quicken] Transactions URL: {page.url}", file=sys.stderr)

        # ── Try to find account selector / filter ─────────────────────────────
        # Look for a dropdown or sidebar that lists accounts
        print("[quicken] Looking for account filter...", file=sys.stderr)

        # Check for clickable account names in sidebar
        account_items = page.query_selector_all(
            '[class*="account"], [class*="Account"], '
            'li:has-text("Credit"), li:has-text("Joint Credit"), '
            'a:has-text("Credit"), a:has-text("Joint Credit"), '
            'span:has-text("Joint Credit")'
        )
        print(f"[quicken] Found {len(account_items)} account-related elements", file=sys.stderr)

        clicked_credit = False
        for item in account_items:
            text = (item.text_content() or "").strip()
            if "credit" in text.lower() and len(text) < 100:
                print(f"[quicken] Clicking account: {text[:50]}", file=sys.stderr)
                try:
                    item.click()
                    page.wait_for_timeout(3000)
                    try:
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except PWTimeout:
                        pass
                    snap("credit_account")
                    clicked_credit = True
                    break
                except Exception as e:
                    print(f"[quicken] Click failed: {e}", file=sys.stderr)

        if not clicked_credit:
            # Try URL-based navigation
            page.goto(f"{QUICKEN_APP}/transactions?displayNode=credit", wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(5000)
            snap("credit_direct_url")

        # ── Extract transaction data ──────────────────────────────────────────
        print("[quicken] Extracting transaction data...", file=sys.stderr)
        snap("before_extract")

        # Try CSV download first
        download_btn = page.query_selector(
            'button[aria-label*="download"], button[aria-label*="export"], '
            'button:has-text("Download"), button:has-text("Export"), '
            'a[aria-label*="download"], [class*="download"], [class*="export"]'
        )

        transactions_data = []

        if download_btn:
            print("[quicken] Found download button — clicking", file=sys.stderr)
            try:
                with page.expect_download(timeout=15000) as dl_info:
                    download_btn.click()
                dl = dl_info.value
                csv_path = "/tmp/quicken_credit_card.csv"
                dl.save_as(csv_path)
                print(f"[quicken] CSV downloaded: {csv_path}", file=sys.stderr)
                import csv
                with open(csv_path, 'r') as f:
                    reader = csv.DictReader(f)
                    transactions_data = list(reader)
            except Exception as e:
                print(f"[quicken] Download failed: {e}", file=sys.stderr)

        # Scrape visible transaction table
        if not transactions_data:
            print("[quicken] Scraping transaction table from DOM...", file=sys.stderr)

            # Get the main content area text
            main_area = page.query_selector('main, [role="main"], #root')
            if main_area:
                page_text = main_area.text_content() or ""
            else:
                page_text = page.text_content("body") or ""

            # Look for table rows
            rows = page.query_selector_all(
                'tr, [class*="TransactionRow"], [class*="transaction-row"], '
                '[class*="register-row"], [role="row"]'
            )
            print(f"[quicken] Found {len(rows)} table rows", file=sys.stderr)

            for row in rows:
                text = (row.text_content() or "").strip()
                if text and len(text) > 5:
                    transactions_data.append(text)

            # If still nothing, grab the full page text
            if not transactions_data:
                transactions_data.append(page_text)

        # ── Also check API responses we captured ──────────────────────────────
        print(f"[quicken] Captured {len(api_responses)} API responses", file=sys.stderr)
        for resp in api_responses:
            print(f"  API: {resp['url'][:100]}", file=sys.stderr)

        # ── Get a full-page screenshot for context ────────────────────────────
        snap("final_state")

        # Also scroll down to see more transactions
        for i in range(3):
            page.keyboard.press("End")
            page.wait_for_timeout(1000)
        snap("scrolled_down")

        # Print results
        output = {
            "url": page.url,
            "transactions_count": len(transactions_data),
            "transactions": transactions_data[:200],  # Cap output
            "api_responses": [{"url": r["url"][:200]} for r in api_responses],
        }
        print(json.dumps(output, indent=2, default=str))

        browser.close()


if __name__ == "__main__":
    main()
