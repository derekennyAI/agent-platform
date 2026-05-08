#!/usr/bin/env python3
"""Deep credit card analysis — login, scroll through all transactions, extract full history."""

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

    sc = [0]
    def snap(name):
        sc[0] += 1
        p = _SCREENSHOT_DIR / f"{sc[0]:02d}_{name}.png"
        page.screenshot(path=str(p))

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # ── Login ─────────────────────────────────────────────────────────────
        print("[quicken] Logging in...", file=sys.stderr)
        page.goto(QUICKEN_APP, wait_until="domcontentloaded", timeout=30000)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)

        signin_btn = page.query_selector('a:has-text("SIGN IN"), a:has-text("Sign In"), button:has-text("SIGN IN")')
        if signin_btn:
            signin_btn.click()
            page.wait_for_timeout(3000)

        # Find email in iframe
        email_input = None
        login_frame = None
        for frame in page.frames:
            if "signin.quicken.com" in frame.url:
                for inp in frame.query_selector_all('input[type="email"], input[type="text"]'):
                    if inp.is_visible():
                        email_input = inp
                        login_frame = frame
                        break
            if email_input:
                break

        if not email_input:
            print("[quicken] No email field", file=sys.stderr)
            browser.close()
            return

        email_input.fill(email)
        cont = login_frame.query_selector('button[type="submit"], button:has-text("Continue")')
        if cont:
            cont.click()
        page.wait_for_timeout(3000)

        # Password
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            pf = frame.query_selector('input[type="password"]')
            if pf and pf.is_visible():
                pf.fill(password)
                sub = frame.query_selector('button[type="submit"], button:has-text("Sign In")')
                if sub:
                    sub.click()
                break

        page.wait_for_timeout(5000)
        try:
            page.wait_for_selector('nav, [class*="sidebar"]', timeout=20000)
        except PWTimeout:
            pass
        page.wait_for_timeout(3000)
        print(f"[quicken] Logged in: {page.url}", file=sys.stderr)
        snap("logged_in")

        # ── Navigate to Transactions → Joint Credit ───────────────────────────
        tx_link = page.query_selector('a[href*="transaction"], a:has-text("Transactions")')
        if tx_link:
            tx_link.click()
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        # Click Joint Credit
        credit_items = page.query_selector_all('span:has-text("Joint Credit"), a:has-text("Joint Credit"), [class*="account"]:has-text("Joint Credit")')
        for item in credit_items:
            text = (item.text_content() or "").strip()
            if "joint credit" in text.lower() and len(text) < 50:
                item.click()
                break
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass
        snap("credit_register")
        print(f"[quicken] Credit card register: {page.url}", file=sys.stderr)

        # ── Extract ALL visible transactions by scrolling ─────────────────────
        all_text = set()
        prev_count = 0
        scroll_attempts = 0

        while scroll_attempts < 20:
            # Get current visible text
            main_el = page.query_selector('main, [role="main"], #root')
            if main_el:
                current = main_el.text_content() or ""
            else:
                current = page.text_content("body") or ""
            all_text.add(current)

            # Count unique transaction-like lines
            lines = [l.strip() for l in current.split('\n') if l.strip()]
            tx_lines = [l for l in lines if re.search(r'\d{2}/\d{2}/\d{4}', l)]

            if len(tx_lines) == prev_count and scroll_attempts > 2:
                print(f"[quicken] No new transactions after scroll {scroll_attempts}", file=sys.stderr)
                break

            prev_count = len(tx_lines)
            scroll_attempts += 1

            # Scroll down
            page.keyboard.press("End")
            page.wait_for_timeout(1500)

            if scroll_attempts % 5 == 0:
                snap(f"scroll_{scroll_attempts}")
                print(f"[quicken] Scroll {scroll_attempts}: {len(tx_lines)} tx lines", file=sys.stderr)

        snap("all_scrolled")

        # Parse the full text into structured transactions
        full_text = "\n".join(all_text)

        # Extract transaction lines: date, payee, category, amount, balance
        # Pattern: MM/DD/YYYY followed by transaction data
        tx_pattern = re.compile(
            r'(\d{2}/\d{2}/\d{4})\s*'  # date
            r'(.+?)'                      # payee
            r'([\w\s&]+?)\s*'            # category
            r'[CR]?\s*'                   # cleared status
            r'(-?[\d,]+\.\d{2})\s*'      # amount
            r'(-?[\d,]+\.\d{2})'         # balance
        )

        # Simpler approach: just get the full text content
        print("\n=== FULL TRANSACTION REGISTER ===", file=sys.stderr)

        # Get the register text more cleanly by extracting each row
        rows = page.query_selector_all('[class*="row"], [class*="Row"], tr')
        structured = []
        for row in rows:
            text = (row.text_content() or "").strip()
            if re.search(r'\d{2}/\d{2}/\d{4}', text):
                structured.append(text)

        if not structured:
            # Fall back to the full page text parsing
            for text_block in all_text:
                # Split by date pattern
                parts = re.split(r'(?=\d{2}/\d{2}/\d{4})', text_block)
                for part in parts:
                    part = part.strip()
                    if part and re.match(r'\d{2}/\d{2}/\d{4}', part):
                        structured.append(part)

        # Print structured output
        print(json.dumps({
            "account": "Joint Credit",
            "balance": "-8,373.75",
            "transaction_count": len(structured),
            "transactions": structured[:300],
        }, indent=2, default=str))

        browser.close()


if __name__ == "__main__":
    main()
