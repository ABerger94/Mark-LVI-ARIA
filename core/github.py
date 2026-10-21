"""
core/github.py — GitHub over the REST API, ARIA's tools ported.

ARIA's two-step trust model, kept exactly:

  1. **Connected** — a personal access token + username are stored
     (config keys github_token / github_username). That says who she
     is on GitHub, nothing more.
  2. **Armed** — a separate flag (github_armed, default OFF) says she
     may create repositories and push files with it. Storing the
     token never arms her; arming is its own deliberate act, by voice
     ("arm GitHub" → github_setup with arm=true) or by hand in the
     config.

Both tools refuse cleanly in the two unready states — not connected,
or connected but not armed — with the instruction for fixing that
state in the outcome itself, the way ARIA's _github_request refused
with "GitHub token not configured or armed".

The HTTP seam (`_urlopen`) is the only network surface; the tests
replace it, so the gate and the request shapes are exercised with no
token and no socket.
"""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from core import outcome
from memory import config_manager

API_BASE = "https://api.github.com"

# Network seam — replaced by tests, never by production code.
_urlopen = urllib.request.urlopen


def state() -> str:
    """"armed" | "disarmed" | "not_connected" — the gate's whole input."""
    if not config_manager.get_github_token():
        return "not_connected"
    return "armed" if config_manager.get_github_armed() else "disarmed"


def not_connected(action: str) -> str:
    return outcome.failed(
        action,
        "GitHub is not connected yet. Tell the user to say 'set up "
        "GitHub' with a personal access token and their GitHub "
        "username, then call github_setup with them.")


def not_armed(action: str) -> str:
    return outcome.failed(
        action,
        "GitHub is connected but not armed, so nothing was done. "
        "Arming lets me create repositories and push files as the "
        "user — to arm it, the user says 'arm GitHub' (call "
        "github_setup with arm=true) or sets github_armed in the "
        "config.")


def gate(action: str) -> Optional[str]:
    """None when a GitHub write may proceed, else the refusal outcome."""
    s = state()
    if s == "armed":
        return None
    return not_connected(action) if s == "not_connected" \
        else not_armed(action)


def _request(path: str, method: str = "GET",
             payload: Optional[dict] = None):
    """(parsed JSON, None) or (None, error string) — ARIA's shape."""
    token = config_manager.get_github_token()
    url = f"{API_BASE}{path}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "ARIA-Agent",
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode("utf-8") if payload else None
    req = urllib.request.Request(url, data=data, headers=headers,
                                 method=method)
    try:
        with _urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")[:300]
        return None, f"GitHub API error {e.code}: {body}"
    except Exception as e:
        return None, f"GitHub request failed: {e}"


def resolve_repo(repo: str) -> str:
    """ARIA defaulted an empty repo to her own; a bare name gains the
    configured username as its owner."""
    repo = (repo or "").strip()
    if not repo:
        return f"{config_manager.get_github_username()}/ARIA-Agent"
    if "/" not in repo:
        return f"{config_manager.get_github_username()}/{repo}"
    return repo


def create_repo(name: str, description: str = "",
                private: bool = True) -> str:
    result, err = _request(
        "/user/repos", "POST",
        {"name": name, "description": description,
         "private": private, "auto_init": True})
    if err:
        return outcome.failed("github_create_repo", err, repo=name)
    return outcome.done("github_create_repo", repo=name,
                        url=result.get("html_url", ""))


def push_file(repo: str, filepath: str, content: str,
              message: str = "") -> str:
    r_name = resolve_repo(repo)
    enc_path = urllib.parse.quote(filepath)
    existing, _ = _request(f"/repos/{r_name}/contents/{enc_path}")
    payload = {
        "message": message or f"ARIA: update {filepath}",
        "content": base64.b64encode(content.encode("utf-8")).decode(),
    }
    if existing and "sha" in existing:
        payload["sha"] = existing["sha"]
    result, err = _request(f"/repos/{r_name}/contents/{enc_path}",
                           "PUT", payload)
    if err:
        return outcome.failed("github_push_file", err,
                              repo=r_name, path=filepath)
    return outcome.done("github_push_file", repo=r_name, path=filepath)
