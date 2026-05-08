#!/usr/bin/env python3
"""Scrape Reddit posts via JSON API (primary) or RSS feed (fallback).

JSON API provides scores, comment counts, and search — preferred on residential IPs.
RSS is the fallback if JSON gets blocked.

Usage:
    python3 scrape_reddit.py --subreddit SaaS --sort hot --limit 25
    python3 scrape_reddit.py --subreddit startups --search "I wish there was" --limit 10
    python3 scrape_reddit.py --subreddit SaaS --sort hot --limit 10 --comments 3

Output: JSON array of posts to stdout. Raw data saved to memory/idea-hunter/raw/ if --save is set.
"""

import argparse
import html
import json
import re
import sys
import time
import urllib.request
import urllib.error
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

USER_AGENT = "DerekBot/1.0 (idea-research)"
DELAY = 1.5  # seconds between requests

ATOM_NS = "{http://www.w3.org/2005/Atom}"


def fetch_url(url, retries=2):
    """Fetch raw bytes from a URL with retries."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.read()
        except urllib.error.HTTPError as e:
            if e.code == 429 and attempt < retries:
                print(f"[rate-limited] waiting 10s...", file=sys.stderr)
                time.sleep(10)
                continue
            print(f"[error] HTTP {e.code} fetching {url}", file=sys.stderr)
            return None
        except Exception as e:
            print(f"[error] {e}", file=sys.stderr)
            if attempt < retries:
                time.sleep(3)
                continue
            return None
    return None


def fetch_json(url, retries=2):
    """Fetch and parse JSON from a URL."""
    raw = fetch_url(url, retries)
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        print(f"[error] failed to parse JSON: {e}", file=sys.stderr)
        return None


def strip_html(text):
    """Remove HTML tags and decode entities."""
    text = html.unescape(text)
    text = re.sub(r"<[^>]+>", "", text)
    return text.strip()


# --- RSS scraping ---

def scrape_rss(subreddit, sort="hot", limit=25):
    """Fetch posts from subreddit RSS feed."""
    url = f"https://www.reddit.com/r/{subreddit}/{sort}/.rss?limit={limit}"
    raw = fetch_url(url)
    if not raw:
        return []

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as e:
        print(f"[error] RSS parse failed: {e}", file=sys.stderr)
        return []

    posts = []
    for entry in root.findall(f"{ATOM_NS}entry"):
        title_el = entry.find(f"{ATOM_NS}title")
        link_el = entry.find(f"{ATOM_NS}link")
        content_el = entry.find(f"{ATOM_NS}content")
        author_el = entry.find(f"{ATOM_NS}author/{ATOM_NS}name")
        updated_el = entry.find(f"{ATOM_NS}updated")
        category_el = entry.find(f"{ATOM_NS}category")

        title = title_el.text if title_el is not None and title_el.text else ""
        link = link_el.get("href", "") if link_el is not None else ""
        content = content_el.text if content_el is not None and content_el.text else ""
        author = author_el.text if author_el is not None and author_el.text else "[unknown]"
        # Strip /u/ prefix from author
        if author.startswith("/u/"):
            author = author[3:]

        # Extract selftext from content HTML
        selftext = strip_html(content)[:1000]

        # Parse permalink from link
        permalink = link

        posts.append({
            "subreddit": subreddit,
            "title": title,
            "selftext": selftext,
            "url": link,
            "permalink": permalink,
            "author": author,
            "score": 0,  # RSS doesn't include scores
            "num_comments": 0,  # RSS doesn't include comment counts
            "created_utc": 0,
            "flair": category_el.get("label", "") if category_el is not None else "",
        })

    return posts[:limit]


def search_rss(subreddit, query, limit=25):
    """Search subreddit via RSS search endpoint."""
    encoded_query = urllib.request.quote(query)
    url = f"https://www.reddit.com/r/{subreddit}/search.rss?q={encoded_query}&restrict_sr=on&sort=relevance&t=week&limit={limit}"
    raw = fetch_url(url)
    if not raw:
        # RSS search may also be blocked — fall back to filtering browse results
        print(f"[info] RSS search failed, falling back to browse + filter", file=sys.stderr)
        return search_via_browse(subreddit, query, limit)

    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        print(f"[info] RSS search parse failed, falling back to browse + filter", file=sys.stderr)
        return search_via_browse(subreddit, query, limit)

    entries = root.findall(f"{ATOM_NS}entry")
    if not entries:
        # Empty results or blocked — try browse+filter
        return search_via_browse(subreddit, query, limit)

    posts = []
    for entry in entries:
        title_el = entry.find(f"{ATOM_NS}title")
        link_el = entry.find(f"{ATOM_NS}link")
        content_el = entry.find(f"{ATOM_NS}content")
        author_el = entry.find(f"{ATOM_NS}author/{ATOM_NS}name")

        title = title_el.text if title_el is not None and title_el.text else ""
        link = link_el.get("href", "") if link_el is not None else ""
        content = content_el.text if content_el is not None and content_el.text else ""
        author = author_el.text if author_el is not None and author_el.text else "[unknown]"
        if author.startswith("/u/"):
            author = author[3:]

        selftext = strip_html(content)[:1000]

        posts.append({
            "subreddit": subreddit,
            "title": title,
            "selftext": selftext,
            "url": link,
            "permalink": link,
            "author": author,
            "score": 0,
            "num_comments": 0,
            "created_utc": 0,
            "flair": "",
        })

    return posts[:limit]


def search_via_browse(subreddit, query, limit=25):
    """Fallback: browse subreddit and filter by keyword match."""
    posts = scrape_rss(subreddit, "new", limit=100)
    if not posts:
        posts = scrape_rss(subreddit, "hot", limit=100)

    query_lower = query.lower()
    terms = query_lower.split()
    matched = []
    for post in posts:
        text = (post["title"] + " " + post["selftext"]).lower()
        if any(term in text for term in terms):
            matched.append(post)

    return matched[:limit]


# --- JSON scraping (primary — works from residential IPs) ---

def scrape_json(subreddit, sort="hot", time_filter="week", limit=25):
    """Fetch posts from subreddit JSON endpoint."""
    url = f"https://www.reddit.com/r/{subreddit}/{sort}.json?limit={limit}&t={time_filter}&raw_json=1"
    data = fetch_json(url)
    if not data:
        return None  # Return None to signal fallback needed
    return extract_json_posts(data, subreddit)


def search_json(subreddit, query, sort="relevance", time_filter="week", limit=25):
    """Search subreddit via JSON endpoint."""
    encoded_query = urllib.request.quote(query)
    url = (
        f"https://www.reddit.com/r/{subreddit}/search.json"
        f"?q={encoded_query}&restrict_sr=on&sort={sort}&t={time_filter}"
        f"&limit={limit}&raw_json=1"
    )
    data = fetch_json(url)
    if not data:
        return None
    return extract_json_posts(data, subreddit)


def extract_json_posts(data, subreddit):
    """Extract post data from Reddit JSON response."""
    posts = []
    children = data.get("data", {}).get("children", [])
    for child in children:
        if child.get("kind") != "t3":
            continue
        p = child["data"]
        if p.get("stickied"):
            continue
        posts.append({
            "subreddit": subreddit,
            "title": p.get("title", ""),
            "selftext": (p.get("selftext", "")[:1000]).strip(),
            "url": p.get("url", ""),
            "permalink": f"https://www.reddit.com{p.get('permalink', '')}",
            "author": p.get("author", "[deleted]"),
            "score": p.get("score", 0),
            "num_comments": p.get("num_comments", 0),
            "created_utc": p.get("created_utc", 0),
            "flair": p.get("link_flair_text", ""),
        })
    return posts


def fetch_comments(permalink, limit=5):
    """Fetch top comments for a post via JSON endpoint."""
    # Extract the path portion from permalink URL
    path = permalink
    if path.startswith("https://www.reddit.com"):
        path = path[len("https://www.reddit.com"):]

    url = f"https://www.reddit.com{path}.json?limit={limit}&sort=top"
    time.sleep(DELAY)
    data = fetch_json(url)
    if not data or len(data) < 2:
        return []

    comments = []
    for child in data[1].get("data", {}).get("children", []):
        if child.get("kind") != "t1":
            continue
        c = child["data"]
        comments.append({
            "author": c.get("author", "[deleted]"),
            "body": (c.get("body", "")[:500]).strip(),
            "score": c.get("score", 0),
        })
    return comments[:limit]


def main():
    parser = argparse.ArgumentParser(description="Scrape Reddit for product/SaaS ideas")
    parser.add_argument("--subreddit", "-s", required=True, help="Subreddit name (without r/)")
    parser.add_argument("--search", "-q", default=None, help="Search query within subreddit")
    parser.add_argument("--sort", default="hot", choices=["hot", "new", "top", "relevance"],
                        help="Sort order (default: hot)")
    parser.add_argument("--time", "-t", default="week", choices=["hour", "day", "week", "month", "year", "all"],
                        help="Time filter (default: week)")
    parser.add_argument("--limit", "-l", type=int, default=25, help="Max posts to fetch (default: 25)")
    parser.add_argument("--comments", "-c", type=int, default=0,
                        help="Fetch top N comments per post (default: 0, requires JSON endpoint)")
    parser.add_argument("--save", action="store_true", help="Save raw output to memory/idea-hunter/raw/")
    parser.add_argument("--json-api", action="store_true",
                        help="Force JSON API instead of RSS (may be blocked on datacenter IPs)")
    args = parser.parse_args()

    # Determine workspace root
    script_dir = Path(__file__).resolve().parent
    workspace = script_dir.parent.parent.parent
    raw_dir = workspace / "memory" / "idea-hunter" / "raw"

    print(f"[scrape] r/{args.subreddit}", file=sys.stderr)

    posts = None

    # Try JSON API first (works on residential IPs, has scores + comment counts)
    if args.search:
        print(f'[scrape] search: "{args.search}" (JSON API)', file=sys.stderr)
        posts = search_json(args.subreddit, args.search, args.sort, args.time, args.limit)
    else:
        print(f"[scrape] using JSON API", file=sys.stderr)
        posts = scrape_json(args.subreddit, args.sort, args.time, args.limit)

    if posts is None:
        # Fall back to RSS if JSON is blocked
        print(f"[scrape] JSON blocked, falling back to RSS", file=sys.stderr)
        if args.search:
            posts = search_rss(args.subreddit, args.search, args.limit)
        else:
            posts = scrape_rss(args.subreddit, args.sort, args.limit)

    if posts is None:
        posts = []

    print(f"[scrape] found {len(posts)} posts", file=sys.stderr)

    # Comments require JSON API — warn if it might be blocked
    if args.comments > 0 and posts:
        print(f"[scrape] fetching top {args.comments} comments per post (JSON API, may be blocked)...", file=sys.stderr)
        blocked = False
        for i, post in enumerate(posts):
            if blocked:
                break
            comments = fetch_comments(post["permalink"], args.comments)
            if comments:
                post["top_comments"] = comments
            elif i == 0:
                print("[scrape] comment fetch failed — JSON API likely blocked on this IP", file=sys.stderr)
                blocked = True
            if (i + 1) % 5 == 0:
                print(f"[scrape] comments fetched for {i+1}/{len(posts)} posts", file=sys.stderr)

    if args.save and posts:
        raw_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"{ts}-{args.subreddit}"
        if args.search:
            safe_q = "".join(c if c.isalnum() else "_" for c in args.search[:30])
            filename += f"-{safe_q}"
        filename += ".json"
        out_path = raw_dir / filename
        with open(out_path, "w") as f:
            json.dump(posts, f, indent=2)
        print(f"[scrape] saved to {out_path}", file=sys.stderr)

    json.dump(posts, sys.stdout, indent=2)
    print(file=sys.stdout)


if __name__ == "__main__":
    main()
