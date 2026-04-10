#!/usr/bin/env python3
"""
Extract UX-relevant signals from a webpage.
Usage: python3 extract_ux.py <url>
Output: structured plain text for UX analysis
"""
import sys
import subprocess
from html.parser import HTMLParser


class UXExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.title = ""
        self.meta = {}
        self.headings = []  # (level, text)
        self.nav_links = []
        self.ctaish = []  # buttons + action-word links
        self.forms = []
        self.images = {"total": 0, "missing_alt": 0, "empty_alt": 0}
        self.has_viewport = False
        self.scripts = 0
        self.stylesheets = 0
        self.text_chunks = []  # visible text
        self._current_tag = []
        self._in_nav = False
        self._nav_depth = 0
        self._in_skip = False  # script/style/head
        self._skip_depth = 0
        self._in_form = False
        self._form_fields = 0
        self._form_labels = 0
        self._current_heading = None
        self._heading_buf = []
        self._in_title = False
        self._title_buf = []

    CTA_WORDS = {"get", "start", "try", "buy", "sign", "join", "book", "learn",
                 "contact", "request", "demo", "free", "download", "subscribe",
                 "register", "apply", "shop", "explore", "discover", "view"}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self._current_tag.append(tag)

        if tag in ("script", "style", "noscript"):
            self._in_skip = True
            self._skip_depth += 1
            if tag == "script" and not attrs.get("src"):
                pass  # inline script, skip
            elif tag == "script":
                self.scripts += 1
            return

        if tag == "link" and attrs.get("rel") == "stylesheet":
            self.stylesheets += 1

        if tag == "title":
            self._in_title = True
            return

        if tag == "meta":
            name = (attrs.get("name") or attrs.get("property") or "").lower()
            content = attrs.get("content", "")
            if name in ("description", "og:description"):
                self.meta.setdefault("description", content)
            if name in ("og:title",):
                self.meta.setdefault("og:title", content)
            if name == "viewport":
                self.has_viewport = True

        if tag == "nav":
            self._in_nav = True
            self._nav_depth += 1

        if tag in ("h1", "h2", "h3", "h4"):
            self._current_heading = tag
            self._heading_buf = []

        if tag == "form":
            self._in_form = True
            self._form_fields = 0
            self._form_labels = 0

        if tag in ("input", "textarea", "select") and self._in_form:
            if attrs.get("type") not in ("hidden", "submit", "button", "reset"):
                self._form_fields += 1

        if tag == "label" and self._in_form:
            self._form_labels += 1

        if tag == "img":
            self.images["total"] += 1
            alt = attrs.get("alt")
            if alt is None:
                self.images["missing_alt"] += 1
            elif alt.strip() == "":
                self.images["empty_alt"] += 1

        if tag == "button":
            pass  # text captured in handle_data

        if tag == "a" and attrs.get("href"):
            href = attrs["href"]
            if self._in_nav:
                self.nav_links.append({"href": href})

    def handle_endtag(self, tag):
        if self._current_tag and self._current_tag[-1] == tag:
            self._current_tag.pop()

        if tag in ("script", "style", "noscript"):
            self._skip_depth -= 1
            if self._skip_depth <= 0:
                self._in_skip = False
                self._skip_depth = 0
            return

        if tag == "title":
            self.title = "".join(self._title_buf).strip()
            self._in_title = False

        if tag == "nav":
            self._nav_depth -= 1
            if self._nav_depth <= 0:
                self._in_nav = False
                self._nav_depth = 0

        if tag in ("h1", "h2", "h3", "h4") and self._current_heading == tag:
            text = "".join(self._heading_buf).strip()
            if text:
                self.headings.append((tag, text))
            self._current_heading = None
            self._heading_buf = []

        if tag == "form" and self._in_form:
            self.forms.append({
                "fields": self._form_fields,
                "labels": self._form_labels,
            })
            self._in_form = False

    def handle_data(self, data):
        if self._in_title:
            self._title_buf.append(data)
            return
        if self._in_skip:
            return
        text = data.strip()
        if not text:
            return
        if self._current_heading:
            self._heading_buf.append(text)
        if len(self.text_chunks) < 40:
            self.text_chunks.append(text)
        # CTA detection: short text on current button/link
        if self._current_tag and self._current_tag[-1] in ("button", "a"):
            words = set(text.lower().split())
            if words & self.CTA_WORDS:
                self.ctaish.append(text[:80])


def fetch(url):
    if not url.startswith(("http://", "https://")):
        print("Error: only http:// and https:// URLs are supported", file=sys.stderr)
        sys.exit(1)
    result = subprocess.run(
        ["curl", "-sL", "--max-time", "15", "-A",
         "Mozilla/5.0 (compatible; UXReviewer/1.0)",
         url],
        capture_output=True, text=True, errors="replace"
    )
    return result.stdout


def extract(url):
    html = fetch(url)
    parser = UXExtractor()
    parser.feed(html)
    return parser


def report(url):
    p = extract(url)
    lines = []
    lines.append(f"URL: {url}")
    lines.append(f"Title: {p.title or '(none)'}")
    lines.append(f"Meta description: {p.meta.get('description', '(none)')}")
    lines.append(f"Viewport meta: {'yes' if p.has_viewport else 'NO - mobile unfriendly'}")
    lines.append(f"Scripts: {p.scripts} | Stylesheets: {p.stylesheets}")
    lines.append("")

    lines.append("## Heading structure")
    if p.headings:
        for level, text in p.headings[:20]:
            indent = "  " * (int(level[1]) - 1)
            lines.append(f"  {indent}{level.upper()}: {text}")
    else:
        lines.append("  (no headings found)")
    lines.append("")

    lines.append("## Navigation links")
    if p.nav_links:
        for link in p.nav_links[:20]:
            lines.append(f"  {link['href']}")
    else:
        lines.append("  (no <nav> element found)")
    lines.append("")

    lines.append("## CTAs / action elements")
    if p.ctaish:
        seen = set()
        for cta in p.ctaish:
            if cta not in seen:
                lines.append(f"  - {cta}")
                seen.add(cta)
    else:
        lines.append("  (none detected)")
    lines.append("")

    lines.append("## Forms")
    if p.forms:
        for i, f in enumerate(p.forms, 1):
            label_coverage = (f["labels"] / f["fields"] * 100) if f["fields"] else 0
            lines.append(f"  Form {i}: {f['fields']} fields, {f['labels']} labels ({label_coverage:.0f}% labeled)")
    else:
        lines.append("  (no forms found)")
    lines.append("")

    lines.append("## Images")
    lines.append(f"  Total: {p.images['total']}")
    lines.append(f"  Missing alt: {p.images['missing_alt']}")
    lines.append(f"  Empty alt: {p.images['empty_alt']}")
    lines.append("")

    lines.append("## First visible text (value prop / above fold)")
    seen = set()
    count = 0
    for chunk in p.text_chunks:
        if chunk not in seen and len(chunk) > 3:
            lines.append(f"  {chunk[:120]}")
            seen.add(chunk)
            count += 1
            if count >= 10:
                break
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python3 extract_ux.py <url>")
        sys.exit(1)
    print(report(sys.argv[1]))
