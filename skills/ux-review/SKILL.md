---
name: ux-review
description: Comprehensive UX teardown of a website. Activate when the user shares a URL and asks for a UX review, critique, audit, teardown, or feedback on their site. Fetches the sitemap, visits each page, and produces a detailed analysis referencing specific elements found on the site — not generic advice.
---

# UX Review Skill

Perform a comprehensive, site-specific UX teardown from a URL. Everything must reference what's actually on the site.

## Workflow

### 1. Extract URL from the user's message

The URL is whatever they pasted. Normalize it (add `https://` if missing).

### 2. Discover pages

**Use `web_fetch` — no exec approval needed.** Fetch these URLs in order until you get a page list:

1. `web_fetch("{base}/sitemap.xml")` — parse `<loc>` tags
2. `web_fetch("{base}/sitemap_index.xml")` — if index, fetch first child sitemap
3. `web_fetch("{base}/robots.txt")` — look for `Sitemap:` line, then fetch it
4. `web_fetch("{base}")` — fall back to homepage, extract `<a href>` links on same domain

Do NOT ask the user to paste the sitemap. Fetch it yourself with `web_fetch`.

Cap at **20 pages**. Prioritize: homepage, top-level nav pages, pages with "pricing", "contact", "about", "product", "features".

### 3. Extract UX data from each page

For each page, call `web_fetch("{url}")` and extract:
- `<title>` and `<meta name="description">`
- `<meta name="viewport">` — present or missing?
- `<h1>`, `<h2>`, `<h3>` — list them all
- `<nav>` links — what labels are in the navigation?
- Buttons and action links — what do the CTAs say?
- `<form>` elements — how many fields?
- First visible text — what does a new visitor read first?

Alternatively, if exec is approved, run the extractor script for cleaner output:
```bash
python3 skills/ux-review/scripts/extract_ux.py "{url}"
```

### 4. Write the teardown

Use the framework below. Be **specific** — quote actual headings, CTA text, nav labels. Never write generic UX advice that could apply to any site.

---

## UX Review Framework

### Navigation & Information Architecture
- What's in the nav? Are labels clear or jargon-y?
- Is there a logical hierarchy? Does the nav match what's on the pages?
- Missing pages that users would expect?

### Value Proposition & Above-the-Fold
- What's the H1? Does it communicate what the site does and for whom?
- Is the meta description compelling (matters for search previews)?
- What's the first visible text? Would a new visitor immediately understand the offer?

### CTAs
- What are the primary calls to action? Are they specific ("Start free trial") or vague ("Learn more")?
- Are they prominent enough? Repeated at the right moments?
- Do the CTAs match the page intent?

### Forms & Conversion
- How many fields? More than 5 on a lead form is usually friction.
- Are all fields labeled? (label coverage from the script)
- Is there a clear submission CTA?

### Content Hierarchy
- H1 → H2 → H3 logical? Or flat, wall-of-text pages?
- Are headings descriptive or just labels ("About Us" vs "We help X do Y")?
- Is the reading flow natural top to bottom?

### Trust & Credibility
- Any social proof visible in the text? Testimonials, logos, numbers?
- Is there a clear "who is this for" signal?
- Contact info visible?

### Accessibility & Mobile
- Viewport meta present on every page?
- Images with missing alt text (flagged by script)?
- Are forms properly labeled?

### Performance Signals
- High script count (>10) on page load?

---

## Output Format

Structure the report as:

```
## UX Teardown: [Site Name]
[1-sentence overall impression]

### Pages Reviewed
[list with titles]

### Navigation & IA
[specific findings with quotes]

### Value Proposition
[specific findings]

### CTAs
[specific findings]

### Forms
[specific findings or "no forms found"]

### Content Hierarchy
[specific findings]

### Trust Signals
[specific findings]

### Accessibility & Mobile
[specific findings]

### Top 3 Fixes (highest impact)
1. ...
2. ...
3. ...
```

Keep it honest and direct. Quote the actual text from the site. If something is good, say so. If it's broken, say exactly what and why.
