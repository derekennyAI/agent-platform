#!/usr/bin/env python3
"""
Claude OAuth callback server for agent subscription setup.
Hosts a web page where users paste their OAuth code after authorizing.
Exchanges the code for a token and saves it.

Usage:
  python3 claude_oauth_server.py --agent vera --port 8285

Flow:
  1. Script generates PKCE values + auth URL
  2. Serves a web page with the auth link + paste form
  3. User clicks link, authorizes on claude.com, copies code
  4. User pastes code into form
  5. Server exchanges code for token via platform.claude.com
  6. Saves token to agent's config directory
"""

import argparse
import base64
import hashlib
import http.server
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
SCOPES = "user:profile user:inference user:sessions:claude_code"

AGENTS_JSON = Path.home() / "derek" / "skills" / "agent-setup" / "agents.json"


def load_agent_config(agent_name):
    with open(AGENTS_JSON) as f:
        agents = json.load(f)["agents"]
    if agent_name not in agents:
        print(f"Unknown agent: {agent_name}. Available: {list(agents.keys())}")
        sys.exit(1)
    return agents[agent_name]


def generate_pkce():
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(32)
    return verifier, challenge, state


def build_auth_url(challenge, state):
    params = urllib.parse.urlencode({
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "code": "true",
    })
    return f"{AUTHORIZE_URL}?{params}"


def exchange_code(code, verifier):
    data = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
    }).encode()

    req = urllib.request.Request(
        TOKEN_URL,
        data=data,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read()), None
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        return None, f"HTTP {e.code}: {body}"


def save_token(agent_config, token_data):
    workspace = Path(agent_config["workspace"])
    config_dir = workspace / ".config" / workspace.name
    config_dir.mkdir(parents=True, exist_ok=True)
    token_path = config_dir / "claude-token.json"
    with open(token_path, "w") as f:
        json.dump(token_data, f, indent=2)
    return token_path


def build_html(auth_url, agent_name, human_name, persona):
    return f"""<!DOCTYPE html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Connect Claude — {persona}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; background: #0a0a0a; color: #e0e0e0; padding: 20px; min-height: 100vh; }}
  .container {{ max-width: 480px; margin: 0 auto; padding-top: 40px; }}
  h1 {{ font-size: 24px; margin-bottom: 8px; color: #fff; }}
  .subtitle {{ color: #888; margin-bottom: 32px; font-size: 14px; }}
  .step {{ background: #1a1a1a; border-radius: 12px; padding: 20px; margin-bottom: 16px; border: 1px solid #333; }}
  .step-num {{ display: inline-block; width: 28px; height: 28px; background: #d4a574; color: #0a0a0a; border-radius: 50%; text-align: center; line-height: 28px; font-weight: 700; font-size: 14px; margin-right: 10px; }}
  .step-title {{ font-weight: 600; font-size: 16px; margin-bottom: 10px; }}
  a.btn {{ display: block; background: #d4a574; color: #0a0a0a; text-decoration: none; padding: 14px 20px; border-radius: 8px; text-align: center; font-weight: 600; font-size: 16px; margin-top: 10px; }}
  a.btn:active {{ background: #c4956a; }}
  input[type="text"] {{ width: 100%; padding: 12px; border-radius: 8px; border: 1px solid #444; background: #111; color: #fff; font-size: 16px; margin-top: 10px; }}
  button {{ display: block; width: 100%; background: #d4a574; color: #0a0a0a; padding: 14px; border: none; border-radius: 8px; font-size: 16px; font-weight: 600; margin-top: 10px; cursor: pointer; }}
  button:active {{ background: #c4956a; }}
  .note {{ color: #888; font-size: 13px; margin-top: 8px; }}
  #result {{ margin-top: 20px; padding: 16px; border-radius: 8px; display: none; }}
  .success {{ background: #1a2e1a; border: 1px solid #2d5a2d; color: #7bc67b; }}
  .error {{ background: #2e1a1a; border: 1px solid #5a2d2d; color: #c67b7b; }}
</style>
</head>
<body>
<div class="container">
  <h1>Connect Claude to {persona}</h1>
  <p class="subtitle">Link your Claude subscription so {persona} runs on your account.</p>

  <div class="step">
    <p class="step-title"><span class="step-num">1</span> Sign in to Claude</p>
    <p>Tap the button below. Sign in with the account you want {persona} to use.</p>
    <a class="btn" href="{auth_url}" target="_blank">Open Claude Sign-In</a>
  </div>

  <div class="step">
    <p class="step-title"><span class="step-num">2</span> Copy the code</p>
    <p>After you authorize, you'll see a code on the page. Copy it.</p>
  </div>

  <div class="step">
    <p class="step-title"><span class="step-num">3</span> Paste it here</p>
    <form id="codeForm" onsubmit="submitCode(event)">
      <input type="text" id="codeInput" placeholder="Paste your code here" autocomplete="off" />
      <button type="submit">Connect</button>
    </form>
    <p class="note">Your code is sent securely and used once to link your account.</p>
  </div>

  <div id="result"></div>
</div>

<script>
async function submitCode(e) {{
  e.preventDefault();
  const code = document.getElementById('codeInput').value.trim().split('#')[0];
  if (!code) return;
  const btn = e.target.querySelector('button');
  btn.textContent = 'Connecting...';
  btn.disabled = true;
  try {{
    const resp = await fetch('/exchange', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{code: code}})
    }});
    const data = await resp.json();
    const el = document.getElementById('result');
    el.style.display = 'block';
    if (data.success) {{
      el.className = 'success';
      el.innerHTML = '&#10003; Connected! {persona} is now running on your Claude subscription. You can close this page.';
    }} else {{
      el.className = 'error';
      el.textContent = 'Error: ' + data.error;
      btn.textContent = 'Connect';
      btn.disabled = false;
    }}
  }} catch(err) {{
    const el = document.getElementById('result');
    el.style.display = 'block';
    el.className = 'error';
    el.textContent = 'Connection error. Try again.';
    btn.textContent = 'Connect';
    btn.disabled = false;
  }}
}}
</script>
</body>
</html>"""


class OAuthHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass  # Suppress default logging

    def do_GET(self):
        if self.path == "/" or self.path.startswith("/?"):
            html = build_html(
                self.server.auth_url,
                self.server.agent_name,
                self.server.human_name,
                self.server.persona,
            )
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(html.encode())
        else:
            self.send_error(404)

    def do_POST(self):
        if self.path == "/exchange":
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(length))
            code = body.get("code", "").strip().split("#")[0]

            if not code:
                self._json_response({"success": False, "error": "No code provided"})
                return

            token_data, error = exchange_code(code, self.server.verifier)
            if error:
                print(f"[ERROR] Token exchange failed: {error}")
                self._json_response({"success": False, "error": error})
                return

            token_path = save_token(self.server.agent_config, token_data)
            print(f"[OK] Token saved to {token_path}")
            self._json_response({"success": True})

            # Signal to stop server
            self.server.exchange_complete = True
            self.server.token_data = token_data
        else:
            self.send_error(404)

    def _json_response(self, data):
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def main():
    parser = argparse.ArgumentParser(description="Claude OAuth server for agent setup")
    parser.add_argument("--agent", required=True, help="Agent name from agents.json")
    parser.add_argument("--port", type=int, default=8285, help="Port to listen on")
    args = parser.parse_args()

    agent_config = load_agent_config(args.agent)
    verifier, challenge, state = generate_pkce()
    auth_url = build_auth_url(challenge, state)

    server = http.server.HTTPServer(("0.0.0.0", args.port), OAuthHandler)
    server.auth_url = auth_url
    server.verifier = verifier
    server.agent_name = args.agent
    server.human_name = agent_config["human"]
    server.persona = agent_config["persona"]
    server.agent_config = agent_config
    server.exchange_complete = False
    server.token_data = None

    print(f"OAuth server for {agent_config['persona']} ({agent_config['human']})")
    print(f"Listening on port {args.port}")
    print(f"Auth URL: {auth_url}")
    print(f"Waiting for user to complete OAuth flow...")

    try:
        while not server.exchange_complete:
            server.handle_request()
    except KeyboardInterrupt:
        pass

    if server.token_data:
        print(f"\nDone! Token saved for {args.agent}.")
    else:
        print("\nServer stopped without completing exchange.")

    server.server_close()


if __name__ == "__main__":
    main()
