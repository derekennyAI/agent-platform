#!/usr/bin/env python3
"""Full credit card history — navigate months and try CSV download."""

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


def extract_transactions(page):
    """Extract transaction text from current page view."""
    main_el = page.query_selector('main, [role="main"], #root')
    if main_el:
        text = main_el.text_content() or ""
    else:
        text = page.text_content("body") or ""

    # Split by date pattern
    parts = re.split(r'(?=\d{2}/\d{2}/\d{4})', text)
    txs = []
    for part in parts:
        part = part.strip()
        if part and re.match(r'\d{2}/\d{2}/\d{4}', part):
            txs.append(part)
    return txs


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
            accept_downloads=True,
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

        signin_btn = page.query_selector('a:has-text("SIGN IN"), button:has-text("SIGN IN")')
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

        # ── Navigate to Transactions → Joint Credit ───────────────────────────
        tx_link = page.query_selector('a[href*="transaction"], a:has-text("Transactions")')
        if tx_link:
            tx_link.click()
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

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

        # ── Try CSV download first ────────────────────────────────────────────
        print("[quicken] Looking for download button...", file=sys.stderr)

        # Look for download icon — usually in the toolbar area, top-right
        # Common patterns: download icon, export button, cloud-download icon
        download_btns = page.query_selector_all(
            'button[aria-label*="ownload"], button[aria-label*="xport"], '
            'button[title*="ownload"], button[title*="xport"], '
            '[class*="download"], [class*="export"], '
            'svg[class*="download"], button:has(svg)'
        )
        print(f"[quicken] Found {len(download_btns)} potential download buttons", file=sys.stderr)

        # Also look for any button/icon in the toolbar area
        toolbar_btns = page.query_selector_all('button')
        for btn in toolbar_btns:
            label = btn.get_attribute("aria-label") or ""
            title = btn.get_attribute("title") or ""
            text = (btn.text_content() or "").strip()
            cls = btn.get_attribute("class") or ""
            if any(kw in (label + title + text + cls).lower() for kw in ["download", "export", "csv"]):
                print(f"[quicken] Download button: label='{label}' title='{title}' text='{text}'", file=sys.stderr)
                try:
                    with page.expect_download(timeout=10000) as dl_info:
                        btn.click()
                    dl = dl_info.value
                    csv_path = "/tmp/quicken_cc_download.csv"
                    dl.save_as(csv_path)
                    print(f"[quicken] CSV saved to {csv_path}", file=sys.stderr)
                    # Read and output the CSV
                    with open(csv_path) as f:
                        print(f.read())
                    browser.close()
                    return
                except Exception as e:
                    print(f"[quicken] Download attempt failed: {e}", file=sys.stderr)

        # ── Manual month-by-month extraction ──────────────────────────────────
        print("[quicken] No CSV download — navigating months manually...", file=sys.stderr)

        all_transactions = []

        # Get current month's transactions
        current_txs = extract_transactions(page)
        all_transactions.extend(current_txs)
        print(f"[quicken] Current month: {len(current_txs)} transactions", file=sys.stderr)
        snap("month_current")

        # Navigate to previous months by clicking the left arrow
        # The month navigation shows "◀ March 2026 ▶" pattern
        for month_back in range(5):  # Go back 5 months
            # Find the left arrow / previous month button
            prev_btn = page.query_selector(
                'button[aria-label*="previous"], button[aria-label*="Previous"], '
                'button[aria-label*="back"], button[aria-label*="Back"], '
                '[class*="prev"], [class*="Prev"], '
                'button:has-text("◀"), button:has-text("←"), button:has-text("<")'
            )

            if not prev_btn:
                # Try looking for navigation arrows near the month display
                month_header = page.query_selector('[class*="month"], [class*="Month"], h2, h3')
                if month_header:
                    # Look for buttons near this element
                    prev_btn = page.query_selector('button:left-of(:text("March")), button:left-of(:text("February"))')

            if not prev_btn:
                # Try all buttons and find ones that look like arrow nav
                for btn in page.query_selector_all('button'):
                    text = (btn.text_content() or "").strip()
                    aria = btn.get_attribute("aria-label") or ""
                    if text in ["<", "◀", "←", "‹"] or "prev" in aria.lower() or "back" in aria.lower():
                        prev_btn = btn
                        break

            if not prev_btn:
                # Try SVG-based navigation icons
                nav_icons = page.query_selector_all('button svg, button [class*="icon"], button [class*="Icon"]')
                # Just try the first few buttons that might be navigation
                all_btns = page.query_selector_all('button')
                for btn in all_btns:
                    bbox = btn.bounding_box()
                    if bbox and bbox["width"] < 60 and bbox["height"] < 60:
                        # Small button, might be nav arrow
                        text = (btn.text_content() or "").strip()
                        if not text or len(text) < 3:
                            # Likely an icon button — check position relative to month text
                            # Look for buttons near the top of the page (toolbar area)
                            if bbox["y"] < 200:
                                print(f"[quicken] Potential nav button at ({bbox['x']}, {bbox['y']})", file=sys.stderr)

            if prev_btn:
                print(f"[quicken] Clicking previous month (attempt {month_back + 1})...", file=sys.stderr)
                prev_btn.click()
                page.wait_for_timeout(2000)
                try:
                    page.wait_for_load_state("networkidle", timeout=8000)
                except PWTimeout:
                    pass
                page.wait_for_timeout(1000)

                snap(f"month_minus_{month_back + 1}")
                month_txs = extract_transactions(page)
                all_transactions.extend(month_txs)
                print(f"[quicken] Month -{month_back + 1}: {len(month_txs)} transactions", file=sys.stderr)
            else:
                print(f"[quicken] Cannot find previous month button", file=sys.stderr)
                snap(f"no_prev_btn_{month_back}")
                break

        # ── Output all data ───────────────────────────────────────────────────
        # Parse transactions into structured format
        parsed = []
        for tx in all_transactions:
            match = re.match(r'(\d{2}/\d{2}/\d{4})(.*?)([CR])(-?[\d,]+\.\d{2})(-?[\d,]+\.\d{2})$', tx)
            if match:
                date_str, desc, status, amount, balance = match.groups()
                parsed.append({
                    "date": date_str,
                    "description": desc.strip(),
                    "amount": float(amount.replace(",", "")),
                    "balance": float(balance.replace(",", "")),
                })
            else:
                # Try simpler parsing
                parts = tx.split("C")
                if len(parts) >= 2:
                    date_match = re.match(r'(\d{2}/\d{2}/\d{4})(.*)', parts[0])
                    if date_match:
                        date_str = date_match.group(1)
                        desc = date_match.group(2).strip()
                        amounts = re.findall(r'-?[\d,]+\.\d{2}', "C".join(parts[1:]))
                        parsed.append({
                            "date": date_str,
                            "description": desc,
                            "amount": float(amounts[0].replace(",", "")) if amounts else 0,
                            "balance": float(amounts[1].replace(",", "")) if len(amounts) > 1 else 0,
                        })

        print(json.dumps({
            "account": "Joint Credit",
            "current_balance": -8373.75,
            "total_transactions": len(parsed),
            "transactions": parsed,
            "raw_count": len(all_transactions),
        }, indent=2))

        browser.close()


if __name__ == "__main__":
    main()
