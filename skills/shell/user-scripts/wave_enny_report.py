#!/usr/bin/env python3
"""Generate and email weekly Wave P&L report for ennyAI.

Pulls invoice revenue and account balances from Wave, emails an HTML
financial summary to YOUR_ADMIN_EMAIL.

Usage:
    python3 wave_enny_report.py --start 2026-03-10 --end 2026-03-23
    python3 wave_enny_report.py --last-week      # auto Mon–Sun of last week
    python3 wave_enny_report.py --dry-run        # preview HTML, don't send
"""

import argparse
import base64
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error
import urllib.parse
from datetime import date, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE = _SCRIPT_DIR.parent.parent.parent
import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

WAVE_GQL = "https://gql.waveapps.com/graphql/public"
BUSINESS_ID = "QnVzaW5lc3M6NDNkNjU4NGYtMTQ4Mi00ODlhLTg0OGItMTkwNjg2OTdmZmY3"
TAX_RATE = 0.30
REPORT_TO = "YOUR_ADMIN_EMAIL"


def wave_query(query, variables, token):
    payload = json.dumps({"query": query, "variables": variables}).encode()
    req = urllib.request.Request(WAVE_GQL, data=payload, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            result = json.loads(r.read().decode())
    except urllib.error.HTTPError as e:
        print(f"[error] Wave API {e.code}: {e.read().decode()[:300]}", file=sys.stderr)
        return None
    if result.get("errors"):
        print(f"[error] Wave GQL: {result['errors']}", file=sys.stderr)
        return None
    return result.get("data", {})


def get_period_invoices(token, start, end):
    """Get all invoices issued in the period with their payment status."""
    q = """
    query($bid: ID!, $start: Date!, $end: Date!, $page: Int!) {
      business(id: $bid) {
        invoices(page: $page, pageSize: 50, invoiceDateStart: $start, invoiceDateEnd: $end) {
          edges {
            node {
              id invoiceNumber status invoiceDate dueDate
              total { value }
              amountPaid { value }
              amountDue { value }
              customer { name }
            }
          }
        }
      }
    }
    """
    invoices = []
    seen = set()
    page_size = 50
    for page in range(1, 20):
        data = wave_query(q, {"bid": BUSINESS_ID, "start": str(start), "end": str(end), "page": page}, token)
        if not data:
            break
        edges = data["business"]["invoices"]["edges"]
        if not edges:
            break
        new_this_page = 0
        for e in edges:
            n = e["node"]
            if n["id"] in seen:
                continue
            seen.add(n["id"])
            new_this_page += 1
            invoices.append({
                "number": n.get("invoiceNumber", ""),
                "status": n.get("status", ""),
                "date": n.get("invoiceDate", ""),
                "due": n.get("dueDate", ""),
                "total": float(n.get("total", {}).get("value", "0").replace(",", "")),
                "paid": float(n.get("amountPaid", {}).get("value", "0").replace(",", "")),
                "due_amount": float(n.get("amountDue", {}).get("value", "0").replace(",", "")),
                "customer": n.get("customer", {}).get("name", ""),
            })
        # If we got fewer new results than page size, we've hit the end
        if new_this_page == 0 or len(edges) < page_size:
            break
    return invoices


def get_accounts(token):
    """Get all account balances, returning a list of account dicts."""
    q = """
    query($bid: ID!, $page: Int!) {
      business(id: $bid) {
        accounts(page: $page, pageSize: 50) {
          edges {
            node {
              id name normalBalanceType isArchived balance
              type { name value }
              subtype { name value }
            }
          }
        }
      }
    }
    """
    accounts = []
    seen = set()
    for page in range(1, 20):
        data = wave_query(q, {"bid": BUSINESS_ID, "page": page}, token)
        if not data:
            break
        edges = data["business"]["accounts"]["edges"]
        if not edges:
            break
        for e in edges:
            n = e["node"]
            if n["id"] not in seen:
                seen.add(n["id"])
                accounts.append({
                    "id": n["id"],
                    "name": n["name"],
                    "type": n["type"]["name"],
                    "subtype": n["subtype"]["name"],
                    "normal": n["normalBalanceType"],  # CREDIT or DEBIT
                    "archived": n.get("isArchived", False),
                    "balance": float(n.get("balance") or 0),
                })
    return accounts


def get_fun_fact():
    try:
        req = urllib.request.Request("https://uselessfacts.jsph.pl/api/v2/facts/random?language=en")
        req.add_header("User-Agent", "derek-agent/1.0")
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read()).get("text", "")
    except Exception:
        return ""


def fmt_money(amount):
    if amount < 0:
        return f"-${abs(amount):,.2f}"
    return f"${amount:,.2f}"



def _pl_row(label, amount, bold=False, color=None):
    """Render a single P&L table row."""
    if color is None:
        color = "#27ae60" if amount >= 0 else "#e74c3c"
    weight = "700" if bold else "600"
    size = "16px" if bold else "14px"
    return (
        f"<tr><td style='padding:10px 20px;border-bottom:1px solid #e8eaed;font-size:14px;"
        f"{'font-weight:700;' if bold else ''}color:#555'>{label}</td>"
        f"<td style='padding:10px 20px;border-bottom:1px solid #e8eaed;text-align:right;"
        f"font-size:{size};font-weight:{weight};color:{color}'>{fmt_money(amount)}</td></tr>"
    )


def generate_html(start, end, invoices, mtd_invoices, ytd_invoices, accounts, mtd_txns=None, ytd_txns=None, prev_txns=None):
    today = date.today()
    period_label = f"{start.strftime('%b %-d')} – {end.strftime('%b %-d, %Y')}"
    mtd_label = f"{today.strftime('%B')} to Date"

    def invoice_stats(inv_list):
        paid = [i for i in inv_list if i["status"] == "PAID"]
        unpaid = [i for i in inv_list if i["status"] in ("UNPAID", "OVERDUE")]
        revenue = sum(i["total"] for i in paid)
        outstanding = sum(i["due_amount"] for i in unpaid)
        return paid, unpaid, revenue, outstanding

    # Period
    paid_invoices, unpaid_invoices, period_revenue, period_outstanding = invoice_stats(invoices)

    # Non-operating categories to exclude from P&L (tax transfers, owner draws, internal transfers)
    _NON_OPERATING = ("Estimated Tax Liability", "Owner Investment / Drawings", "Transfer to F. MISCHEL")
    # Extended filter for prior-year data which has more internal transfer noise
    _NON_OP_EXTENDED = _NON_OPERATING + (
        "Transfer to BUS COMPLETE CHK", "Transfer to [Old Bank]", "Transfer to CREDIT CARD",
        "Transfer from", "Journal entry", "Office Returns", "Refund for",
    )
    _REVENUE_CATS = ("Income", "Invoice", "Sales", "Service")
    _DRAW_CATS = ("Owner Investment / Drawings",)

    def _is_operating(t):
        cat = t.get("category", "")
        return not any(noc in cat for noc in _NON_OPERATING)

    # Prior year (2025) P&L
    if prev_txns is not None:
        prev_ops_income = [t for t in prev_txns["transactions"]
                          if t["credit"] > 0 and any(k in t.get("category", "") for k in _REVENUE_CATS)]
        prev_ops_expense = [t for t in prev_txns["transactions"]
                           if t["debit"] > 0 and not any(k in t.get("category", "") for k in _NON_OP_EXTENDED)]
        prev_revenue = sum(t["credit"] for t in prev_ops_income)
        prev_expenses = sum(t["debit"] for t in prev_ops_expense)
        prev_profit = prev_revenue - prev_expenses
        prev_draws = sum(t["debit"] for t in prev_txns["transactions"]
                        if any(k in t.get("category", "") for k in _DRAW_CATS))
        prev_taxes_paid = sum(t["debit"] for t in prev_txns["transactions"]
                             if "Estimated Tax Liability" in t.get("category", "") or "Taxes Paid" in t.get("category", ""))
    else:
        prev_revenue = prev_expenses = prev_profit = prev_draws = prev_taxes_paid = 0.0

    # MTD — use transaction data if available, fall back to invoices
    if mtd_txns is not None:
        mtd_ops_income = [t for t in mtd_txns["transactions"]
                          if t["credit"] > 0 and any(k in t.get("category", "") for k in _REVENUE_CATS)]
        mtd_ops_expense = [t for t in mtd_txns["transactions"]
                           if t["debit"] > 0 and _is_operating(t)]
        mtd_revenue = sum(t["credit"] for t in mtd_ops_income)
        mtd_expenses_actual = sum(t["debit"] for t in mtd_ops_expense)
        mtd_net = mtd_revenue - mtd_expenses_actual
        mtd_unpaid = [i for i in mtd_invoices if i["status"] in ("UNPAID", "OVERDUE")]
    else:
        _, mtd_unpaid, mtd_revenue, _ = invoice_stats(mtd_invoices)
        mtd_ops_income = []
        mtd_expenses_actual = 0.0
        mtd_net = mtd_revenue

    # MTD draws
    if mtd_txns is not None:
        mtd_draws = sum(t["debit"] for t in mtd_txns["transactions"]
                        if any(k in t.get("category", "") for k in _DRAW_CATS))
    else:
        mtd_draws = 0.0

    mtd_tax = max(0, mtd_net) * TAX_RATE

    # YTD invoices (for reference)
    ytd_paid, ytd_unpaid, ytd_inv_revenue, ytd_inv_outstanding = invoice_stats(ytd_invoices)

    # --- YTD from transactions (preferred) or account balances ---
    income_accs = [a for a in accounts if a["type"] == "Income" and not a["archived"]]
    expense_accs = [a for a in accounts if a["type"] == "Expenses" and not a["archived"]]
    if ytd_txns is not None:
        ytd_ops_income = [t for t in ytd_txns["transactions"]
                          if t["credit"] > 0 and any(k in t.get("category", "") for k in _REVENUE_CATS)]
        ytd_ops_expense = [t for t in ytd_txns["transactions"]
                           if t["debit"] > 0 and _is_operating(t)]
        ytd_revenue = sum(t["credit"] for t in ytd_ops_income)
        ytd_expenses = sum(t["debit"] for t in ytd_ops_expense)
        # Expense breakdown by category from transactions
        from collections import defaultdict
        _ytd_exp_by_cat = defaultdict(float)
        for t in ytd_ops_expense:
            _ytd_exp_by_cat[t.get("category", "Uncategorized")] += t["debit"]
        ytd_exp_by_cat = sorted(_ytd_exp_by_cat.items(), key=lambda x: -x[1])
    else:
        ytd_ops = []
        ytd_revenue = sum(a["balance"] for a in income_accs if a["balance"] > 0)
        ytd_expenses = sum(a["balance"] for a in expense_accs if a["balance"] > 0)
        ytd_exp_by_cat = []
    ytd_net = ytd_revenue - ytd_expenses
    ytd_tax_should = max(0, ytd_net * TAX_RATE)

    # Key balances
    tax_liability = next(
        (a["balance"] for a in accounts if "Estimated Tax Liability" in a["name"] and not a["archived"]), 0
    )

    # Draw / take-home calculation
    if ytd_txns is not None:
        all_ytd = ytd_txns["transactions"]
        draws_taken = sum(t["debit"] for t in all_ytd if any(x in t.get("category", "") for x in _DRAW_CATS))
        ytd_taxes_paid = sum(t["debit"] for t in all_ytd
                            if "Estimated Tax Liability" in t.get("category", "") or "Taxes Paid" in t.get("category", ""))
    else:
        draws_taken = 0.0
        ytd_taxes_paid = 0.0
    # Use actual current account balance for tax reserved
    tax_reserved_amount = tax_liability

    # Tax estimate: 30% of YTD net profit (income - expenses) only
    # Don't credit taxes already paid to IRS — those already reduced cash
    ytd_tax_obligation = max(0, ytd_net) * TAX_RATE
    ytd_tax_gap = max(0.0, ytd_tax_obligation - tax_reserved_amount)

    # Total unpaid tax obligations (reserved + gap — these must come from checking)
    unpaid_tax_total = tax_reserved_amount + ytd_tax_gap

    # YTD available to draw (profit minus what's spoken for)
    ytd_available = ytd_net - draws_taken - ytd_taxes_paid - tax_reserved_amount - ytd_tax_gap
    # Deduplicate cash accounts by name (Wave sometimes creates duplicate entries)
    _cash_raw = [
        a for a in accounts
        if a["type"] == "Assets" and not a["archived"] and a["balance"] > 0
        and any(k in a["name"] for k in ["CHK", "Checking", "Cash", "Bank", "Savings"])
        and "Transfer" not in a["name"]
    ]
    _cash_by_name = {}
    for a in _cash_raw:
        if a["name"] not in _cash_by_name:
            _cash_by_name[a["name"]] = dict(a)
        else:
            _cash_by_name[a["name"]]["balance"] += a["balance"]
    cash_accounts = list(_cash_by_name.values())
    total_cash = sum(a["balance"] for a in cash_accounts)
    ar_balance = sum(
        a["balance"] for a in accounts
        if a["type"] == "Assets" and "Receivable" in a["name"] and a["balance"] > 0
    )
    # Cash-based safe-to-withdraw: actual bank balance minus unpaid tax obligations
    safe_to_withdraw = total_cash - unpaid_tax_total
    # Helpers
    def inv_rows(inv_list):
        if not inv_list:
            return "<tr><td colspan='4' style='padding:6px 8px;color:#999;font-style:italic;font-size:13px'>None</td></tr>"
        rows = ""
        for i in sorted(inv_list, key=lambda x: x["date"], reverse=True):
            rows += (
                f"<tr>"
                f"<td style='padding:5px 8px;font-size:13px;color:#555'>{i['date']}</td>"
                f"<td style='padding:5px 8px;font-size:13px;color:#333'>{i['customer']}</td>"
                f"<td style='padding:5px 8px;font-size:13px;color:#555'>#{i['number']}</td>"
                f"<td style='padding:5px 8px;text-align:right;font-size:13px;font-weight:600;color:#333'>{fmt_money(i['total'])}</td>"
                f"</tr>"
            )
        return rows

    def summary_card(label, revenue, tax, net, paid_count, outstanding):
        outstanding_html = (
            f"<div style='font-size:12px;color:#e67e22;margin-top:6px'>⚠️ {fmt_money(outstanding)} outstanding</div>"
            if outstanding > 0 else ""
        )
        return f"""
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f7fa;border-radius:8px;margin-bottom:8px">
    <tr>
      <td width="33%" style="padding:16px 20px;border-right:1px solid #e8eaed;vertical-align:top">
        <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Revenue</div>
        <div style="font-size:22px;font-weight:700;color:#2c3e50">{fmt_money(revenue)}</div>
        <div style="font-size:12px;color:#aaa;margin-top:3px">{paid_count} paid invoice{"s" if paid_count != 1 else ""}</div>
        {outstanding_html}
      </td>
      <td width="33%" style="padding:16px 20px;border-right:1px solid #e8eaed;vertical-align:top">
        <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Tax Reserve (30%)</div>
        <div style="font-size:22px;font-weight:700;color:#e67e22">{fmt_money(tax)}</div>
        <div style="font-size:12px;color:#aaa;margin-top:3px">set aside</div>
      </td>
      <td width="33%" style="padding:16px 20px;vertical-align:top">
        <div style="font-size:11px;color:#999;text-transform:uppercase;letter-spacing:.8px;margin-bottom:6px">Take-Home</div>
        <div style="font-size:22px;font-weight:700;color:#27ae60">{fmt_money(net)}</div>
        <div style="font-size:12px;color:#aaa;margin-top:3px">after tax</div>
      </td>
    </tr>
  </table>"""

    # MTD transaction rows HTML
    # MTD income-only transaction rows
    if mtd_ops_income:
        income_ops = sorted(mtd_ops_income, key=lambda x: x["date"], reverse=True)
        def _income_row(t):
            return (
                f"<tr>"
                f"<td style='padding:5px 10px;font-size:12px;color:#888'>{t['date']}</td>"
                f"<td style='padding:5px 10px;font-size:13px;color:#333'>{t['description'][:50]}</td>"
                f"<td style='padding:5px 10px;font-size:12px;color:#aaa'>{t.get('category','')}</td>"
                f"<td style='padding:5px 10px;text-align:right;font-size:13px;font-weight:600;color:#27ae60'>{fmt_money(t['credit'])}</td>"
                f"</tr>"
            )
        mtd_tx_rows_html = (
            "<table class='inv-table' style='margin-bottom:8px'>"
            "<tr style='background:#f8f9fc'>"
            "<td style='padding:5px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#aaa'>Date</td>"
            "<td style='padding:5px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#aaa'>Description</td>"
            "<td style='padding:5px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#aaa'>Category</td>"
            "<td style='padding:5px 10px;font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.8px;color:#aaa;text-align:right'>Amount</td>"
            "</tr>"
            + ("".join(_income_row(t) for t in income_ops) if income_ops else
               "<tr><td colspan='4' style='padding:8px 10px;color:#999;font-style:italic;font-size:13px'>No income this period</td></tr>")
            + "</table>"
        )
    else:
        mtd_tx_rows_html = "<p style='padding:8px 10px;color:#999;font-style:italic;font-size:13px'>No transaction data</p>"

    # YTD expense breakdown from transactions (operating only)
    if ytd_exp_by_cat:
        expense_rows = "".join(
            f"<tr><td style='padding:5px 10px;font-size:13px;color:#555'>{cat}</td>"
            f"<td style='padding:5px 10px;text-align:right;font-size:13px;color:#e74c3c;font-weight:500'>{fmt_money(amt)}</td></tr>"
            for cat, amt in ytd_exp_by_cat[:10]
        )
    else:
        top_expenses = sorted([a for a in expense_accs if a["balance"] > 0], key=lambda x: -x["balance"])[:8]
        expense_rows = "".join(
            f"<tr><td style='padding:5px 10px;font-size:13px;color:#555'>{a['name']}</td>"
            f"<td style='padding:5px 10px;text-align:right;font-size:13px;color:#e74c3c;font-weight:500'>{fmt_money(a['balance'])}</td></tr>"
            for a in top_expenses
        )
    cash_rows = "".join(
        f"<tr><td style='padding:3px 20px;font-size:12px;color:#888'>{a['name']}</td>"
        f"<td style='padding:3px 20px;text-align:right;font-size:12px;color:#888'>{fmt_money(a['balance'])}</td></tr>"
        for a in cash_accounts
    )
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Helvetica, sans-serif; background: #fff; color: #1a1a2e; font-size: 14px; }}
  .page {{ max-width: 760px; margin: 0 auto; padding: 0 0 40px; }}
  .header {{ background: linear-gradient(135deg, #0f3460 0%, #16213e 100%); color: #fff; padding: 32px 40px 28px; }}
  .header-logo {{ font-size: 28px; font-weight: 800; letter-spacing: -0.5px; margin-bottom: 4px; }}
  .header-logo span {{ color: #4ecca3; }}
  .header-subtitle {{ font-size: 14px; opacity: 0.7; }}
  .header-date {{ font-size: 12px; opacity: 0.5; margin-top: 4px; }}
  .section {{ padding: 28px 40px 0; }}
  .section-title {{ font-size: 11px; font-weight: 700; letter-spacing: 1.2px; text-transform: uppercase; color: #888; margin-bottom: 14px; padding-bottom: 8px; border-bottom: 1px solid #eee; }}
  .metric-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 20px; }}
  .metric-card {{ background: #f8f9fc; border-radius: 10px; padding: 18px 20px; border-left: 4px solid #ddd; }}
  .metric-card.green {{ border-left-color: #27ae60; }}
  .metric-card.red {{ border-left-color: #e74c3c; }}
  .metric-card.blue {{ border-left-color: #2980b9; }}
  .metric-card.orange {{ border-left-color: #e67e22; }}
  .metric-label {{ font-size: 11px; font-weight: 600; letter-spacing: .8px; text-transform: uppercase; color: #999; margin-bottom: 6px; }}
  .metric-value {{ font-size: 22px; font-weight: 700; color: #1a1a2e; line-height: 1; }}
  .metric-sub {{ font-size: 11px; color: #aaa; margin-top: 4px; }}
  .inv-table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; }}
  .inv-table td {{ padding: 7px 10px; font-size: 13px; border-bottom: 1px solid #f0f0f0; }}
  .inv-table td:last-child {{ text-align: right; font-weight: 600; }}
  .pl-table {{ width: 100%; border-collapse: collapse; background: #f8f9fc; border-radius: 10px; overflow: hidden; margin-bottom: 20px; }}
  .pl-table td {{ padding: 10px 20px; font-size: 13px; }}
  .pl-table tr:not(:last-child) td {{ border-bottom: 1px solid #eee; }}
  .pl-table .total td {{ font-weight: 700; font-size: 15px; background: #f0f4f8; }}
  .exp-table {{ width: 100%; border-collapse: collapse; margin-bottom: 8px; }}
  .exp-table td {{ padding: 7px 10px; font-size: 13px; border-bottom: 1px solid #f5f5f5; color: #555; }}
  .exp-table td:last-child {{ text-align: right; color: #e74c3c; font-weight: 500; }}
  .balance-grid {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; }}
  .balance-card {{ background: #f8f9fc; border-radius: 10px; padding: 16px 18px; }}
  .balance-label {{ font-size: 11px; font-weight: 600; letter-spacing: .8px; text-transform: uppercase; color: #aaa; margin-bottom: 6px; }}
  .balance-value {{ font-size: 18px; font-weight: 700; color: #1a1a2e; }}
  .balance-sub {{ font-size: 11px; color: #bbb; margin-top: 3px; }}
  .tag-outstanding {{ display: inline-block; background: #fff3e0; color: #e67e22; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 20px; letter-spacing: .5px; vertical-align: middle; margin-left: 6px; }}
  .tag-gap-bad {{ display: inline-block; background: #fde8e8; color: #e74c3c; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 20px; }}
  .tag-gap-good {{ display: inline-block; background: #e8f8f0; color: #27ae60; font-size: 10px; font-weight: 700; padding: 2px 7px; border-radius: 20px; }}
  .footer {{ margin: 32px 40px 0; padding-top: 16px; border-top: 1px solid #eee; font-size: 11px; color: #bbb; }}
  @media print {{ .page {{ padding: 0; }} }}
</style>
</head>
<body>
<div class="page">

  <!-- Header -->
  <div class="header">
    <div class="header-logo">enny<span>AI</span></div>
    <div class="header-subtitle">Financial Report — {period_label}</div>
    <div class="header-date">Generated {today.strftime('%B %-d, %Y')}</div>
  </div>

  <!-- MTD Section -->
  <div class="section">
    <div class="section-title">{mtd_label}</div>
    <div class="metric-grid">
      <div class="metric-card green">
        <div class="metric-label">Income</div>
        <div class="metric-value">{fmt_money(mtd_revenue)}</div>
        <div class="metric-sub">all sources</div>
      </div>
      <div class="metric-card red">
        <div class="metric-label">Expenses</div>
        <div class="metric-value">{fmt_money(mtd_expenses_actual)}</div>
        <div class="metric-sub">operating costs</div>
      </div>
      <div class="metric-card {"blue" if mtd_net >= 0 else "red"}">
        <div class="metric-label">Net</div>
        <div class="metric-value" style="color:{"#27ae60" if mtd_net >= 0 else "#e74c3c"}">{fmt_money(mtd_net)}</div>
        <div class="metric-sub">{"profit" if mtd_net >= 0 else "loss"}</div>
      </div>
    </div>
    {mtd_tx_rows_html}
    <div style="margin-top:12px;padding:12px 16px;background:#fef9f0;border-radius:8px;border-left:3px solid #e67e22">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:13px;font-weight:600;color:#555">Draws Taken This Month</span>
        <span style="font-size:16px;font-weight:700;color:{"#e67e22" if mtd_draws > 0 else "#999"}">{f"− {fmt_money(mtd_draws)}" if mtd_draws > 0 else "$0.00"}</span>
      </div>
    </div>
  </div>

  {"" if prev_txns is None else f'''
  <!-- Prior Year (2025) -->
  <div class="section" style="margin-top:24px">
    <div class="section-title">Prior Year — 2025</div>
    <div class="metric-grid">
      <div class="metric-card green">
        <div class="metric-label">Revenue</div>
        <div class="metric-value">{fmt_money(prev_revenue)}</div>
        <div class="metric-sub">Change Influence + Lean Marketing</div>
      </div>
      <div class="metric-card red">
        <div class="metric-label">Expenses</div>
        <div class="metric-value">{fmt_money(prev_expenses)}</div>
        <div class="metric-sub">operating costs</div>
      </div>
      <div class="metric-card blue">
        <div class="metric-label">Net Profit</div>
        <div class="metric-value" style="color:#27ae60">{fmt_money(prev_profit)}</div>
        <div class="metric-sub">after expenses</div>
      </div>
    </div>
  </div>
  '''}

  <!-- YTD P&L -->
  <div class="section" style="margin-top:24px">
    <div class="section-title">Year to Date — {today.strftime('%Y')}</div>
    <div class="metric-grid">
      <div class="metric-card green">
        <div class="metric-label">Revenue</div>
        <div class="metric-value">{fmt_money(ytd_revenue)}</div>
        <div class="metric-sub">all income sources</div>
      </div>
      <div class="metric-card red">
        <div class="metric-label">Expenses</div>
        <div class="metric-value">{fmt_money(ytd_expenses)}</div>
        <div class="metric-sub">operating costs</div>
      </div>
      <div class="metric-card {"blue" if ytd_net >= 0 else "red"}">
        <div class="metric-label">Net Profit</div>
        <div class="metric-value" style="color:{"#27ae60" if ytd_net >= 0 else "#e74c3c"}">{fmt_money(ytd_net)}</div>
        <div class="metric-sub">{"profit" if ytd_net >= 0 else "loss"}</div>
      </div>
    </div>
  </div>

  <!-- Safe to Withdraw (cash-based) -->
  <div class="section" style="margin-top:24px">
    <div class="section-title">Safe to Withdraw</div>
    <table class="pl-table">
      <tr><td style="color:#555">Cash in Bank</td><td style="text-align:right;color:#2980b9;font-weight:600">{fmt_money(total_cash)}</td></tr>
      <tr><td style="color:#555">Tax Reserved (in Wave)</td><td style="text-align:right;color:#e74c3c">− {fmt_money(tax_reserved_amount)}</td></tr>
      {"" if ytd_tax_gap <= 0 else f"<tr><td style='color:#555'>Additional Tax Owed (30% of net profit)</td><td style='text-align:right;color:#e74c3c'>− {fmt_money(ytd_tax_gap)}</td></tr>"}
      <tr class="total"><td>Safe to Withdraw</td><td style="text-align:right;color:{"#27ae60" if safe_to_withdraw >= 0 else "#e74c3c"};font-size:18px">{fmt_money(safe_to_withdraw)}</td></tr>
    </table>
    <div style="font-size:12px;color:#888;margin-top:6px;padding:0 20px">Cash minus all unpaid tax obligations. This is what you can actually take out today.</div>
  </div>

  <!-- Profit Breakdown (YTD) -->
  <div class="section" style="margin-top:24px">
    <div class="section-title">Profit Breakdown (YTD)</div>
    <table class="pl-table">
      <tr><td style="color:#555">YTD Net Profit</td><td style="text-align:right;color:#27ae60">{fmt_money(ytd_net)}</td></tr>
      <tr><td style="color:#555">Draws Taken</td><td style="text-align:right;color:#e67e22">− {fmt_money(draws_taken)}</td></tr>
      {"" if ytd_taxes_paid <= 0 else f"<tr><td style='color:#555'>Taxes Paid to IRS/CA</td><td style='text-align:right;color:#e74c3c'>− {fmt_money(ytd_taxes_paid)}</td></tr>"}
      <tr><td style="color:#555">Tax Reserved</td><td style="text-align:right;color:#e74c3c">− {fmt_money(tax_reserved_amount)}</td></tr>
      {"" if ytd_tax_gap <= 0 else f"<tr><td style='color:#555'>Additional Tax Owed (30% of net profit)</td><td style='text-align:right;color:#e74c3c'>− {fmt_money(ytd_tax_gap)}</td></tr>"}
      <tr class="total"><td>Untouched Earnings</td><td style="text-align:right;color:{"#27ae60" if ytd_available >= 0 else "#e74c3c"}">{fmt_money(ytd_available)}</td></tr>
    </table>
  </div>

  <!-- Top Expenses -->
  <div class="section" style="margin-top:24px">
    <div class="section-title">Top Expenses (YTD)</div>
    <table class="exp-table">
      {expense_rows}
    </table>
  </div>

  <!-- Current Balances -->
  <div class="section" style="margin-top:24px">
    <div class="section-title">Current Balances</div>
    <div class="balance-grid">
      <div class="balance-card" style="border-top:3px solid #2980b9">
        <div class="balance-label">Cash &amp; Bank</div>
        <div class="balance-value">{fmt_money(total_cash)}</div>
        <div class="balance-sub">{len(cash_accounts)} account{"s" if len(cash_accounts) != 1 else ""}</div>
      </div>
      <div class="balance-card" style="border-top:3px solid #e67e22">
        <div class="balance-label">Accounts Receivable</div>
        <div class="balance-value" style="color:#e67e22">{fmt_money(ar_balance)}</div>
        <div class="balance-sub">{"⚠ no unpaid invoices — may be stale" if ar_balance > 0 and not ytd_unpaid else "pending collection"}</div>
      </div>
      <div class="balance-card" style="border-top:3px solid #8e44ad">
        <div class="balance-label">Tax Reserved</div>
        <div class="balance-value" style="color:#8e44ad">{fmt_money(tax_reserved_amount)}</div>
        <div class="balance-sub">in tax account</div>
      </div>
    </div>
  </div>

  {(lambda f: f'''
  <div class="section" style="margin-top:24px">
    <div style="background:#f0f4f8;border-radius:8px;padding:14px 20px;border-left:4px solid #4ecca3">
      <div style="font-size:10px;font-weight:700;letter-spacing:1px;text-transform:uppercase;color:#888;margin-bottom:6px">Fun Fact of the Day</div>
      <div style="font-size:13px;color:#444;line-height:1.5">{f}</div>
    </div>
  </div>''' if f else "")(get_fun_fact())}

  <div class="footer">
    Generated by Derek · ennyAI · {today.strftime('%B %-d, %Y')}
  </div>

</div>
</body>
</html>"""


def html_to_pdf(html, pdf_path):
    """Convert HTML string to PDF using Playwright's headless Chromium."""
    import os
    env = os.environ.copy()
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(Path.home() / ".cache/ms-playwright")
    # Pass pdf_path via env var to avoid string interpolation in code
    env["_PDF_OUTPUT_PATH"] = str(pdf_path)
    script = (
        "import os,sys;"
        "os.environ['PLAYWRIGHT_BROWSERS_PATH']=os.environ.get('PLAYWRIGHT_BROWSERS_PATH','');"
        "from playwright.sync_api import sync_playwright; html=sys.stdin.read();"
        "p=sync_playwright().start(); b=p.chromium.launch(); pg=b.new_page();"
        "pg.set_content(html,wait_until='networkidle');"
        "pg.pdf(path=os.environ['_PDF_OUTPUT_PATH'],format='A4',"
        "margin={'top':'20px','bottom':'20px','left':'20px','right':'20px'});"
        "b.close(); p.stop()"
    )
    result = subprocess.run(
        ["python3", "-c", script],
        input=html, capture_output=True, text=True, env=env
    )
    if result.returncode != 0:
        print(f"[error] PDF generation failed: {result.stderr[:300]}", file=sys.stderr)
        return False
    return True


def send_telegram_pdf(pdf_path, caption):
    """Send PDF to Farlen via Telegram bot API."""
    import urllib.parse
    secrets = load_secrets()
    # Load bot token — try known channel config locations in priority order
    bot_token = None
    for channel_dir in ["telegram_test", "telegram", "telegram_dereklm"]:
        access_file = Path.home() / ".claude/channels" / channel_dir / "access.json"
        if access_file.exists():
            try:
                data = json.loads(access_file.read_text())
                bot_token = data.get("botToken")
                if bot_token:
                    break
            except Exception:
                pass
    # Legacy .env fallback
    if not bot_token:
        env_file = Path.home() / ".claude/channels/telegram/.env"
        if env_file.exists():
            for line in env_file.read_text().splitlines():
                if line.startswith("TELEGRAM_BOT_TOKEN="):
                    bot_token = line.split("=", 1)[1].strip()
    if not bot_token:
        print("[error] No Telegram bot token found", file=sys.stderr)
        return False

    chat_id = secrets.get("telegram_chat_id", "YOUR_TELEGRAM_CHAT_ID")
    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    boundary = "----FormBoundary7MA4YWxk"
    pdf_bytes = Path(pdf_path).read_bytes()
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="chat_id"\r\n\r\n{chat_id}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="caption"\r\n\r\n{caption}\r\n'
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="document"; filename="ennyai_report.pdf"\r\n'
        f"Content-Type: application/pdf\r\n\r\n"
    ).encode() + pdf_bytes + f"\r\n--{boundary}--\r\n".encode()

    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            resp = json.loads(r.read())
            return resp.get("ok", False)
    except Exception as e:
        print(f"[error] Telegram send failed: {e}", file=sys.stderr)
        return False


_AGENT_NAME = os.environ.get("AGENT_NAME", "derek")
_AGENT_CONFIG = _WORKSPACE / ".config" / _AGENT_NAME
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
    """Load and auto-refresh Gmail access token. Returns (token_data, token_path, creds)."""
    token_data = _load_google_token()
    creds = _load_google_creds()
    token_path = _find_disk_token()
    return token_data, token_path, creds


def _refresh_gmail_token(token_data, token_path, creds):
    """Force-refresh via the shared helper. `creds` ignored (helper resolves
    them as a sibling of token_path). Returns the new access_token string."""
    import sys as _sys
    _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/_lib")
    from google_auth import get_token as _gauth_get_token
    return _gauth_get_token(token_path, force_refresh=True)


def send_email_with_pdf(to, subject, pdf_path):
    """Send email with PDF as attachment and a brief plain-text body."""
    token_data, token_path, creds = _gmail_token()
    access_token = token_data.get("access_token", "")

    def _build_and_send(token):
        msg = MIMEMultipart()
        msg["to"] = to
        msg["from"] = "YOUR_BUSINESS_EMAIL"
        msg["subject"] = subject
        msg.attach(MIMEText("Your ennyAI financial report is attached.", "plain"))
        pdf_bytes = Path(pdf_path).read_bytes()
        part = MIMEApplication(pdf_bytes, Name=Path(pdf_path).name)
        part["Content-Disposition"] = f'attachment; filename="{Path(pdf_path).name}"'
        msg.attach(part)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        payload = json.dumps({"raw": raw}).encode()
        req = urllib.request.Request(
            "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
            data=payload, method="POST"
        )
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req) as r:
                return json.loads(r.read()), token
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return None, token
            raise

    result, _ = _build_and_send(access_token)
    if result is None:
        access_token = _refresh_gmail_token(token_data, token_path, creds)
        result, _ = _build_and_send(access_token)
    return result is not None


def send_report(start, end, html, dry_run=False):
    subject = f"ennyAI Financial Report — {start.strftime('%b %-d')}–{end.strftime('%-d, %Y')}"
    pdf_path = f"/tmp/ennyai_report_{start}_{end}.pdf"

    if dry_run:
        out_path = Path("/tmp/wave_report_preview.html")
        out_path.write_text(html)
        print(f"[dry-run] HTML → {out_path}")
        html_to_pdf(html, pdf_path)
        print(f"[dry-run] PDF  → {pdf_path}")
        return True

    if not html_to_pdf(html, pdf_path):
        print("[error] PDF generation failed", file=sys.stderr)
        return False

    # Email PDF as attachment
    ok = send_email_with_pdf(REPORT_TO, subject, pdf_path)
    if not ok:
        print("[error] email failed", file=sys.stderr)
        return False
    print(f"Sent email: {subject}")

    # Also send PDF to Telegram
    caption = f"📊 ennyAI Report — {start.strftime('%b %-d')}–{end.strftime('%-d, %Y')}"
    tg_ok = send_telegram_pdf(pdf_path, caption)
    print(f"Telegram PDF sent: {tg_ok}")
    return True


def get_transactions(start, end):
    """Run wave_transactions.py as subprocess and return aggregated data."""
    tx_script = _SCRIPT_DIR / "wave_transactions.py"
    result = subprocess.run(
        ["python3", str(tx_script), "--start", str(start), "--end", str(end), "--json"],
        capture_output=True, text=True, timeout=300
    )
    if result.returncode != 0:
        print(f"[warn] wave_transactions failed: {result.stderr[-300:]}", file=sys.stderr)
        return None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                pass
    # Try full stdout
    try:
        return json.loads(result.stdout.strip())
    except Exception:
        print(f"[warn] Could not parse transaction output", file=sys.stderr)
        return None


def last_week_period():
    """Return (start, end) for last Monday–Sunday."""
    today = date.today()
    # Most recent Sunday
    days_since_sunday = (today.weekday() + 1) % 7
    last_sunday = today - timedelta(days=days_since_sunday)
    last_monday = last_sunday - timedelta(days=6)
    return last_monday, last_sunday


def main():
    parser = argparse.ArgumentParser(description="Weekly Wave financial report for ennyAI")
    parser.add_argument("--start", help="Period start (YYYY-MM-DD)")
    parser.add_argument("--end", help="Period end (YYYY-MM-DD)")
    parser.add_argument("--last-week", action="store_true", help="Use last Monday–Sunday")
    parser.add_argument("--mtd", action="store_true", help="Month-to-date (1st of current month through today)")
    parser.add_argument("--dry-run", action="store_true", help="Write HTML to /tmp, don't email")
    args = parser.parse_args()

    secrets = load_secrets()
    token = secrets["wave_access_token"]

    if args.mtd:
        _today = date.today()
        start = _today.replace(day=1)
        end = _today
    elif args.last_week:
        start, end = last_week_period()
    elif args.start and args.end:
        start = date.fromisoformat(args.start)
        end = date.fromisoformat(args.end)
    else:
        parser.print_help()
        sys.exit(1)

    today = date.today()
    # When reporting on a prior month, use end date as reference for MTD/YTD
    ref_date = end if end < today else today
    mtd_start = ref_date.replace(day=1)
    ytd_start = ref_date.replace(month=1, day=1)

    print(f"Fetching Wave data for {start} to {end}...", file=sys.stderr)
    invoices = get_period_invoices(token, start, end)
    mtd_invoices = get_period_invoices(token, mtd_start, ref_date)
    ytd_invoices = get_period_invoices(token, ytd_start, ref_date)
    accounts = get_accounts(token)
    print(f"  period={len(invoices)} mtd={len(mtd_invoices)} ytd={len(ytd_invoices)} accounts={len(accounts)}", file=sys.stderr)

    print(f"Fetching MTD transactions ({mtd_start} to {ref_date})...", file=sys.stderr)
    mtd_txns = get_transactions(mtd_start, ref_date)
    if mtd_txns:
        print(f"  MTD: income={mtd_txns['income']:.2f} expenses={mtd_txns['expenses']:.2f} count={mtd_txns['count']}", file=sys.stderr)
    else:
        print("  MTD transactions unavailable, falling back to invoices", file=sys.stderr)

    print(f"Fetching YTD transactions ({ytd_start} to {ref_date})...", file=sys.stderr)
    ytd_txns = get_transactions(ytd_start, ref_date)
    if ytd_txns:
        print(f"  YTD: income={ytd_txns['income']:.2f} expenses={ytd_txns['expenses']:.2f} count={ytd_txns['count']}", file=sys.stderr)
    else:
        print("  YTD transactions unavailable, falling back to account balances", file=sys.stderr)

    prev_year = ref_date.year - 1
    print(f"Fetching {prev_year} transactions ({prev_year}-01-01 to {prev_year}-12-31)...", file=sys.stderr)
    prev_txns = get_transactions(date(prev_year, 1, 1), date(prev_year, 12, 31))
    if prev_txns:
        print(f"  {prev_year}: count={prev_txns['count']}", file=sys.stderr)
    else:
        print(f"  {prev_year} transactions unavailable", file=sys.stderr)

    html = generate_html(start, end, invoices, mtd_invoices, ytd_invoices, accounts, mtd_txns=mtd_txns, ytd_txns=ytd_txns, prev_txns=prev_txns)
    ok = send_report(start, end, html, dry_run=args.dry_run)
    print(json.dumps({"sent": ok, "period": f"{start}/{end}", "invoices": len(invoices)}))


if __name__ == "__main__":
    main()
