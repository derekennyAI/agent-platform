#!/usr/bin/env python3
"""Weekly personal finance report from Quicken Classic web data.

Logs into Quicken, downloads CC transactions via CSV, analyzes spending patterns,
and generates an HTML report emailed to specified recipients.

Usage:
    python3 quicken_weekly_report.py
    python3 quicken_weekly_report.py --dry-run   # print HTML, don't send
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

_SCREENSHOT_DIR = Path("/tmp/quicken_screenshots")
QUICKEN_APP = "https://app.quicken.com"
PLAYWRIGHT_BROWSERS = str(Path.home() / ".cache/ms-playwright")
SEND_EMAIL = Path(__file__).resolve().parent.parent.parent / "idea-hunter" / "scripts" / "send_email.py"

RECIPIENTS = [
    "YOUR_EMAIL",
    "laurenclarebailey@gmail.com",
]


def download_quicken_csv(account_name="Joint Credit"):
    """Login to Quicken and download CSV for the specified account. Returns path to CSV."""
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    secrets = load_secrets()
    email = secrets.get("quicken_email") or secrets.get("quicken_credentials")
    password = secrets["quicken_password"]
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)

    csv_path = None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox",
                  "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = browser.new_context(
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 900},
            accept_downloads=True,
        )
        page = ctx.new_page()
        page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        # Login
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

        # Email in iframe
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
            print("[quicken] Login failed — no email field", file=sys.stderr)
            browser.close()
            return None

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

        # Navigate to Transactions
        tx_link = page.query_selector('a[href*="transaction"], a:has-text("Transactions")')
        if tx_link:
            tx_link.click()
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        # Click target account
        account_items = page.query_selector_all(
            f'span:has-text("{account_name}"), a:has-text("{account_name}"), '
            f'[class*="account"]:has-text("{account_name}")'
        )
        for item in account_items:
            text = (item.text_content() or "").strip()
            if account_name.lower() in text.lower() and len(text) < 50:
                item.click()
                break
        page.wait_for_timeout(3000)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except PWTimeout:
            pass

        # Download CSV
        toolbar_btns = page.query_selector_all('button')
        for btn in toolbar_btns:
            label = btn.get_attribute("aria-label") or ""
            title = btn.get_attribute("title") or ""
            text = (btn.text_content() or "").strip()
            cls = btn.get_attribute("class") or ""
            if any(kw in (label + title + text + cls).lower() for kw in ["download", "export", "csv"]):
                try:
                    with page.expect_download(timeout=15000) as dl_info:
                        btn.click()
                    dl = dl_info.value
                    csv_path = f"/tmp/quicken_{account_name.replace(' ', '_').lower()}.csv"
                    dl.save_as(csv_path)
                    print(f"[quicken] CSV saved: {csv_path}", file=sys.stderr)
                    break
                except Exception as e:
                    print(f"[quicken] Download failed: {e}", file=sys.stderr)

        # Also get account balances from the sidebar
        accounts_text = ""
        sidebar = page.query_selector('[class*="sidebar"], [class*="Sidebar"], nav')
        if sidebar:
            accounts_text = sidebar.text_content() or ""

        browser.close()

    return csv_path, accounts_text


def parse_transactions(csv_path):
    """Parse Quicken CSV into structured transaction list."""
    txs = []
    with open(csv_path, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        for row in reader:
            amt_str = row.get('amount', '').replace('$', '').replace(',', '')
            try:
                amt = float(amt_str)
            except ValueError:
                continue
            try:
                dt = datetime.strptime(row['postedOn'], '%m/%d/%Y')
            except (ValueError, KeyError):
                continue
            txs.append({
                'date': dt,
                'month': dt.strftime('%Y-%m'),
                'week': dt.isocalendar()[1],
                'payee': row.get('payee', '').strip(),
                'category': row.get('category', '').strip(),
                'amount': amt,
                'memo': row.get('memo', '').strip(),
            })
    return txs


def simplify_category(cat):
    """Map Quicken subcategories to simplified parent categories."""
    c = cat.lower()
    if ('food' in c or 'dining' in c or 'restaurant' in c or 'grocer' in c or
        'coffee' in c or 'fast food' in c or 'alcohol' in c or 'bar' in c) and 'pet' not in c:
        return 'Food & Dining'
    elif 'pet' in c:
        return 'Pets'
    elif 'shopping' in c or 'clothing' in c or 'sporting' in c:
        return 'Shopping'
    elif 'auto' in c or 'gas' in c or 'transport' in c:
        return 'Auto & Transport'
    elif 'home' in c or 'mortgage' in c or 'rent' in c or 'improvement' in c:
        return 'Home'
    elif 'entertainment' in c or 'music' in c or 'movie' in c:
        return 'Entertainment'
    elif 'bill' in c or 'utilit' in c:
        return 'Bills & Utilities'
    elif 'travel' in c or 'air' in c or 'hotel' in c:
        return 'Travel'
    elif 'personal' in c or 'gym' in c or 'health' in c or 'fitness' in c:
        return 'Personal & Health'
    else:
        return 'Other'


def analyze(txs):
    """Run MTD + YTD spending analysis. Returns None if too early in month."""
    import calendar
    now = datetime.now()
    this_month = now.strftime('%Y-%m')
    last_month = (now.replace(day=1) - timedelta(days=1)).strftime('%Y-%m')
    ytd_start = f"{now.year}-01"

    # Monthly breakdown
    monthly = defaultdict(lambda: {'spending': 0, 'payments': 0, 'count': 0})
    for tx in txs:
        m = tx['month']
        monthly[m]['count'] += 1
        if tx['amount'] < 0:
            monthly[m]['spending'] += abs(tx['amount'])
        else:
            monthly[m]['payments'] += tx['amount']

    months_sorted = sorted(monthly.keys(), reverse=True)[:6]

    # Too-early check: if day <= 7 and fewer than 5 spending transactions this month, skip
    mtd_txs = [tx for tx in txs if tx['month'] == this_month and tx['amount'] < 0]
    if now.day <= 7 and len(mtd_txs) < 5:
        return None  # Not enough data yet

    # MTD category breakdown
    cat_totals = defaultdict(float)
    for tx in mtd_txs:
        cat_totals[tx['category']] += abs(tx['amount'])

    # Simplify categories
    simplified_cats = defaultdict(float)
    for cat, total in cat_totals.items():
        simplified_cats[simplify_category(cat)] += total
    top_simplified = sorted(simplified_cats.items(), key=lambda x: x[1], reverse=True)

    # MTD totals
    mtd_spending = monthly[this_month]['spending']
    mtd_payments = monthly[this_month]['payments']
    last_m_spend = monthly[last_month]['spending'] if last_month in monthly else 0

    # MTD daily average
    days_in_month = now.day
    daily_avg = mtd_spending / days_in_month if days_in_month > 0 else 0

    # Last month daily average (full month)
    last_month_dt = now.replace(day=1) - timedelta(days=1)
    last_month_days = calendar.monthrange(last_month_dt.year, last_month_dt.month)[1]
    last_daily_avg = monthly[last_month]['spending'] / last_month_days if last_month in monthly else 0

    # Projected month-end spend
    total_month_days = calendar.monthrange(now.year, now.month)[1]
    days_remaining = total_month_days - now.day
    projected_total = mtd_spending + (daily_avg * days_remaining)

    # 3-month spending average
    recent_3 = sorted(monthly.keys(), reverse=True)[:3]
    avg_3mo = sum(monthly[m]['spending'] for m in recent_3) / len(recent_3) if recent_3 else 0

    # YTD totals
    ytd_months = [m for m in monthly if m >= ytd_start]
    ytd_spending = sum(monthly[m]['spending'] for m in ytd_months)
    ytd_payments = sum(monthly[m]['payments'] for m in ytd_months)
    ytd_months_count = len(ytd_months)
    ytd_monthly_avg = ytd_spending / ytd_months_count if ytd_months_count > 0 else 0

    # Days since last real payment
    payments = [tx for tx in txs if tx['amount'] > 0 and 'payment' in tx['category'].lower()]
    payments.sort(key=lambda x: x['date'], reverse=True)
    real_payments = [p for p in payments if p['amount'] > 100]
    days_since_payment = (now - real_payments[0]['date']).days if real_payments else 999

    # Food & Dining MTD
    food_total = sum(v for k, v in cat_totals.items()
                     if ('food' in k.lower() or 'dining' in k.lower() or 'restaurant' in k.lower() or
                         'grocer' in k.lower() or 'coffee' in k.lower() or 'fast food' in k.lower() or
                         'alcohol' in k.lower()) and 'pet' not in k.lower())

    return {
        'this_month': this_month,
        'month_name': now.strftime('%B %Y'),
        'days_in_month': days_in_month,
        'total_month_days': total_month_days,
        # MTD
        'mtd_spending': mtd_spending,
        'mtd_payments': mtd_payments,
        'last_month_spending': last_m_spend,
        'daily_avg': daily_avg,
        'last_daily_avg': last_daily_avg,
        'projected_total': projected_total,
        'avg_3mo': avg_3mo,
        'top_categories': top_simplified,
        'food_total': food_total,
        # YTD
        'ytd_spending': ytd_spending,
        'ytd_payments': ytd_payments,
        'ytd_monthly_avg': ytd_monthly_avg,
        'ytd_months_count': ytd_months_count,
        # Trend
        'monthly': {m: monthly[m] for m in months_sorted},
        'months_sorted': months_sorted,
        # Payments
        'days_since_payment': days_since_payment,
        'recent_payments': payments[:5],
    }


def fc(amount):
    """Format currency — no cents for big numbers."""
    if abs(amount) >= 100:
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def generate_html(data, interactive_url=""):
    """Generate MTD + YTD HTML email report — big numbers, behavioral insights."""
    now = datetime.now()
    mtd_spend = data['mtd_spending']
    last_m_spend = data['last_month_spending']

    # Month-over-month comparison
    if last_m_spend > 0:
        pct_change = ((mtd_spend - last_m_spend) / last_m_spend) * 100
        trend_arrow = "&#x25B2;" if pct_change > 0 else "&#x25BC;"
        trend_color = "#e74c3c" if pct_change > 0 else "#27ae60"
        trend_text = f"{trend_arrow} {abs(pct_change):.0f}% vs last month"
    else:
        trend_text = ""
        trend_color = "#666"

    # Payment alert
    days_since = data['days_since_payment']
    if days_since > 25:
        payment_alert = f"""
        <div style="background:#fde8e8;border-radius:12px;padding:20px 25px;margin-bottom:20px;border-left:5px solid #e74c3c">
            <div style="font-size:14px;font-weight:600;color:#c0392b;text-transform:uppercase;letter-spacing:1px">&#9888; Payment Overdue</div>
            <div style="font-size:16px;color:#333;margin-top:8px">It's been <strong>{days_since} days</strong> since your last payment. The balance grows every day it sits unpaid.</div>
        </div>"""
    elif days_since > 14:
        payment_alert = f"""
        <div style="background:#fff8e1;border-radius:12px;padding:20px 25px;margin-bottom:20px;border-left:5px solid #f39c12">
            <div style="font-size:14px;font-weight:600;color:#e67e22;text-transform:uppercase;letter-spacing:1px">&#128276; Payment Reminder</div>
            <div style="font-size:16px;color:#333;margin-top:8px"><strong>{days_since} days</strong> since last payment. A payment now would bring the balance back under control.</div>
        </div>"""
    else:
        payment_alert = ""

    # Projected spend insight
    proj = data['projected_total']
    avg3 = data['avg_3mo']
    if proj > avg3 * 1.15:
        pace_msg = f"On pace to spend <strong>{fc(proj)}</strong> this month — above your 3-month average of {fc(avg3)}."
        pace_color = "#e74c3c"
    elif proj < avg3 * 0.9:
        pace_msg = f"On pace to spend <strong>{fc(proj)}</strong> this month — below your 3-month average of {fc(avg3)}. Nice."
        pace_color = "#27ae60"
    else:
        pace_msg = f"On pace to spend <strong>{fc(proj)}</strong> this month — roughly in line with your 3-month average of {fc(avg3)}."
        pace_color = "#2c3e50"

    # Category bars
    cat_blocks = ""
    cat_colors = {
        'Food & Dining': '#e74c3c', 'Shopping': '#3498db', 'Auto & Transport': '#f39c12',
        'Home': '#9b59b6', 'Entertainment': '#e91e63', 'Bills & Utilities': '#607d8b',
        'Travel': '#00bcd4', 'Personal & Health': '#4caf50', 'Pets': '#8d6e63', 'Other': '#95a5a6',
    }
    for cat, total in data['top_categories']:
        pct = (total / mtd_spend * 100) if mtd_spend > 0 else 0
        color = cat_colors.get(cat, '#95a5a6')
        cat_blocks += f"""
        <div style="margin-bottom:12px">
            <div style="display:flex;justify-content:space-between;margin-bottom:4px">
                <span style="font-size:14px;font-weight:500">{cat}</span>
                <span style="font-size:14px;font-weight:600">{fc(total)}</span>
            </div>
            <div style="background:#f0f0f0;border-radius:6px;height:20px;width:100%;overflow:hidden">
                <div style="background:{color};border-radius:6px;height:20px;width:{min(pct, 100):.0f}%;min-width:2px"></div>
            </div>
            <div style="font-size:11px;color:#999;margin-top:2px">{pct:.0f}% of total</div>
        </div>"""

    # Monthly trend bars
    monthly_bars = ""
    max_spend = max(d['spending'] for d in data['monthly'].values()) if data['monthly'] else 1
    for m in data['months_sorted']:
        d = data['monthly'][m]
        bar_pct = (d['spending'] / max_spend * 100) if max_spend > 0 else 0
        net = d['payments'] - d['spending']
        net_color = "#27ae60" if net >= 0 else "#e74c3c"
        net_icon = "+" if net >= 0 else ""
        month_label = datetime.strptime(m, '%Y-%m').strftime('%b %Y')
        monthly_bars += f"""
        <div style="margin-bottom:14px">
            <div style="display:flex;justify-content:space-between;align-items:baseline;margin-bottom:3px">
                <span style="font-size:13px;font-weight:500;min-width:70px">{month_label}</span>
                <span style="font-size:13px">{fc(d['spending'])} spent</span>
                <span style="font-size:13px;color:{net_color};font-weight:600">{net_icon}{fc(net)} net</span>
            </div>
            <div style="background:#f0f0f0;border-radius:4px;height:14px;width:100%;overflow:hidden">
                <div style="background:{'#e74c3c' if net < 0 else '#3498db'};border-radius:4px;height:14px;width:{bar_pct:.0f}%"></div>
            </div>
        </div>"""

    # Daily spending comparison
    daily = data['daily_avg']
    last_daily = data['last_daily_avg']
    if last_daily > 0:
        daily_diff = ((daily - last_daily) / last_daily) * 100
        daily_trend = f"{'&#x25B2;' if daily_diff > 0 else '&#x25BC;'} {abs(daily_diff):.0f}% vs last month"
        daily_color = "#e74c3c" if daily_diff > 0 else "#27ae60"
    else:
        daily_trend = ""
        daily_color = "#666"

    html = f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:640px;margin:0 auto;padding:20px;color:#333;line-height:1.5">

<!-- HEADER -->
<div style="text-align:center;padding:30px 0 20px;border-bottom:2px solid #eee;margin-bottom:25px">
    <div style="font-size:13px;color:#999;text-transform:uppercase;letter-spacing:2px">Personal Finance Report</div>
    <div style="font-size:15px;color:#666;margin-top:5px">{now.strftime('%B %d, %Y')}</div>
</div>

{payment_alert}

<!-- MTD SCOREBOARD -->
<div style="background:linear-gradient(135deg, #2c3e50 0%, #34495e 100%);border-radius:14px;padding:28px;margin-bottom:25px;color:white">
    <div style="font-size:13px;text-transform:uppercase;letter-spacing:2px;opacity:0.7;margin-bottom:15px">Month to Date &mdash; {data['month_name']} &mdash; Day {data['days_in_month']}</div>

    <div style="display:flex;gap:15px;margin-bottom:20px">
        <div style="flex:1;text-align:center">
            <div style="font-size:11px;text-transform:uppercase;opacity:0.6">Spent</div>
            <div style="font-size:28px;font-weight:700">{fc(mtd_spend)}</div>
        </div>
        <div style="flex:1;text-align:center">
            <div style="font-size:11px;text-transform:uppercase;opacity:0.6">Paid</div>
            <div style="font-size:28px;font-weight:700;color:#2ecc71">{fc(data['mtd_payments'])}</div>
        </div>
        <div style="flex:1;text-align:center">
            <div style="font-size:11px;text-transform:uppercase;opacity:0.6">Daily Avg</div>
            <div style="font-size:28px;font-weight:700">{fc(daily)}</div>
        </div>
    </div>

    <div style="background:rgba(255,255,255,0.1);border-radius:8px;padding:12px 16px;font-size:14px">
        <span style="color:{trend_color}">{trend_text}</span>
        &nbsp;&middot;&nbsp;
        <span style="color:{daily_color}">{daily_trend} daily</span>
    </div>
</div>

<!-- PACE CHECK -->
<div style="background:#f8f9fa;border-radius:12px;padding:20px 25px;margin-bottom:25px">
    <div style="font-size:14px;font-weight:600;color:#2c3e50;margin-bottom:8px">&#127939; Pace Check</div>
    <div style="font-size:15px;color:{pace_color}">{pace_msg}</div>
</div>

<!-- YTD SUMMARY -->
<div style="background:linear-gradient(135deg, #1a5276 0%, #21618c 100%);border-radius:14px;padding:28px;margin-bottom:25px;color:white">
    <div style="font-size:13px;text-transform:uppercase;letter-spacing:2px;opacity:0.7;margin-bottom:15px">Year to Date &mdash; {now.year}</div>

    <div style="display:flex;gap:15px;margin-bottom:12px">
        <div style="flex:1;text-align:center">
            <div style="font-size:11px;text-transform:uppercase;opacity:0.6">Total Spent</div>
            <div style="font-size:28px;font-weight:700">{fc(data['ytd_spending'])}</div>
        </div>
        <div style="flex:1;text-align:center">
            <div style="font-size:11px;text-transform:uppercase;opacity:0.6">Total Paid</div>
            <div style="font-size:28px;font-weight:700;color:#2ecc71">{fc(data['ytd_payments'])}</div>
        </div>
        <div style="flex:1;text-align:center">
            <div style="font-size:11px;text-transform:uppercase;opacity:0.6">Monthly Avg</div>
            <div style="font-size:28px;font-weight:700">{fc(data['ytd_monthly_avg'])}</div>
        </div>
    </div>
</div>

<!-- WHERE IT'S GOING — MTD Categories -->
<div style="margin-bottom:30px">
    <div style="font-size:18px;font-weight:600;color:#2c3e50;margin-bottom:18px">Where It's Going — MTD</div>
    {cat_blocks}
    <div style="text-align:center;margin-top:18px">
        <a href="{interactive_url}" style="display:inline-block;background:#2c3e50;color:white;padding:12px 28px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600;letter-spacing:0.5px">View Full Breakdown &rarr;</a>
        <div style="font-size:11px;color:#999;margin-top:6px">Tap any category to see every transaction</div>
    </div>
</div>

<!-- MONTHLY TREND -->
<div style="margin-bottom:30px">
    <div style="font-size:18px;font-weight:600;color:#2c3e50;margin-bottom:18px">6-Month Trend</div>
    {monthly_bars}
</div>

<!-- FOOD INSIGHT -->
<div style="background:#fff8f0;border-radius:12px;padding:20px 25px;margin-bottom:25px;border-left:5px solid #e67e22">
    <div style="font-size:14px;font-weight:600;color:#d35400">&#127869; Food & Dining — MTD</div>
    <div style="font-size:32px;font-weight:700;color:#2c3e50;margin:8px 0">{fc(data['food_total'])}</div>
    <div style="font-size:13px;color:#666">That's {fc(data['food_total'] / max(data['days_in_month'], 1))}/day on food — restaurants, groceries, takeout, and drinks combined.</div>
</div>

<!-- FOOTER -->
<hr style="border:none;border-top:1px solid #eee;margin:30px 0 15px">
<p style="font-size:11px;color:#bbb;text-align:center">
    Generated by Derek &middot; Data from Quicken Classic &middot; {now.strftime('%B %d, %Y at %I:%M %p')}
</p>

</body>
</html>"""

    return html


def main():
    parser = argparse.ArgumentParser(description="Personal finance report — MTD & YTD")
    parser.add_argument("--dry-run", action="store_true", help="Print HTML, don't send")
    parser.add_argument("--csv", type=str, default=None, help="Use existing CSV instead of downloading")
    args = parser.parse_args()

    if args.csv:
        csv_path = args.csv
        print(f"[report] Using existing CSV: {csv_path}", file=sys.stderr)
    else:
        print("[report] Downloading Quicken data...", file=sys.stderr)
        result = download_quicken_csv("Joint Credit")
        if not result or not result[0]:
            print("[error] Failed to download Quicken data", file=sys.stderr)
            sys.exit(1)
        csv_path = result[0]

    print("[report] Parsing transactions...", file=sys.stderr)
    txs = parse_transactions(csv_path)
    print(f"[report] Parsed {len(txs)} transactions", file=sys.stderr)

    data = analyze(txs)

    # Too early in month — not enough transactions yet
    if data is None:
        print("[report] Too early in the month — not enough transactions to report. Skipping.", file=sys.stderr)
        return

    # Generate and upload interactive report
    interactive_url = ""
    try:
        from quicken_interactive_report import generate_interactive_html, publish_report, parse_transactions as ir_parse
        ir_txs = ir_parse(csv_path)
        ir_html = generate_interactive_html(ir_txs)
        now = datetime.now()
        filename = f"finance-report-{now.strftime('%Y-%m-%d')}.html"
        ir_path = Path("/tmp") / filename
        ir_path.write_text(ir_html)
        url = publish_report(ir_html, filename)
        if url:
            interactive_url = url
            print(f"[report] Interactive report: {url}", file=sys.stderr)
    except Exception as e:
        print(f"[report] Interactive report skipped: {e}", file=sys.stderr)

    html = generate_html(data, interactive_url=interactive_url)

    if args.dry_run:
        print(html)
        return

    report_path = Path("/tmp/quicken_weekly_report.html")
    report_path.write_text(html)
    print(f"[report] HTML saved: {report_path}", file=sys.stderr)

    now = datetime.now()
    subject = f"Personal Finance — {data['month_name']} MTD + YTD"
    for recipient in RECIPIENTS:
        print(f"[report] Sending to {recipient}...", file=sys.stderr)
        import subprocess
        result = subprocess.run(
            [sys.executable, str(SEND_EMAIL),
             "--to", recipient,
             "--subject", subject,
             "--html", "--stdin"],
            input=html, capture_output=True, text=True
        )
        if result.returncode == 0:
            print(f"[report] Sent to {recipient}: {result.stdout.strip()}", file=sys.stderr)
        else:
            print(f"[error] Failed to send to {recipient}: {result.stderr}", file=sys.stderr)

    print("[report] Done!", file=sys.stderr)


if __name__ == "__main__":
    main()
