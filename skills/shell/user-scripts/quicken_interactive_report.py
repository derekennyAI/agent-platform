#!/usr/bin/env python3
"""Generate interactive HTML finance report with click-to-expand categories.

Outputs a self-contained HTML file with JavaScript for interactivity.
Can be uploaded to Supabase Storage for a shareable link.

Usage:
    python3 quicken_interactive_report.py --csv /tmp/quicken_cc_download.csv
    python3 quicken_interactive_report.py --csv /tmp/quicken_cc_download.csv --upload
"""

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.request
import urllib.error
from collections import defaultdict
from datetime import datetime, timedelta, date
from pathlib import Path
import calendar

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

OUTPUT_DIR = Path("/tmp")


def parse_transactions(csv_path):
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
                'date': dt.strftime('%m/%d/%Y'),
                'date_obj': dt,
                'month': dt.strftime('%Y-%m'),
                'payee': row.get('payee', '').strip(),
                'category': row.get('category', '').strip(),
                'amount': amt,
            })
    return txs


def simplify_category(cat):
    c = cat.lower()
    if ('food' in c or 'dining' in c or 'restaurant' in c or 'grocer' in c or
        'coffee' in c or 'fast food' in c or 'alcohol' in c or 'bar' in c) and 'pet' not in c:
        return 'Food & Dining'
    elif 'pet' in c:
        return 'Pets'
    elif 'shopping' in c or 'clothing' in c or 'sporting' in c:
        return 'Shopping'
    elif 'auto' in c or 'gas' in c or 'transport' in c or 'parking' in c:
        return 'Auto & Transport'
    elif 'home' in c or 'mortgage' in c or 'rent' in c or 'improvement' in c:
        return 'Home'
    elif 'entertainment' in c or 'music' in c or 'movie' in c:
        return 'Entertainment'
    elif 'bill' in c or 'utilit' in c or 'television' in c:
        return 'Bills & Utilities'
    elif 'travel' in c or 'air travel' in c or 'hotel' in c:
        return 'Travel'
    elif 'personal' in c or 'gym' in c or 'health' in c or 'fitness' in c:
        return 'Personal & Health'
    elif 'business' in c:
        return 'Business'
    elif 'fee' in c or 'finance' in c or 'financial' in c:
        return 'Fees & Charges'
    else:
        return 'Other'


def fc(amount):
    if abs(amount) >= 100:
        return f"${amount:,.0f}"
    return f"${amount:,.2f}"


def generate_interactive_html(txs):
    now = datetime.now()
    this_month = now.strftime('%Y-%m')

    # Build all transactions as JSON for client-side rendering
    all_txs = []
    for tx in txs:
        all_txs.append({
            'date': tx['date'],
            'month': tx['month'],
            'payee': tx['payee'],
            'category': tx['category'],
            'parent_cat': simplify_category(tx['category']),
            'amount': tx['amount'],
        })

    # Monthly summary for trend chart
    monthly = defaultdict(lambda: {'spending': 0, 'payments': 0})
    for tx in txs:
        m = tx['month']
        if tx['amount'] < 0:
            monthly[m]['spending'] += abs(tx['amount'])
        else:
            monthly[m]['payments'] += tx['amount']

    months_sorted = sorted(monthly.keys(), reverse=True)
    monthly_data = []
    for m in months_sorted:
        d = monthly[m]
        monthly_data.append({
            'key': m,
            'label': datetime.strptime(m, '%Y-%m').strftime('%b %Y'),
            'spending': d['spending'],
            'payments': d['payments'],
            'net': d['payments'] - d['spending'],
        })

    # Days since last real payment
    payments = sorted([tx for tx in txs if tx['amount'] > 0 and 'payment' in tx['category'].lower()],
                      key=lambda x: x['date_obj'], reverse=True)
    real_payments = [p for p in payments if p['amount'] > 100]
    days_since_payment = (now - real_payments[0]['date_obj']).days if real_payments else 999

    data_json = json.dumps({
        'transactions': all_txs,
        'monthly': monthly_data,
        'available_months': months_sorted,
        'current_month': this_month,
        'today': now.strftime('%Y-%m-%d'),
        'days_since_payment': days_since_payment,
    })

    cat_colors_json = json.dumps({
        'Food & Dining': '#e74c3c', 'Shopping': '#3498db', 'Auto & Transport': '#f39c12',
        'Home': '#9b59b6', 'Entertainment': '#e91e63', 'Bills & Utilities': '#607d8b',
        'Travel': '#00bcd4', 'Personal & Health': '#4caf50', 'Pets': '#8d6e63',
        'Business': '#ff7043', 'Fees & Charges': '#78909c', 'Other': '#95a5a6',
    })

    food_cats_json = json.dumps(['Food & Dining'])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Personal Finance Report</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif; background:#f5f6fa; color:#333; line-height:1.5; }}
.container {{ max-width:680px; margin:0 auto; padding:16px; }}
.header {{ text-align:center; padding:30px 0 20px; }}
.header .label {{ font-size:12px; color:#999; text-transform:uppercase; letter-spacing:2px; }}
.header .title {{ font-size:15px; color:#666; margin-top:4px; }}

.month-nav {{ display:flex; align-items:center; justify-content:center; gap:16px; margin-bottom:20px; }}
.month-nav button {{ background:none; border:2px solid #ddd; border-radius:50%; width:40px; height:40px; font-size:18px; cursor:pointer; color:#2c3e50; transition:all .2s; display:flex; align-items:center; justify-content:center; }}
.month-nav button:hover {{ background:#2c3e50; color:white; border-color:#2c3e50; }}
.month-nav button:disabled {{ opacity:.3; cursor:default; }}
.month-nav button:disabled:hover {{ background:none; color:#2c3e50; border-color:#ddd; }}
.month-nav .month-label {{ font-size:20px; font-weight:700; color:#2c3e50; min-width:180px; text-align:center; }}

.alert {{ border-radius:12px; padding:18px 22px; margin-bottom:18px; }}
.alert.danger {{ background:#fde8e8; border-left:5px solid #e74c3c; }}
.alert.warning {{ background:#fff8e1; border-left:5px solid #f39c12; }}
.alert .alert-title {{ font-size:13px; font-weight:600; text-transform:uppercase; letter-spacing:1px; }}
.alert.danger .alert-title {{ color:#c0392b; }}
.alert.warning .alert-title {{ color:#e67e22; }}
.alert .alert-body {{ font-size:15px; margin-top:6px; }}

.scoreboard {{ background:linear-gradient(135deg,#2c3e50,#34495e); border-radius:14px; padding:24px; margin-bottom:20px; color:#fff; }}
.scoreboard .label {{ font-size:12px; text-transform:uppercase; letter-spacing:2px; opacity:.6; margin-bottom:14px; }}
.score-grid {{ display:flex; gap:12px; margin-bottom:16px; }}
.score-item {{ flex:1; text-align:center; }}
.score-item .small {{ font-size:10px; text-transform:uppercase; opacity:.5; }}
.score-item .num {{ font-size:26px; font-weight:700; }}
.score-item .num.green {{ color:#2ecc71; }}
.score-meta {{ background:rgba(255,255,255,.1); border-radius:8px; padding:10px 14px; font-size:13px; }}

.card {{ background:#fff; border-radius:12px; padding:20px 22px; margin-bottom:16px; box-shadow:0 1px 4px rgba(0,0,0,.06); }}
.card .card-title {{ font-size:16px; font-weight:600; color:#2c3e50; margin-bottom:14px; }}

.category {{ border-radius:10px; margin-bottom:8px; overflow:hidden; border:1px solid #e0e0e0; background:#fff; transition:box-shadow .2s; }}
.category:hover {{ box-shadow:0 2px 8px rgba(0,0,0,.08); }}
.cat-header {{ display:flex; align-items:center; padding:16px 18px; cursor:pointer; user-select:none; }}
.cat-header:active {{ background:#f0f4ff; }}
.cat-dot {{ width:12px; height:12px; border-radius:50%; margin-right:12px; flex-shrink:0; }}
.cat-name {{ flex:1; font-size:15px; font-weight:500; }}
.cat-amount {{ font-size:15px; font-weight:600; margin-right:8px; }}
.cat-pct {{ font-size:12px; color:#999; min-width:40px; text-align:right; margin-right:8px; }}
.cat-arrow {{ font-size:16px; color:#3498db; transition:transform .2s; font-weight:bold; }}
.cat-arrow.open {{ transform:rotate(90deg); }}
.cat-bar {{ height:6px; background:#f0f0f0; margin:0 18px 4px; border-radius:3px; }}
.cat-bar-fill {{ height:6px; border-radius:3px; transition:width .4s; }}
.cat-detail {{ display:none; padding:0 18px 14px; }}
.cat-detail.open {{ display:block; }}

.sub-group {{ margin-bottom:10px; }}
.sub-header {{ font-size:13px; font-weight:600; color:#666; padding:6px 0; border-bottom:1px solid #f5f5f5; display:flex; justify-content:space-between; }}
.tx-row {{ display:flex; padding:5px 0; border-bottom:1px solid #fafafa; font-size:13px; }}
.tx-date {{ width:70px; color:#999; flex-shrink:0; }}
.tx-payee {{ flex:1; }}
.tx-amount {{ width:80px; text-align:right; font-weight:500; }}

.trend-row {{ display:flex; align-items:center; gap:10px; margin-bottom:12px; padding:8px 0; }}
.trend-label {{ width:70px; font-size:13px; font-weight:500; flex-shrink:0; }}
.trend-bar-wrap {{ flex:1; height:18px; background:#f0f0f0; border-radius:4px; overflow:hidden; position:relative; }}
.trend-bar {{ height:18px; border-radius:4px; transition:width .5s; }}
.trend-nums {{ font-size:12px; min-width:140px; text-align:right; }}
.trend-nums .net {{ font-weight:600; }}
.trend-nums .net.pos {{ color:#27ae60; }}
.trend-nums .net.neg {{ color:#e74c3c; }}

.food-card {{ background:#fff8f0; border-radius:12px; padding:20px 22px; margin-bottom:16px; border-left:5px solid #e67e22; }}
.food-card .food-title {{ font-size:13px; font-weight:600; color:#d35400; }}
.food-card .food-num {{ font-size:34px; font-weight:700; color:#2c3e50; margin:6px 0; }}
.food-card .food-detail {{ font-size:13px; color:#666; }}

.footer {{ text-align:center; padding:20px 0; font-size:11px; color:#bbb; }}
</style>
</head>
<body>

<div class="container">
    <div class="header">
        <div class="label">Personal Finance Report</div>
    </div>

    <!-- MONTH NAVIGATION -->
    <div class="month-nav">
        <button id="prev-month" onclick="changeMonth(-1)">&#9664;</button>
        <div class="month-label" id="month-label"></div>
        <button id="next-month" onclick="changeMonth(1)">&#9654;</button>
    </div>
    <div style="text-align:center;margin-top:-12px;margin-bottom:18px">
        <button id="today-btn" onclick="goToCurrentMonth()" style="background:#2c3e50;color:white;border:none;border-radius:6px;padding:6px 16px;font-size:12px;font-weight:600;cursor:pointer;text-transform:uppercase;letter-spacing:1px;display:none">This Month</button>
    </div>

    <div id="payment-alert"></div>

    <div class="scoreboard">
        <div class="label" id="mtd-label"></div>
        <div class="score-grid">
            <div class="score-item">
                <div class="small">Spent</div>
                <div class="num" id="month-spent"></div>
            </div>
            <div class="score-item">
                <div class="small">Paid</div>
                <div class="num green" id="month-paid"></div>
            </div>
            <div class="score-item">
                <div class="small">Daily Avg</div>
                <div class="num" id="daily-avg"></div>
            </div>
        </div>
        <div class="score-meta" id="score-meta"></div>
    </div>

    <div class="card" id="pace-card">
        <div class="card-title">&#127939; Pace Check</div>
        <div id="pace-body" style="font-size:15px"></div>
    </div>

    <!-- YTD SUMMARY -->
    <div class="scoreboard" style="background:linear-gradient(135deg,#1a5276,#21618c)">
        <div class="label" id="ytd-label"></div>
        <div class="score-grid">
            <div class="score-item">
                <div class="small">Total Spent</div>
                <div class="num" id="ytd-spent"></div>
            </div>
            <div class="score-item">
                <div class="small">Total Paid</div>
                <div class="num green" id="ytd-paid"></div>
            </div>
            <div class="score-item">
                <div class="small">Monthly Avg</div>
                <div class="num" id="ytd-avg"></div>
            </div>
        </div>
    </div>

    <div style="margin-bottom:16px">
        <div style="font-size:18px;font-weight:600;color:#2c3e50;margin-bottom:14px;padding:0 4px">
            Where It's Going <span style="font-size:12px;color:#3498db;font-weight:500">&#8212; tap any row &#9660;</span>
        </div>
        <div id="categories"></div>
    </div>

    <div class="food-card">
        <div class="food-title" id="food-title">&#127869; Food &amp; Dining</div>
        <div class="food-num" id="food-total"></div>
        <div class="food-detail" id="food-detail"></div>
    </div>

    <div class="card">
        <div class="card-title">6-Month Trend</div>
        <div id="trend"></div>
    </div>

    <div class="footer">
        Generated by Derek &middot; Data from Quicken Classic &middot; {now.strftime('%B %d, %Y at %I:%M %p')}
    </div>
</div>

<script>
const DATA = {data_json};
const CAT_COLORS = {cat_colors_json};
const FOOD_CATS = {food_cats_json};
const MONTH_NAMES = ['January','February','March','April','May','June','July','August','September','October','November','December'];

let selectedMonth = DATA.current_month;

function fc(n) {{ return n >= 100 ? '$' + Math.round(n).toLocaleString() : '$' + n.toFixed(2); }}

function parseMonth(m) {{
    const [y, mo] = m.split('-').map(Number);
    return {{ year: y, month: mo }};
}}

function monthKey(y, m) {{
    return y + '-' + String(m).padStart(2, '0');
}}

function daysInMonth(y, m) {{
    return new Date(y, m, 0).getDate();
}}

function prevMonthKey(m) {{
    const p = parseMonth(m);
    if (p.month === 1) return monthKey(p.year - 1, 12);
    return monthKey(p.year, p.month - 1);
}}

function nextMonthKey(m) {{
    const p = parseMonth(m);
    if (p.month === 12) return monthKey(p.year + 1, 1);
    return monthKey(p.year, p.month + 1);
}}

function changeMonth(dir) {{
    const idx = DATA.available_months.indexOf(selectedMonth);
    const newIdx = idx - dir; // available_months is sorted desc
    if (newIdx >= 0 && newIdx < DATA.available_months.length) {{
        selectedMonth = DATA.available_months[newIdx];
        render();
    }}
}}

function goToCurrentMonth() {{
    selectedMonth = DATA.current_month;
    render();
}}

function render() {{
    const m = selectedMonth;
    const p = parseMonth(m);
    const isCurrentMonth = (m === DATA.current_month);
    const today = new Date(DATA.today);

    // Nav buttons
    const idx = DATA.available_months.indexOf(m);
    document.getElementById('next-month').disabled = (idx <= 0);
    document.getElementById('prev-month').disabled = (idx >= DATA.available_months.length - 1);
    document.getElementById('today-btn').style.display = isCurrentMonth ? 'none' : 'inline-block';

    // Month label
    document.getElementById('month-label').textContent = MONTH_NAMES[p.month - 1] + ' ' + p.year;

    // Filter transactions for this month
    const monthTxs = DATA.transactions.filter(tx => tx.month === m);
    const spending = monthTxs.filter(tx => tx.amount < 0);
    const totalSpend = spending.reduce((s, tx) => s + Math.abs(tx.amount), 0);
    const totalPay = monthTxs.filter(tx => tx.amount > 0).reduce((s, tx) => s + tx.amount, 0);

    // Days calculation
    const totalDays = daysInMonth(p.year, p.month);
    const daysElapsed = isCurrentMonth ? today.getDate() : totalDays;
    const dailyAvg = daysElapsed > 0 ? totalSpend / daysElapsed : 0;

    // MTD label
    if (isCurrentMonth) {{
        document.getElementById('mtd-label').textContent = 'MONTH TO DATE \u2014 DAY ' + daysElapsed + ' OF ' + totalDays;
    }} else {{
        document.getElementById('mtd-label').textContent = MONTH_NAMES[p.month - 1].toUpperCase() + ' ' + p.year + ' \u2014 FULL MONTH';
    }}

    // MTD numbers
    document.getElementById('month-spent').textContent = fc(totalSpend);
    document.getElementById('month-paid').textContent = fc(totalPay);
    document.getElementById('daily-avg').textContent = fc(dailyAvg);

    // vs last month
    const prevM = prevMonthKey(m);
    const prevData = DATA.monthly.find(d => d.key === prevM);
    const prevSpend = prevData ? prevData.spending : 0;
    if (prevSpend > 0) {{
        const pctChange = ((totalSpend - prevSpend) / prevSpend) * 100;
        const arrow = pctChange > 0 ? '\u25B2' : '\u25BC';
        const clr = pctChange > 0 ? '#e74c3c' : '#27ae60';
        document.getElementById('score-meta').innerHTML = '<span style="color:' + clr + '">' + arrow + ' ' + Math.abs(pctChange).toFixed(0) + '% vs prior month</span>';
    }} else {{
        document.getElementById('score-meta').innerHTML = '';
    }}

    // Pace check (only for current month)
    const paceCard = document.getElementById('pace-card');
    if (isCurrentMonth && daysElapsed > 0) {{
        paceCard.style.display = '';
        const projected = totalSpend + (dailyAvg * (totalDays - daysElapsed));
        const recent3 = DATA.monthly.slice(0, 3);
        const avg3mo = recent3.length > 0 ? recent3.reduce((s, d) => s + d.spending, 0) / recent3.length : 0;
        const pClr = projected > avg3mo * 1.15 ? '#e74c3c' : projected < avg3mo * 0.9 ? '#27ae60' : '#2c3e50';
        document.getElementById('pace-body').innerHTML = '<span style="color:' + pClr + '">On pace to spend <strong>' + fc(projected) + '</strong> this month \u2014 3-month average is ' + fc(avg3mo) + '.</span>';
    }} else {{
        paceCard.style.display = 'none';
    }}

    // YTD
    const yearStart = monthKey(p.year, 1);
    const ytdMonths = DATA.monthly.filter(d => d.key >= yearStart && d.key <= m);
    const ytdSpend = ytdMonths.reduce((s, d) => s + d.spending, 0);
    const ytdPay = ytdMonths.reduce((s, d) => s + d.payments, 0);
    const ytdCount = ytdMonths.length || 1;
    document.getElementById('ytd-label').textContent = 'YEAR TO DATE \u2014 ' + p.year;
    document.getElementById('ytd-spent').textContent = fc(ytdSpend);
    document.getElementById('ytd-paid').textContent = fc(ytdPay);
    document.getElementById('ytd-avg').textContent = fc(ytdSpend / ytdCount);

    // Payment alert (only on current month)
    const alertEl = document.getElementById('payment-alert');
    if (isCurrentMonth && DATA.days_since_payment > 25) {{
        alertEl.innerHTML = '<div class="alert danger"><div class="alert-title">\u26A0 Payment Overdue</div><div class="alert-body">It\\'s been <strong>' + DATA.days_since_payment + ' days</strong> since your last payment.</div></div>';
    }} else if (isCurrentMonth && DATA.days_since_payment > 14) {{
        alertEl.innerHTML = '<div class="alert warning"><div class="alert-title">&#128276; Payment Reminder</div><div class="alert-body"><strong>' + DATA.days_since_payment + ' days</strong> since last payment.</div></div>';
    }} else {{
        alertEl.innerHTML = '';
    }}

    // Categories
    const catMap = {{}};
    spending.forEach(tx => {{
        const parent = tx.parent_cat;
        if (!catMap[parent]) catMap[parent] = {{ total: 0, subs: {{}} }};
        catMap[parent].total += Math.abs(tx.amount);
        const sub = tx.category || 'Uncategorized';
        if (!catMap[parent].subs[sub]) catMap[parent].subs[sub] = {{ total: 0, txs: [] }};
        catMap[parent].subs[sub].total += Math.abs(tx.amount);
        catMap[parent].subs[sub].txs.push(tx);
    }});

    const sortedCats = Object.entries(catMap).sort((a, b) => b[1].total - a[1].total);
    const catEl = document.getElementById('categories');

    if (sortedCats.length === 0) {{
        catEl.innerHTML = '<div style="text-align:center;padding:30px 20px;color:#999;font-size:15px">No transactions this month.</div>';
    }} else {{
        let catHtml = '';
        sortedCats.forEach(([name, info], i) => {{
            const pct = totalSpend > 0 ? (info.total / totalSpend * 100) : 0;
            const color = CAT_COLORS[name] || '#95a5a6';
            const sortedSubs = Object.entries(info.subs).sort((a, b) => b[1].total - a[1].total);
            let subHtml = '';
            sortedSubs.forEach(([subName, subInfo]) => {{
                const sortedTxs = subInfo.txs.sort((a, b) => b.date < a.date ? -1 : 1);
                let txHtml = '';
                sortedTxs.forEach(tx => {{
                    txHtml += '<div class="tx-row"><div class="tx-date">' + tx.date.slice(0,5) + '</div><div class="tx-payee">' + tx.payee + '</div><div class="tx-amount">' + fc(Math.abs(tx.amount)) + '</div></div>';
                }});
                subHtml += '<div class="sub-group"><div class="sub-header"><span>' + subName + '</span><span>' + fc(subInfo.total) + '</span></div>' + txHtml + '</div>';
            }});

            catHtml += '<div class="category"><div class="cat-header" onclick="toggleCat(' + i + ')"><div class="cat-dot" style="background:' + color + '"></div><div class="cat-name">' + name + '</div><div class="cat-pct">' + pct.toFixed(0) + '%</div><div class="cat-amount">' + fc(info.total) + '</div><div class="cat-arrow" id="arrow-' + i + '">\u25B6</div></div><div class="cat-bar"><div class="cat-bar-fill" style="width:' + Math.min(pct, 100).toFixed(0) + '%;background:' + color + '"></div></div><div class="cat-detail" id="detail-' + i + '">' + subHtml + '</div></div>';
        }});
        catEl.innerHTML = catHtml;
    }}

    // Food insight
    const foodSpend = sortedCats.filter(([n]) => FOOD_CATS.includes(n)).reduce((s, [, info]) => s + info.total, 0);
    const foodDaily = daysElapsed > 0 ? foodSpend / daysElapsed : 0;
    document.getElementById('food-total').textContent = fc(foodSpend);
    document.getElementById('food-detail').textContent = "That's " + fc(foodDaily) + '/day on food \u2014 restaurants, groceries, takeout, and drinks combined.';
}}

function toggleCat(i) {{
    const detail = document.getElementById('detail-' + i);
    const arrow = document.getElementById('arrow-' + i);
    if (detail) {{ detail.classList.toggle('open'); }}
    if (arrow) {{ arrow.classList.toggle('open'); }}
}}

// Trend (static — always shows last 6 months from data)
const trendEl = document.getElementById('trend');
const trendMonths = DATA.monthly.slice(0, 6);
const maxSpend = Math.max(...trendMonths.map(m => m.spending));
trendMonths.forEach(m => {{
    const pct = maxSpend > 0 ? (m.spending / maxSpend * 100).toFixed(0) : 0;
    const netCls = m.net >= 0 ? 'pos' : 'neg';
    const netSign = m.net >= 0 ? '+' : '';
    const barClr = m.net < 0 ? '#e74c3c' : '#3498db';
    trendEl.innerHTML += '<div class="trend-row"><div class="trend-label">' + m.label + '</div><div class="trend-bar-wrap"><div class="trend-bar" style="width:' + pct + '%;background:' + barClr + '"></div></div><div class="trend-nums">' + fc(m.spending) + ' &middot; <span class="net ' + netCls + '">' + netSign + fc(m.net) + '</span></div></div>';
}});

// Initial render
render();
</script>
</body>
</html>"""

    return html


def publish_report(html_content, filename):
    """Deploy HTML to Vercel and return public URL."""
    import subprocess
    deploy_dir = Path("/tmp/finance-site")
    deploy_dir.mkdir(parents=True, exist_ok=True)
    (deploy_dir / "index.html").write_text(html_content)
    print(f"[report] Deploying to Vercel...", file=sys.stderr)
    result = subprocess.run(
        ["npx", "vercel", "deploy", "--prod", "--yes"],
        cwd=str(deploy_dir), capture_output=True, text=True, timeout=60
    )
    if result.returncode == 0:
        print(f"[report] Vercel deploy succeeded", file=sys.stderr)
        return "https://finance-site-psi.vercel.app"
    else:
        print(f"[report] Vercel deploy failed: {result.stderr}", file=sys.stderr)
        # Fallback: local server
        reports_dir = Path("/Users/YOUR_MAC_USERNAME/derek/reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        (reports_dir / filename).write_text(html_content)
        return f"http://100.97.110.23:8282/{filename}"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--csv", required=True, help="Path to Quicken CSV")
    parser.add_argument("--upload", action="store_true", help="Upload to Supabase Storage")
    args = parser.parse_args()

    print("[report] Parsing transactions...", file=sys.stderr)
    txs = parse_transactions(args.csv)
    print(f"[report] Parsed {len(txs)} transactions", file=sys.stderr)

    print("[report] Generating interactive HTML...", file=sys.stderr)
    html = generate_interactive_html(txs)

    now = datetime.now()
    filename = f"finance-report-{now.strftime('%Y-%m-%d')}.html"
    local_path = OUTPUT_DIR / filename
    local_path.write_text(html)
    print(f"[report] Saved: {local_path}", file=sys.stderr)

    if args.upload:
        print("[report] Publishing to report server...", file=sys.stderr)
        url = publish_report(html, filename)
        print(f"[report] URL: {url}", file=sys.stderr)
        print(json.dumps({"local": str(local_path), "url": url}))
    else:
        print(json.dumps({"local": str(local_path)}))


if __name__ == "__main__":
    main()
