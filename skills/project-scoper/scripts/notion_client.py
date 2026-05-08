#!/usr/bin/env python3
"""Notion API client using stdlib only (urllib). No pip dependencies.

Provides low-level helpers for creating pages, adding blocks, reading pages,
and reading comments. All other scripts in this skill import from here.
"""

import json
import os
import urllib.request
import urllib.error
from pathlib import Path

API_BASE = "https://api.notion.com/v1"


def _get_api_key():
    """Get Notion API key from environment."""
    key = os.environ.get("NOTION_API_KEY", "")
    if not key:
        raise RuntimeError("NOTION_API_KEY not set in environment")
    return key


def _load_config():
    """Load config.json from references directory."""
    config_path = Path(__file__).resolve().parent.parent / "references" / "config.json"
    with open(config_path) as f:
        return json.load(f)


def _request(method, path, body=None):
    """Make an authenticated Notion API request."""
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {_get_api_key()}",
        "Notion-Version": _load_config()["notion_api_version"],
        "Content-Type": "application/json",
    }
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"Notion API {e.code}: {error_body}")


def get_database_id():
    """Return the project pipeline database ID from config."""
    return _load_config()["notion_database_id"]


def create_page(database_id, properties):
    """Create a new page in a database with the given properties."""
    return _request("POST", "/pages", {
        "parent": {"database_id": database_id},
        "properties": properties,
    })


def append_blocks(page_id, blocks):
    """Append child blocks to a page. Chunks at 100 to respect Notion API limit."""
    for i in range(0, len(blocks), 100):
        batch = blocks[i:i + 100]
        _request("PATCH", f"/blocks/{page_id}/children", {"children": batch})


def append_blocks_with_ids(page_id, blocks):
    """Append blocks and return a flat list of created block objects (parallel to input list)."""
    all_created = []
    for i in range(0, len(blocks), 100):
        batch = blocks[i:i + 100]
        result = _request("PATCH", f"/blocks/{page_id}/children", {"children": batch})
        all_created.extend(result.get("results", []))
    return all_created


def update_block(block_id, update_body):
    """Update an existing block's content."""
    return _request("PATCH", f"/blocks/{block_id}", update_body)


def check_todo(block_id):
    """Mark a to_do block as checked (task complete)."""
    return update_block(block_id, {"to_do": {"checked": True}})


def update_callout(block_id, text, emoji):
    """Update a callout block's text and icon emoji."""
    return update_block(block_id, {
        "callout": {
            "rich_text": [{"text": {"content": text}}],
            "icon": {"emoji": emoji},
        }
    })


def create_child_page(parent_page_id, title):
    """Create a new page as a child of another page (not a database entry)."""
    return _request("POST", "/pages", {
        "parent": {"page_id": parent_page_id},
        "properties": {
            "title": {"title": [{"text": {"content": title}}]},
        },
    })


def get_page(page_id):
    """Get a page's properties."""
    return _request("GET", f"/pages/{page_id}")


def get_blocks(page_id):
    """Get all child blocks of a page (handles pagination)."""
    all_blocks = []
    cursor = None
    while True:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        result = _request("GET", path)
        all_blocks.extend(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return all_blocks


def get_comments(page_id):
    """Get all comments on a page."""
    return _request("GET", f"/comments?block_id={page_id}")


def add_comment(page_id, text):
    """Add a comment to a page."""
    return _request("POST", "/comments", {
        "parent": {"page_id": page_id},
        "rich_text": [{"text": {"content": text}}],
    })


def query_database(database_id, filter_obj=None, sorts=None):
    """Query a database with optional filter and sorts."""
    body = {}
    if filter_obj:
        body["filter"] = filter_obj
    if sorts:
        body["sorts"] = sorts
    return _request("POST", f"/databases/{database_id}/query", body)


def update_page(page_id, properties):
    """Update a page's properties."""
    return _request("PATCH", f"/pages/{page_id}", {
        "properties": properties,
    })


# --- Block builders (convenience) ---

def heading2(text):
    return {"type": "heading_2", "heading_2": {"rich_text": [{"text": {"content": text}}]}}

def paragraph(text):
    return {"type": "paragraph", "paragraph": {"rich_text": [{"text": {"content": text}}]}}

def todo(text, checked=False):
    return {"type": "to_do", "to_do": {"rich_text": [{"text": {"content": text}}], "checked": checked}}

def todo_with_children(text, children, checked=False):
    """Create a to_do block with nested child blocks (details, commands, files, notes)."""
    return {
        "type": "to_do",
        "to_do": {
            "rich_text": [{"text": {"content": text}}],
            "checked": checked,
            "children": children,
        },
    }

def bullet(text):
    return {"type": "bulleted_list_item", "bulleted_list_item": {"rich_text": [{"text": {"content": text}}]}}

def divider():
    return {"type": "divider", "divider": {}}

def callout(text, emoji="👆"):
    return {
        "type": "callout",
        "callout": {
            "rich_text": [{"text": {"content": text}}],
            "icon": {"emoji": emoji},
        },
    }

def code(text, language="mermaid"):
    return {
        "type": "code",
        "code": {
            "rich_text": [{"text": {"content": text}}],
            "language": language,
        },
    }

def heading3(text):
    return {"type": "heading_3", "heading_3": {"rich_text": [{"text": {"content": text}}]}}
