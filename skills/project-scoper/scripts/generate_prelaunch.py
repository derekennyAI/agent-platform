#!/usr/bin/env python3
"""Generate a pre-launch package for an approved scope.

Creates three Notion sub-pages under the scope page:
  - Target Market Analysis
  - Voice & Writing Guide (15 sections)
  - Structured Offer

Usage:
    python3 generate_prelaunch.py --page-id <notion-page-id>
    python3 generate_prelaunch.py --check-pending   # scan Approved scopes, generate for new ones
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_WORKSPACE = _SCRIPT_DIR.parent.parent.parent

sys.path.insert(0, str(_SCRIPT_DIR))
import notion_client as notion

STATE_FILE = _WORKSPACE / "memory" / "prelaunch_state.json"
ANTHROPIC_API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 16000


# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def mark_generated(page_id, package_page_id):
    state = load_state()
    state[page_id] = {"package_page_id": package_page_id}
    save_state(state)


def already_generated(page_id):
    return page_id in load_state()


# ---------------------------------------------------------------------------
# Scope reading
# ---------------------------------------------------------------------------

def read_scope_text(page_id):
    """Return a plain-text summary of the scope page suitable for prompting."""
    page = notion.get_page(page_id)
    props = page.get("properties", {})
    title = ""
    if "Name" in props and props["Name"]["type"] == "title":
        title = "".join(t.get("plain_text", "") for t in props["Name"]["title"])

    blocks = notion.get_blocks(page_id)
    lines = []
    for block in blocks:
        btype = block["type"]
        rt_key = btype
        rt = block.get(rt_key, {}).get("rich_text", [])
        text = "".join(t.get("plain_text", "") for t in rt).strip()
        if not text:
            continue
        if btype == "heading_2":
            lines.append(f"\n## {text}")
        elif btype == "heading_3":
            lines.append(f"\n### {text}")
        elif btype in ("bulleted_list_item", "numbered_list_item"):
            lines.append(f"- {text}")
        elif btype == "to_do":
            checked = block.get("to_do", {}).get("checked", False)
            lines.append(f"[{'x' if checked else ' '}] {text}")
        else:
            lines.append(text)

    return title, "\n".join(lines)


# ---------------------------------------------------------------------------
# Claude API call
# ---------------------------------------------------------------------------

PROMPT_TEMPLATE = """You are a product strategist generating a pre-launch marketing package.

Based on the scope document below, produce three documents separated by the exact markers shown. Be specific and concrete — derive everything from the scope content. No generic filler.

PROJECT: {title}

SCOPE:
{scope}

---

=== TARGET MARKET ===

Write a complete Target Market Analysis with these exact fields:

Company Name: {title}
Offer: [one-sentence description of what the product does]
Audience Type (overall): [B2B / B2C / B2B2C]
Primary Target Market: [concise description — size, sector, core need]
Audience Type: [B2B / B2C]
Industries: [sector breakdown with sub-categories]

Needs – Primary Buying Considerations:
- [bulleted list — specific, operational, concrete]

Demographics:
- Age Range:
- Gender:
- Geography:
- Income Level:
- Profession or Business Size:

Psychographics:
Lifestyle:
- [how they spend their time, what pressures they face]
Values:
- [what they care about]
Pain Points:
- [specific, tangible problems — not abstract]

Buying Behavior:
- [how they research, what influences them, what triggers a switch, what tools they use now]

Decision-Making Roles:
Primary Decision Maker:
- [role/title]
Secondary Decision Influencers:
- [roles]
Support Roles:
- [roles with indirect impact]

Secondary Target Market:
[if applicable, otherwise "None"]

=== VOICE & WRITING GUIDE ===

Write a complete 15-section Voice & Writing Guide. Start with:
Influences: [two writers or thinkers whose style fits this brand] — [one-line description of the blend]

Then write all 15 sections in full:

1) Core identity and context
1.1 Who I Am
1.2 What I Believe (5–7 bulleted convictions)
1.3 Background Highlights (domain expertise signals)
1.4 Personality Cues (4–6 adjectives)
1.5 Constraints and Quirks (formatting rules, style non-negotiables)

2) Audience definition
2.1 Primary Audience
2.2 Secondary Audience
2.3 Needs and Barriers
2.4 Desired Outcomes (think / feel / do)

3) Voice principles
3.1 Always Do (6–8 active rules)
3.2 Never Do (6–8 prohibitions)
3.3 Voice Settings (0–5 scale): Formality, Warmth, Humor, Directness, Intensity, Skepticism

4) Tone by situation
For each: Teaching, Announcement, Sales, Apology, Disagreement, Praise
Format: Goal / Tone / Example line

5) Diction and language choices
5.1 Preferred Vocabulary (power words for this product)
5.2 Words to Avoid
5.3 Jargon Policy
5.4 Swearing and Slang
5.5 Point of View (you/we/I by context)
5.6 Inclusivity and Accessibility (target readability level)

6) Rhetorical moves
6.1 Openers (4–6 named patterns with examples)
6.2 Evidence (what counts — hierarchy of proof)
6.3 Questions vs Directives
6.4 Analogies and Metaphors
6.5 Emphasis

7) Structure and formatting
7.1 Paragraphs
7.2 Sentences
7.3 Lists
7.4 Headings
7.5 Asides
7.6 Links and References
7.7 Visuals

8) Punctuation and grammar rules (Oxford comma, dashes, numbers, quotes, voice, capitalisation)

9) Content types and how to write them
For each type: Educational post, Story post, Sales page, Newsletter, Short social post, FAQ/help doc, Press/announcement, Proposal/scope doc
Format: Goal / Outline / Tone / CTA pattern / Length

10) Story and proof library
10.1 Signature Stories (3–5 named stories with reminders)
10.2 Proof Points (safe buckets)
10.3 Common Patterns You See
10.4 Stories To Avoid

11) Positions and boundaries
11.1 Topics You Take a Stand On
11.2 Topics You Avoid
11.3 Privacy Rules
11.4 Ethical and Legal Lines

12) Editing and QA checklist (10-point pre-publish checklist)

13) Reusable blocks
13.1 Openers Bank (10 examples)
13.2 CTA Bank (10 examples)
13.3 PS Patterns (3 examples)
13.4 Signature Sign-offs (3)

14) Interaction and response policy
Comment/reply style, how to handle disagreement/hostility/negative feedback, escalation rules

15) One-page cheat sheet
Top 5 traits / Do list / Don't list / Tone sliders (0–5) / 3 example openers / 1 default CTA

=== STRUCTURED OFFER ===

Write sections 2–10 of the structured offer (section 1 is the target market, covered above).

2. Dream Outcome
The specific, concrete result the customer achieves — their desired end state, not your features.

3. Before / After
A tight contrast: where they are now vs. where they'll be after using this product.

4. Unique Mechanism
The specific process, method, or system that produces the result. What makes this work in a way no competitor can directly compare.

5. Offer Stack
All components bundled together — core product, bonuses, extras — each with a standalone value so the perceived total exceeds the price.

6. Effort, Time & Risk Reduction
How fast results arrive, how little work the buyer does, what removes their fear of buying. (Maximize outcome × likelihood, minimize time × effort.)

7. Proof & Credibility
What makes the dream outcome believable — case studies, metrics, founder story, early evidence.

8. Objection Map
The 3–5 real objections blocking the sale (cost, complexity, "I've tried this before," trust) — addressed directly.

9. Guarantee / Risk Reversal
Specific, named guarantee (outcome-based, time-based, or anti-guarantee) that transfers risk from buyer to seller.

10. Pricing & Anchoring
What you charge, how it's framed relative to the value stack, and why the number makes sense as a positioning statement.
"""


def call_claude(prompt):
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set")

    payload = {
        "model": MODEL,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }
    data = json.dumps(payload).encode()
    req = urllib.request.Request(ANTHROPIC_API_URL, data=data, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Claude API {e.code}: {e.read().decode()}")
    return result["content"][0]["text"]


def parse_package(raw):
    """Split Claude output into three sections by marker."""
    sections = {"target_market": "", "tov": "", "structured_offer": ""}
    markers = [
        ("=== TARGET MARKET ===", "target_market"),
        ("=== VOICE & WRITING GUIDE ===", "tov"),
        ("=== STRUCTURED OFFER ===", "structured_offer"),
    ]
    parts = {}
    current_key = None
    for line in raw.splitlines():
        matched = False
        for marker, key in markers:
            if marker in line:
                current_key = key
                parts[key] = []
                matched = True
                break
        if not matched and current_key:
            parts[current_key].append(line)

    for key in sections:
        if key in parts:
            sections[key] = "\n".join(parts[key]).strip()
    return sections


# ---------------------------------------------------------------------------
# Notion blocks from text
# ---------------------------------------------------------------------------

def text_to_blocks(text):
    """Convert plain/markdown-ish text to Notion blocks."""
    blocks = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("## "):
            blocks.append(notion.heading2(stripped[3:]))
        elif stripped.startswith("### "):
            blocks.append(notion.heading3(stripped[4:]))
        elif stripped.startswith("- ") or stripped.startswith("• "):
            blocks.append(notion.bullet(stripped[2:]))
        elif stripped.startswith("* "):
            blocks.append(notion.bullet(stripped[2:]))
        else:
            # Chunk long paragraphs — Notion has a 2000 char limit per rich_text
            chunks = [stripped[i:i+1900] for i in range(0, len(stripped), 1900)]
            for chunk in chunks:
                blocks.append(notion.paragraph(chunk))
    return blocks


# ---------------------------------------------------------------------------
# Main generation logic
# ---------------------------------------------------------------------------

def generate_for_scope(page_id):
    """Generate and publish a pre-launch package for a given scope page."""
    print(f"Reading scope {page_id}...", file=sys.stderr)
    title, scope_text = read_scope_text(page_id)
    if not scope_text.strip():
        raise RuntimeError(f"Scope page {page_id} has no readable content")

    print(f"Generating package for: {title}", file=sys.stderr)
    # Escape braces in user content so str.format() doesn't choke on JSON/code in scope docs
    safe_title = (title or "Untitled Project").replace("{", "{{").replace("}", "}}")
    safe_scope = scope_text.replace("{", "{{").replace("}", "}}")
    prompt = PROMPT_TEMPLATE.format(title=safe_title, scope=safe_scope)
    raw = call_claude(prompt)

    package = parse_package(raw)
    if not any(package.values()):
        raise RuntimeError("Claude output did not contain expected markers")

    # Create parent "Pre-Launch Package" page under scope
    print("Creating Notion pages...", file=sys.stderr)
    parent = notion.create_child_page(page_id, "Pre-Launch Package")
    parent_id = parent["id"]
    parent_url = parent.get("url", "")

    # Create sub-pages
    docs = [
        ("Target Market Analysis", package["target_market"]),
        ("Voice & Writing Guide", package["tov"]),
        ("Structured Offer", package["structured_offer"]),
    ]
    for doc_title, doc_text in docs:
        if not doc_text.strip():
            continue
        sub = notion.create_child_page(parent_id, doc_title)
        blocks = text_to_blocks(doc_text)
        if blocks:
            notion.append_blocks(sub["id"], blocks)

    mark_generated(page_id, parent_id)
    return title, parent_url


def get_approved_scopes():
    db_id = notion.get_database_id()
    result = notion.query_database(
        db_id,
        filter_obj={"property": "Status", "select": {"equals": "Approved"}},
        sorts=[{"property": "Created", "direction": "descending"}],
    )
    return result.get("results", [])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Generate pre-launch package for a scope")
    parser.add_argument("--page-id", help="Notion scope page ID")
    parser.add_argument("--check-pending", action="store_true",
                        help="Scan for Approved scopes without packages and generate them")
    args = parser.parse_args()

    if args.page_id:
        if already_generated(args.page_id):
            print(json.dumps({"status": "already_generated", "page_id": args.page_id}))
            return
        title, url = generate_for_scope(args.page_id)
        print(json.dumps({"status": "generated", "title": title, "url": url}))

    elif args.check_pending:
        approved = get_approved_scopes()
        generated = []
        for page in approved:
            pid = page["id"]
            if already_generated(pid):
                continue
            try:
                title, url = generate_for_scope(pid)
                generated.append({"title": title, "url": url})
                print(f"Generated: {title}", file=sys.stderr)
            except Exception as e:
                print(f"Error for {pid}: {e}", file=sys.stderr)
        print(json.dumps({"generated": generated, "count": len(generated)}))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
