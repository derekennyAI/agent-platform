#!/usr/bin/env python3
"""Transcribe audio/video files using OpenAI Whisper API.

Supports voice notes (.ogg, .oga, .opus), audio (.mp3, .m4a, .wav, .flac),
and video (.mp4, .mov, .webm) — video has audio extracted via ffmpeg first.

Usage:
    python3 transcribe.py <file_path>
    python3 transcribe.py <file_path> --json
"""

import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent
import sys as _sys; _sys.path.insert(0, "/Users/YOUR_MAC_USERNAME/derek/skills/admin-mcp")
from vault_client import load_secrets as _load_secrets  # reads from Supabase credential vault

# OpenAI Whisper API
WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
MODEL = "whisper-1"

# File types that need audio extraction via ffmpeg
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi", ".mkv"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".opus", ".mp3", ".m4a", ".wav", ".flac", ".wma", ".aac"}


def get_api_key():
    """Get OpenAI API key from environment or vault."""
    key = os.environ.get("OPENAI_API_KEY", "")
    # Handle malformed env var (e.g. "OPENAI_API_KEY=sk-proj-...")
    if key.startswith("OPENAI_API_KEY="):
        key = key.split("=", 1)[1]
    if key:
        return key
    secrets = _load_secrets()
    key = secrets.get("openai_api_key")
    if key:
        return key
    raise RuntimeError("No OpenAI API key found in env or vault")


def extract_audio(video_path, output_path):
    """Extract audio from video using ffmpeg."""
    result = subprocess.run(
        ["ffmpeg", "-i", str(video_path), "-vn", "-acodec", "libmp3lame",
         "-q:a", "2", "-y", str(output_path)],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {result.stderr}")


def transcribe(file_path, api_key):
    """Send audio file to OpenAI Whisper API and return transcription."""
    file_path = Path(file_path)

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    # If video, extract audio first
    temp_audio = None
    if file_path.suffix.lower() in VIDEO_EXTENSIONS:
        temp_audio = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        temp_audio.close()
        extract_audio(file_path, temp_audio.name)
        file_path = Path(temp_audio.name)

    try:
        # Build multipart form data
        boundary = "----WebKitFormBoundary7MA4YWxkTrZu0gW"
        file_data = file_path.read_bytes()
        filename = file_path.name

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{MODEL}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: application/octet-stream\r\n\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        req = urllib.request.Request(
            WHISPER_URL,
            data=body,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=120) as resp:
            result = json.loads(resp.read().decode())
            return result.get("text", "")
    finally:
        if temp_audio and os.path.exists(temp_audio.name):
            os.unlink(temp_audio.name)


def main():
    if len(sys.argv) < 2:
        print("Usage: transcribe.py <file_path> [--json]", file=sys.stderr)
        sys.exit(1)

    file_path = sys.argv[1]
    json_output = "--json" in sys.argv

    api_key = get_api_key()
    text = transcribe(file_path, api_key)

    if json_output:
        print(json.dumps({"text": text, "file": file_path}))
    else:
        print(text)


if __name__ == "__main__":
    main()
