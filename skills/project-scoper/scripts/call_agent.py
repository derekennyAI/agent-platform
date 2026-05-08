#!/usr/bin/env python3
"""Thin Anthropic API wrapper for delegating complex coding tasks to a sub-agent.

No pip dependencies — uses stdlib urllib only.

Usage:
    python3 call_agent.py --prompt-file prompts/write-auth.md \
        --context src/lib/supabase.ts src/types/ \
        --output src/lib/auth.ts \
        --model claude-sonnet-4-6
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

API_URL = "https://api.anthropic.com/v1/messages"
DEFAULT_MODEL = "claude-sonnet-4-6"
MAX_TOKENS = 8096


def call_api(prompt, model):
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not key:
        raise RuntimeError("ANTHROPIC_API_KEY not set in environment")

    payload = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "messages": [{"role": "user", "content": prompt}],
    }

    data = json.dumps(payload).encode()
    req = urllib.request.Request(API_URL, data=data, method="POST")
    req.add_header("x-api-key", key)
    req.add_header("anthropic-version", "2023-06-01")
    req.add_header("content-type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        error_body = e.read().decode()
        raise RuntimeError(f"Anthropic API {e.code}: {error_body}")

    return result["content"][0]["text"]


def read_context_path(p):
    """Return list of (filename, content) tuples for a file or directory."""
    path = Path(p)
    if not path.exists():
        print(f"Warning: context path not found: {p}", file=sys.stderr)
        return []
    if path.is_file():
        return [(str(path), path.read_text(errors="replace"))]
    # Directory — read all files recursively, skip unreadable
    entries = []
    for child in sorted(path.rglob("*")):
        if child.is_file():
            try:
                entries.append((str(child), child.read_text()))
            except Exception:
                pass
    return entries


def build_prompt(prompt_text, context_paths):
    parts = [prompt_text]
    for cp in context_paths:
        for filename, content in read_context_path(cp):
            parts.append(f"\n\n=== {filename} ===\n{content}")
    return "\n".join(parts)


def main():
    parser = argparse.ArgumentParser(description="Call Claude API with a prompt and optional context files")
    parser.add_argument("--prompt-file", required=True, help="Path to prompt markdown file")
    parser.add_argument("--context", nargs="*", default=[], help="Context files or directories to append inline")
    parser.add_argument("--output", default="", help="Write response to this file (default: stdout)")
    parser.add_argument("--model", default=DEFAULT_MODEL, help=f"Model ID (default: {DEFAULT_MODEL})")
    args = parser.parse_args()

    prompt_path = Path(args.prompt_file)
    if not prompt_path.exists():
        print(f"Error: prompt file not found: {args.prompt_file}", file=sys.stderr)
        sys.exit(1)

    prompt_text = prompt_path.read_text()
    full_prompt = build_prompt(prompt_text, args.context or [])

    print(f"Calling {args.model} ({len(full_prompt)} chars)...", file=sys.stderr)
    response = call_api(full_prompt, args.model)

    if args.output:
        out_path = Path(args.output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(response)
        print(f"Response written to {args.output}", file=sys.stderr)
    else:
        print(response)


if __name__ == "__main__":
    main()
