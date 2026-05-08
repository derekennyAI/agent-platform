#!/usr/bin/env python3
"""Read the current state of a Notion scope document.

Returns the page properties (status, score, etc.), block content (including
checkbox states), and any comments — so Derek can see what Farlen approved,
changed, or commented on.

Usage:
    python3 read_scope.py --page-id <notion-page-id>
    python3 read_scope.py --title "AI Resume Roaster"
    python3 read_scope.py --status Review
    python3 read_scope.py --list
"""

import argparse
import json
import sys

import notion_client as notion


def extract_text(rich_text_array):
    """Extract plain text from a Notion rich_text array."""
    return "".join(t.get("plain_text", "") for t in rich_text_array)


def parse_blocks(blocks):
    """Parse Notion blocks into a structured summary."""
    sections = []
    current_section = None

    for block in blocks:
        btype = block["type"]

        if btype == "heading_2":
            if current_section:
                sections.append(current_section)
            current_section = {
                "heading": extract_text(block["heading_2"]["rich_text"]),
                "items": [],
            }

        elif btype == "to_do" and current_section:
            current_section["items"].append({
                "type": "checkbox",
                "text": extract_text(block["to_do"]["rich_text"]),
                "checked": block["to_do"]["checked"],
            })

        elif btype == "paragraph" and current_section:
            text = extract_text(block["paragraph"]["rich_text"])
            if text.strip():
                current_section["items"].append({
                    "type": "text",
                    "text": text,
                })

        elif btype == "bulleted_list_item" and current_section:
            current_section["items"].append({
                "type": "bullet",
                "text": extract_text(block["bulleted_list_item"]["rich_text"]),
            })

    if current_section:
        sections.append(current_section)

    return sections


def parse_properties(props):
    """Extract key properties from a Notion page."""
    result = {}
    if "Name" in props and props["Name"]["type"] == "title":
        result["title"] = extract_text(props["Name"]["title"])
    if "Status" in props and props["Status"]["type"] == "select":
        sel = props["Status"]["select"]
        result["status"] = sel["name"] if sel else None
    if "Complexity" in props and props["Complexity"]["type"] == "select":
        sel = props["Complexity"]["select"]
        result["complexity"] = sel["name"] if sel else None
    if "Score" in props and props["Score"]["type"] == "number":
        result["score"] = props["Score"]["number"]
    if "Source Idea" in props and props["Source Idea"]["type"] == "rich_text":
        result["source_idea"] = extract_text(props["Source Idea"]["rich_text"])
    if "Created" in props and props["Created"]["type"] == "date":
        d = props["Created"]["date"]
        result["created"] = d["start"] if d else None
    return result


def read_page(page_id):
    """Read full scope document: properties, blocks, and comments."""
    page = notion.get_page(page_id)
    props = parse_properties(page.get("properties", {}))

    blocks = notion.get_blocks(page_id)
    sections = parse_blocks(blocks)

    comments_resp = notion.get_comments(page_id)
    comments = []
    for c in comments_resp.get("results", []):
        comments.append({
            "author": c.get("created_by", {}).get("name", "unknown"),
            "text": extract_text(c.get("rich_text", [])),
            "created": c.get("created_time", ""),
        })

    return {
        "page_id": page_id,
        "url": page.get("url", ""),
        "properties": props,
        "sections": sections,
        "comments": comments,
    }


def list_projects(status_filter=None):
    """List all projects in the pipeline database."""
    db_id = notion.get_database_id()
    filter_obj = None
    if status_filter:
        filter_obj = {
            "property": "Status",
            "select": {"equals": status_filter},
        }
    result = notion.query_database(
        db_id,
        filter_obj=filter_obj,
        sorts=[{"property": "Created", "direction": "descending"}],
    )
    projects = []
    for page in result.get("results", []):
        props = parse_properties(page.get("properties", {}))
        props["page_id"] = page["id"]
        props["url"] = page.get("url", "")
        projects.append(props)
    return projects


def find_by_title(title):
    """Find a project by title (case-insensitive partial match)."""
    projects = list_projects()
    title_lower = title.lower()
    matches = [p for p in projects if title_lower in p.get("title", "").lower()]
    return matches


def main():
    parser = argparse.ArgumentParser(description="Read Notion scope documents")
    parser.add_argument("--page-id", default=None, help="Notion page ID to read")
    parser.add_argument("--title", default=None, help="Find project by title")
    parser.add_argument("--status", default=None, help="Filter by status")
    parser.add_argument("--list", action="store_true", help="List all projects")
    args = parser.parse_args()

    if args.list or args.status:
        projects = list_projects(args.status)
        print(json.dumps({"count": len(projects), "projects": projects}, indent=2))

    elif args.title:
        matches = find_by_title(args.title)
        if not matches:
            print(json.dumps({"error": f"no project found matching: {args.title}"}))
            sys.exit(1)
        if len(matches) == 1:
            result = read_page(matches[0]["page_id"])
            print(json.dumps(result, indent=2))
        else:
            print(json.dumps({"matches": matches, "count": len(matches)}, indent=2))

    elif args.page_id:
        result = read_page(args.page_id)
        print(json.dumps(result, indent=2))

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
