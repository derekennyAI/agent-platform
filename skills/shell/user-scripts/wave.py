#!/usr/bin/env python3
"""Wave accounting API client — enny.ai invoicing and finances.

Usage:
    python3 wave.py businesses                         # list businesses
    python3 wave.py customers [--business-id ID]      # list customers
    python3 wave.py invoices [--business-id ID] [--status DRAFT|UNPAID|OVERDUE|PAID]
    python3 wave.py invoice <invoice-id>               # get invoice detail
    python3 wave.py transactions [--business-id ID] [--page 1]
    python3 wave.py accounts [--business-id ID]       # chart of accounts
"""

import argparse
import json
import sys
import urllib.request
import urllib.error
from pathlib import Path

import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets  # reads from Supabase credential vault

WAVE_GQL = "https://gql.waveapps.com/graphql/public"


def wave_query(query, variables=None, token=None):
    payload = {"query": query}
    if variables:
        payload["variables"] = variables
    data = json.dumps(payload).encode()
    req = urllib.request.Request(WAVE_GQL, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        detail = e.read().decode()
        print(json.dumps({"error": f"HTTP {e.code}", "detail": detail}), file=sys.stderr)
        sys.exit(1)
    if result.get("errors"):
        print(json.dumps({"errors": result["errors"]}), file=sys.stderr)
        sys.exit(1)
    return result.get("data", {})


def cmd_businesses(args, secrets):
    q = """
    query { businesses(page: 1, pageSize: 20) {
      edges { node { id name isPersonal currency { code } } }
    }}"""
    data = wave_query(q, token=secrets["wave_access_token"])
    nodes = [e["node"] for e in data.get("businesses", {}).get("edges", [])]
    print(json.dumps({"count": len(nodes), "businesses": nodes}, indent=2))


def cmd_customers(args, secrets):
    bid = args.business_id
    q = """
    query($businessId: ID!, $page: Int!) {
      business(id: $businessId) {
        customers(page: $page, pageSize: 50) {
          edges { node { id name email } }
        }
      }
    }"""
    data = wave_query(q, variables={"businessId": bid, "page": 1}, token=secrets["wave_access_token"])
    nodes = [e["node"] for e in data.get("business", {}).get("customers", {}).get("edges", [])]
    print(json.dumps({"count": len(nodes), "customers": nodes}, indent=2))


def cmd_invoices(args, secrets):
    bid = args.business_id
    q = """
    query($businessId: ID!, $page: Int!, $invoiceStatus: InvoiceStatus) {
      business(id: $businessId) {
        invoices(page: $page, pageSize: 50, invoiceStatus: $invoiceStatus) {
          edges { node {
            id invoiceNumber status amountDue { value currency { code } }
            amountPaid { value } customer { name } createdAt dueDate
          }}
        }
      }
    }"""
    variables = {"businessId": bid, "page": 1}
    if args.status:
        variables["invoiceStatus"] = args.status
    data = wave_query(q, variables=variables, token=secrets["wave_access_token"])
    edges = data.get("business", {}).get("invoices", {}).get("edges", [])
    invoices = []
    for e in edges:
        n = e["node"]
        invoices.append({
            "id": n["id"],
            "number": n.get("invoiceNumber"),
            "status": n.get("status"),
            "customer": n.get("customer", {}).get("name"),
            "amount_due": n.get("amountDue", {}).get("value"),
            "amount_paid": n.get("amountPaid", {}).get("value"),
            "currency": n.get("amountDue", {}).get("currency", {}).get("code"),
            "created": n.get("createdAt"),
            "due": n.get("dueDate"),
        })
    print(json.dumps({"count": len(invoices), "invoices": invoices}, indent=2))


def cmd_invoice(args, secrets):
    q = """
    query($invoiceId: ID!) {
      invoice(id: $invoiceId) {
        id invoiceNumber status memo
        amountDue { value currency { code } }
        amountPaid { value }
        customer { name email }
        createdAt dueDate
        items {
          description quantity unitPrice { value }
        }
      }
    }"""
    data = wave_query(q, variables={"invoiceId": args.invoice_id}, token=secrets["wave_access_token"])
    print(json.dumps(data.get("invoice", {}), indent=2))


def cmd_transactions(args, secrets):
    bid = args.business_id
    q = """
    query($businessId: ID!, $page: Int!) {
      business(id: $businessId) {
        transactions(page: $page, pageSize: 50) {
          edges { node {
            id description amount { value currency { code } }
            direction anchor account { name } createdAt
          }}
        }
      }
    }"""
    data = wave_query(q, variables={"businessId": bid, "page": args.page}, token=secrets["wave_access_token"])
    edges = data.get("business", {}).get("transactions", {}).get("edges", [])
    txns = []
    for e in edges:
        n = e["node"]
        txns.append({
            "id": n["id"],
            "description": n.get("description"),
            "amount": n.get("amount", {}).get("value"),
            "direction": n.get("direction"),
            "account": n.get("account", {}).get("name"),
            "date": n.get("anchor") or n.get("createdAt"),
        })
    print(json.dumps({"count": len(txns), "transactions": txns}, indent=2))


def cmd_accounts(args, secrets):
    bid = args.business_id
    q = """
    query($businessId: ID!, $page: Int!) {
      business(id: $businessId) {
        accounts(page: $page, pageSize: 100) {
          edges { node { id name type { name } subtype { name } normalBalanceType } }
        }
      }
    }"""
    data = wave_query(q, variables={"businessId": bid, "page": 1}, token=secrets["wave_access_token"])
    nodes = [e["node"] for e in data.get("business", {}).get("accounts", {}).get("edges", [])]
    print(json.dumps({"count": len(nodes), "accounts": nodes}, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Wave accounting client (enny.ai)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("businesses")

    p_customers = sub.add_parser("customers")
    p_customers.add_argument("--business-id", required=True)

    p_invoices = sub.add_parser("invoices")
    p_invoices.add_argument("--business-id", required=True)
    p_invoices.add_argument("--status", choices=["DRAFT", "UNPAID", "OVERDUE", "PAID"], default=None)

    p_invoice = sub.add_parser("invoice")
    p_invoice.add_argument("invoice_id")

    p_txns = sub.add_parser("transactions")
    p_txns.add_argument("--business-id", required=True)
    p_txns.add_argument("--page", type=int, default=1)

    p_accounts = sub.add_parser("accounts")
    p_accounts.add_argument("--business-id", required=True)

    args = parser.parse_args()
    secrets = load_secrets()

    {
        "businesses": cmd_businesses,
        "customers": cmd_customers,
        "invoices": cmd_invoices,
        "invoice": cmd_invoice,
        "transactions": cmd_transactions,
        "accounts": cmd_accounts,
    }[args.command](args, secrets)


if __name__ == "__main__":
    main()
