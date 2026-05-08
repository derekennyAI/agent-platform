#!/usr/bin/env python3
"""Format idea digest as a clean HTML email. Stdlib only.

Reads ideas.json and generates a polished HTML email body.

Usage:
    python3 format_digest.py --days 1
    python3 format_digest.py --days 7 --top 5
    python3 format_digest.py --days 1 --output /tmp/digest.html

Output: HTML string to stdout (pipe to send_email.py --stdin --html)

Full pipeline:
    python3 format_digest.py --days 1 | python3 send_email.py --to "YOUR_ADMIN_EMAIL" --subject "Daily Idea Digest" --stdin --html
"""

import argparse
import html as html_mod
import json
import sys
import os
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from pathlib import Path


def urllib_quote(s):
    """URL-encode a string for use in mailto links."""
    return urllib.parse.quote(str(s), safe="")


def esc(s):
    """HTML-escape a string for safe embedding in email HTML."""
    return html_mod.escape(str(s)) if s else ""


def get_supabase_config():
    url = os.environ.get("PERSONAL_SUPABASE_URL") or os.environ.get("SUPABASE_URL")
    key = os.environ.get("PERSONAL_SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_KEY")
    if not url or not key:
        raise RuntimeError("Supabase credentials not found (set PERSONAL_SUPABASE_URL/PERSONAL_SUPABASE_KEY or SUPABASE_URL/SUPABASE_SERVICE_KEY)")
    return url.rstrip("/"), key


def load_ideas(days=None, top=None, all_ideas=False):
    url, key = get_supabase_config()
    params = {"order": "total_score.desc", "status": "neq.archived"}
    if not all_ideas and days:
        cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        params["created_at"] = f"gte.{cutoff}"
    if top:
        params["limit"] = top
    qs = urllib.parse.urlencode(params)
    endpoint = f"{url}/rest/v1/ideas?{qs}"
    req = urllib.request.Request(endpoint, headers={
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            return data or []
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Supabase error {e.code}: {e.read().decode()}")


def score_color(score):
    if score >= 7.5:
        return "#22c55e"
    elif score >= 6.0:
        return "#eab308"
    elif score >= 4.5:
        return "#94a3b8"
    else:
        return "#ef4444"


def score_bg(score):
    if score >= 7.5:
        return "#f0fdf4"
    elif score >= 6.0:
        return "#fefce8"
    elif score >= 4.5:
        return "#f8fafc"
    else:
        return "#fef2f2"


def render_score_dots(scores):
    """Render score dimensions as a minimal dot grid."""
    dims = [
        ("D", "demand", "#6366f1"),
        ("S", "simplicity", "#22c55e"),
        ("T", "timing", "#f59e0b"),
        ("M", "moat", "#a855f7"),
        ("F", "founder_fit", "#ec4899"),
    ]
    cells = ""
    for label, key, color in dims:
        val = scores.get(key, 0)
        # Filled circles for score, empty for remainder
        dots = f'<span style="color:{color};">{"●" * val}</span><span style="color:#e2e8f0;">{"●" * (10 - val)}</span>'
        cells += f'''
        <tr>
          <td style="padding:2px 8px 2px 0;font-size:11px;color:#94a3b8;font-weight:500;text-transform:uppercase;letter-spacing:0.5px;white-space:nowrap;">{label}</td>
          <td style="padding:2px 0;font-size:9px;letter-spacing:1px;font-family:monospace;">{dots}</td>
          <td style="padding:2px 0 2px 6px;font-size:12px;color:#64748b;font-weight:600;">{val}</td>
        </tr>'''
    return f'<table cellpadding="0" cellspacing="0" style="margin:0;">{cells}</table>'


def render_idea_card(rank, idea):
    score = idea.get("total_score", 0)
    color = score_color(score)
    bg = score_bg(score)
    tags = idea.get("tags", [])
    scores = idea.get("scores", {})
    evidence = idea.get("evidence", [])

    # Tags as minimal text
    tag_str = " · ".join(esc(t) for t in tags[:4]) if tags else ""
    tag_html = f'<div style="font-size:11px;color:#94a3b8;margin-top:10px;">{tag_str}</div>' if tag_str else ""

    # Evidence quote
    evidence_html = ""
    for e in evidence[:1]:
        quote = esc(e.get("quote", "")[:140])
        ev_url = esc(e.get("url", ""))
        if quote:
            link = f' <a href="{ev_url}" style="color:#6366f1;text-decoration:none;font-size:11px;">&#8599;</a>' if ev_url else ""
            evidence_html = f'''
            <div style="margin-top:12px;padding:10px 14px;background:#f8fafc;border-radius:8px;font-size:12px;color:#64748b;line-height:1.5;">
              &ldquo;{quote}&rdquo;{link}
            </div>'''

    # Score visualization
    score_dots = render_score_dots(scores)

    return f'''
    <div style="margin-bottom:16px;border-radius:12px;overflow:hidden;background:#ffffff;box-shadow:0 1px 3px rgba(0,0,0,0.04);">
      <!-- Score accent bar -->
      <div style="height:3px;background:{color};"></div>
      <div style="padding:20px 24px;">
        <!-- Rank + Title row -->
        <table width="100%" cellpadding="0" cellspacing="0"><tr>
          <td style="vertical-align:top;">
            <div style="font-size:11px;color:#94a3b8;font-weight:600;margin-bottom:4px;">#{rank}</div>
            <div style="font-size:18px;font-weight:700;color:#0f172a;line-height:1.3;">{esc(idea.get("title", "Untitled"))}</div>
          </td>
          <td width="64" align="right" style="vertical-align:top;">
            <div style="width:52px;height:52px;border-radius:50%;background:{bg};border:2px solid {color};display:inline-block;text-align:center;line-height:52px;">
              <span style="font-size:18px;font-weight:800;color:{color};">{score}</span>
            </div>
          </td>
        </tr></table>

        <!-- Problem / Solution -->
        <div style="margin-top:16px;">
          <div style="font-size:13px;color:#0f172a;line-height:1.5;margin-bottom:8px;">
            <span style="font-weight:600;color:#ef4444;">Problem</span><span style="color:#cbd5e1;"> — </span>{esc(idea.get("problem", "N/A"))}
          </div>
          <div style="font-size:13px;color:#0f172a;line-height:1.5;">
            <span style="font-weight:600;color:#22c55e;">Solution</span><span style="color:#cbd5e1;"> — </span>{esc(idea.get("solution", "N/A"))}
          </div>
        </div>

        <!-- Scores + Evidence side by side -->
        <table width="100%" cellpadding="0" cellspacing="0" style="margin-top:14px;"><tr>
          <td style="vertical-align:top;width:180px;">{score_dots}</td>
          <td style="vertical-align:top;">{evidence_html}</td>
        </tr></table>

        {tag_html}

        <!-- Scope button -->
        <div style="margin-top:14px;">
          <a href="mailto:YOUR_BUSINESS_EMAIL?subject=Scope%3A%20{urllib_quote(idea.get('id', ''))}&body=Scope%20this%20idea%3A%20{urllib_quote(idea.get('title', ''))}"
             style="display:inline-block;padding:10px 20px;background:#6366f1;color:#ffffff;font-size:13px;font-weight:600;border-radius:8px;text-decoration:none;letter-spacing:0.3px;">
            Scope this &rarr;
          </a>
        </div>
      </div>
    </div>'''


def render_digest(ideas, days):
    now = datetime.now()
    date_str = now.strftime("%b %d, %Y")
    count = len(ideas)
    avg_score = sum(i.get("total_score", 0) for i in ideas) / count if count else 0
    top_score = max((i.get("total_score", 0) for i in ideas), default=0)

    # Top tags
    all_tags = {}
    for idea in ideas:
        for tag in idea.get("tags", []):
            all_tags[tag] = all_tags.get(tag, 0) + 1
    top_tags = sorted(all_tags.items(), key=lambda x: -x[1])[:6]
    tags_html = " ".join(
        f'<span style="display:inline-block;padding:3px 10px;margin:2px;border-radius:6px;font-size:11px;background:#f1f5f9;color:#475569;">{esc(t)}</span>'
        for t, _ in top_tags
    )

    # Cards
    cards_html = ""
    for rank, idea in enumerate(ideas, 1):
        cards_html += render_idea_card(rank, idea)

    return f'''<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Inter',Roboto,sans-serif;-webkit-font-smoothing:antialiased;">
  <div style="max-width:600px;margin:0 auto;padding:24px 16px;">

    <!-- Header -->
    <div style="padding:32px 24px;margin-bottom:24px;background:#0f172a;border-radius:16px;text-align:center;">
      <div style="font-size:32px;font-weight:800;color:#ffffff;letter-spacing:-0.5px;">Derek's Idea Hunt</div>
      <div style="font-size:13px;color:#64748b;margin-top:8px;">{date_str}</div>
    </div>

    <!-- Stats row -->
    <table width="100%" cellpadding="0" cellspacing="0" style="margin-bottom:24px;">
      <tr>
        <td width="33%" align="center" style="padding:16px 8px;background:#ffffff;border-radius:12px;">
          <div style="font-size:28px;font-weight:800;color:#0f172a;">{count}</div>
          <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Ideas</div>
        </td>
        <td width="8"></td>
        <td width="33%" align="center" style="padding:16px 8px;background:#ffffff;border-radius:12px;">
          <div style="font-size:28px;font-weight:800;color:#6366f1;">{top_score}</div>
          <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Best</div>
        </td>
        <td width="8"></td>
        <td width="33%" align="center" style="padding:16px 8px;background:#ffffff;border-radius:12px;">
          <div style="font-size:28px;font-weight:800;color:#0f172a;">{avg_score:.1f}</div>
          <div style="font-size:10px;color:#94a3b8;text-transform:uppercase;letter-spacing:1px;margin-top:2px;">Average</div>
        </td>
      </tr>
    </table>

    <!-- Tags -->
    <div style="margin-bottom:24px;text-align:center;">{tags_html}</div>

    <!-- Divider -->
    <div style="height:1px;background:#e2e8f0;margin-bottom:24px;"></div>

    <!-- Ideas -->
    {cards_html}

    <!-- Footer -->
    <div style="text-align:center;padding:24px 0 8px;font-size:11px;color:#94a3b8;line-height:1.6;">
      Derek's Idea Hunt · enny.ai<br>
      <span style="color:#cbd5e1;">Reply to explore an idea further</span>
    </div>

  </div>
</body>
</html>'''


def main():
    parser = argparse.ArgumentParser(description="Format idea digest as HTML email")
    parser.add_argument("--days", type=int, default=1, help="Look back N days (default: 1)")
    parser.add_argument("--top", type=int, default=None, help="Limit to top N ideas")
    parser.add_argument("--output", "-o", default=None, help="Write to file instead of stdout")
    parser.add_argument("--all", action="store_true", help="Include all non-archived ideas regardless of date")
    args = parser.parse_args()

    ideas = load_ideas(
        days=args.days if not args.all else None,
        top=args.top,
        all_ideas=args.all,
    )

    if not ideas:
        print("<!-- No ideas found -->", file=sys.stderr)
        print("<p>No new ideas found.</p>")
        sys.exit(0)

    html = render_digest(ideas, args.days)

    if args.output:
        Path(args.output).write_text(html)
        print(f"[format] wrote {args.output}", file=sys.stderr)
    else:
        print(html)


if __name__ == "__main__":
    main()
