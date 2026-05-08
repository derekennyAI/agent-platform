"""Google OAuth helper — refresh-at-use, replaces 6h cron-based refresh.

Public API:
    get_token(token_path, *, force_refresh=False) -> str
        Returns a fresh access_token. Refreshes via Google's refresh_token grant
        if the stored token is missing `expires_at`, expired, or within 60s of
        expiry. Concurrency-safe (flock), atomic write (temp + rename).

    with_retry(token_path, request_fn) -> response
        Calls request_fn(token); on 401, force-refreshes and retries once.

Token file layout (preserved verbatim except for the four mutated keys):
    {
        "access_token":  "<string>",
        "refresh_token": "<string>",
        "expires_in":    3599,
        "expires_at":    <epoch_seconds>,   # NEW — added by this helper
        "scope":         "...",
        "token_type":    "Bearer"
    }

Credentials are loaded from a sibling `google-credentials.json` in the same
directory as the token file. The credentials file is the standard Google
OAuth client JSON, with its `installed` or `web` subobject containing
`client_id` and `client_secret`.
"""

import fcntl
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
EXPIRY_BUFFER_SECONDS = 60       # treat token as expired this many seconds early
LOCK_TIMEOUT_SECONDS = 10        # max wait for the per-token-file flock
NETWORK_TIMEOUT_SECONDS = 15     # Google refresh HTTP timeout


class GoogleAuthError(RuntimeError):
    pass


def get_token(token_path, *, force_refresh: bool = False) -> str:
    """Return a fresh Google access_token for `token_path`.

    Refreshes if expires_at is missing, expired, or within EXPIRY_BUFFER_SECONDS
    of expiry. Uses double-checked locking: a lock-free fast path for the
    common case (token still fresh) so concurrent callers don't serialize
    on the flock, and a locked slow path that re-checks after acquiring the
    lock to handle the case where another process just refreshed.
    """
    token_path = Path(token_path)
    if not token_path.exists():
        raise GoogleAuthError(f"token file not found: {token_path}")

    # Fast path: lock-free read. Atomic-rename writes mean a concurrent
    # writer either gives us the old file in full or the new file in full;
    # never partial. If the read shows fresh, we don't need the lock at all.
    if not force_refresh:
        try:
            cached = json.loads(token_path.read_text())
            if _is_token_fresh(cached):
                return cached["access_token"]
        except (OSError, ValueError):
            pass  # fall through to locked slow path

    # Slow path: someone needs to refresh. Take the lock; re-check inside it
    # so two callers racing into the slow path don't both hit Google.
    lock_path = token_path.with_suffix(token_path.suffix + ".lock")
    with _flock(lock_path):
        token_data = json.loads(token_path.read_text())
        if not force_refresh and _is_token_fresh(token_data):
            return token_data["access_token"]
        creds = _load_credentials(token_path)
        new_token_data = _refresh_token(token_data, creds)
        _atomic_write_json(token_path, new_token_data)
        return new_token_data["access_token"]


def with_retry(token_path, request_fn):
    """Run `request_fn(access_token)` with the current token; on 401 (or
    `urllib.error.HTTPError(code=401)`) force-refresh the token and retry once.

    `request_fn` should accept a single string argument (the access_token) and
    return either:
      - a `urllib` response object (from urlopen), or
      - a `requests.Response` object (if `requests` is installed and used by
        the caller).

    The helper detects 401 by checking `.status` (urllib) or `.status_code`
    (requests). Other HTTP errors propagate to the caller untouched.
    """
    token = get_token(token_path)
    try:
        resp = request_fn(token)
    except urllib.error.HTTPError as e:
        if e.code != 401:
            raise
        token = get_token(token_path, force_refresh=True)
        return request_fn(token)

    status = getattr(resp, "status", None) or getattr(resp, "status_code", None)
    if status == 401:
        token = get_token(token_path, force_refresh=True)
        return request_fn(token)
    return resp


# ─────────────────────────────────────────────────────────────────────────────
# Internals
# ─────────────────────────────────────────────────────────────────────────────

def _is_token_fresh(token_data: dict) -> bool:
    """Token is fresh iff it has a non-stale `expires_at` field.

    Files written by the legacy cron lack `expires_at`; we treat those as not
    fresh so the first call through the helper refreshes once and persists the
    expiry, after which staleness checks are precise.
    """
    expires_at = token_data.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        return False
    return time.time() < (expires_at - EXPIRY_BUFFER_SECONDS)


def _load_credentials(token_path: Path) -> dict:
    """Load `google-credentials.json` from the token file's directory.

    Returns a dict with `client_id` and `client_secret`. Tolerates the standard
    `installed` (desktop) and `web` (server) wrapper keys.
    """
    cred_path = token_path.parent / "google-credentials.json"
    if not cred_path.exists():
        raise GoogleAuthError(
            f"google-credentials.json not found alongside {token_path.name}"
        )
    raw = json.loads(cred_path.read_text())
    creds = raw.get("installed") or raw.get("web") or raw
    if "client_id" not in creds or "client_secret" not in creds:
        raise GoogleAuthError(
            f"missing client_id/client_secret in {cred_path}"
        )
    return creds


def _refresh_token(token_data: dict, creds: dict) -> dict:
    """POST to Google's token endpoint with `grant_type=refresh_token`.

    Mutates a copy of `token_data` in place: updates `access_token`, optionally
    `refresh_token` (if Google rotates it), and persists `expires_at`. All
    other fields (scope, token_type, etc.) are preserved verbatim.
    """
    refresh_token = token_data.get("refresh_token")
    if not refresh_token:
        raise GoogleAuthError("token file has no refresh_token; re-auth required")

    body = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": refresh_token,
        "grant_type": "refresh_token",
    }).encode()

    req = urllib.request.Request(GOOGLE_TOKEN_URL, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=NETWORK_TIMEOUT_SECONDS) as resp:
            new_tokens = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        # Surface the OAuth error body — e.g. invalid_grant on revoked refresh.
        body_text = e.read().decode(errors="replace")
        raise GoogleAuthError(
            f"refresh failed (HTTP {e.code}): {body_text[:300]}"
        ) from e

    if "access_token" not in new_tokens:
        raise GoogleAuthError(f"refresh response missing access_token: {new_tokens}")

    updated = dict(token_data)
    updated["access_token"] = new_tokens["access_token"]
    if "refresh_token" in new_tokens:
        updated["refresh_token"] = new_tokens["refresh_token"]
    expires_in = int(new_tokens.get("expires_in", token_data.get("expires_in", 3599)))
    updated["expires_in"] = expires_in
    updated["expires_at"] = int(time.time()) + expires_in
    return updated


def _atomic_write_json(path: Path, data: dict) -> None:
    """Write `data` to `path` via a temp file in the same dir, then rename.

    Same dir is required so `os.rename` is atomic on POSIX (cross-FS rename
    is not). Uses `indent=2` to match the legacy file formatting.
    """
    tmp_path = path.with_suffix(path.suffix + ".new")
    with tmp_path.open("w") as f:
        json.dump(data, f, indent=2)
    os.chmod(tmp_path, 0o600)
    os.rename(tmp_path, path)


class _flock:
    """Context manager: acquire an exclusive flock on `lock_path`, blocking up
    to LOCK_TIMEOUT_SECONDS. Lock file is created if absent and persists across
    invocations (cheap; never grows)."""

    def __init__(self, lock_path: Path):
        self.lock_path = Path(lock_path)
        self._fd = None

    def __enter__(self):
        self._fd = os.open(str(self.lock_path), os.O_CREAT | os.O_RDWR, 0o600)
        deadline = time.time() + LOCK_TIMEOUT_SECONDS
        while True:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except BlockingIOError:
                if time.time() >= deadline:
                    os.close(self._fd)
                    self._fd = None
                    raise GoogleAuthError(
                        f"timed out waiting for lock on {self.lock_path}"
                    )
                time.sleep(0.05)

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._fd is not None:
            try:
                fcntl.flock(self._fd, fcntl.LOCK_UN)
            finally:
                os.close(self._fd)
                self._fd = None


# ─────────────────────────────────────────────────────────────────────────────
# Self-test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    """Run with: python3 -m google_auth <path/to/google-token.json>

    Exercises:
      - cold call: refresh, persist expires_at
      - warm call: no network, return cached
      - force_refresh: refresh again
      - concurrency: two threads racing on the same token file should both
        succeed with only ONE network refresh.

    No mocks — hits Google for real. Use a token file you don't mind rotating.
    """
    import sys
    import threading

    if len(sys.argv) < 2:
        print("usage: python3 -m google_auth <token_path>", file=sys.stderr)
        sys.exit(2)

    path = Path(sys.argv[1])
    print(f"Test target: {path}")

    print("\n[1] cold call — should refresh and persist expires_at")
    t1 = get_token(path)
    after = json.loads(path.read_text())
    assert "expires_at" in after, "expires_at must be persisted after refresh"
    print(f"    got token (...{t1[-8:]}), expires_at={after['expires_at']}")

    print("\n[2] warm call — should return cached, no refresh")
    t2 = get_token(path)
    after2 = json.loads(path.read_text())
    assert t1 == t2, "warm call must return the same token (no refresh)"
    assert after["expires_at"] == after2["expires_at"], "expires_at unchanged on warm call"
    print(f"    got same token, expires_at unchanged")

    print("\n[3] force_refresh — should refresh again, new expires_at")
    t3 = get_token(path, force_refresh=True)
    after3 = json.loads(path.read_text())
    assert after3["expires_at"] >= after["expires_at"], "force_refresh must update expires_at"
    print(f"    got token (...{t3[-8:]}), new expires_at={after3['expires_at']}")

    print("\n[4] concurrency — two threads racing should both succeed")
    results = []
    errors = []

    def worker():
        try:
            results.append(get_token(path, force_refresh=True))
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"concurrent call errors: {errors}"
    assert len(results) == 2, f"expected 2 results, got {len(results)}"
    print(f"    both threads succeeded; tokens (...{results[0][-8:]}) and (...{results[1][-8:]})")

    print("\nAll self-tests passed.")
