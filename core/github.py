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

Both write tools refuse cleanly in the two unready states — not
connected, or connected but not armed — with the instruction for
fixing that state in the outcome itself, the way ARIA's
_github_request refused with "GitHub token not configured or armed".

The READ tools (list / read file / repo info) sit on the other side
of that line: they need only the connected state. Arming is the
write-side trust decision — reads were never the dangerous side in
ARIA's semantics — and until they existed, "review the codebase"
was impossible for her: the port shipped with no read path at all,
so she reported a missing capability as a broken connector.

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


def read_gate(action: str) -> Optional[str]:
    """None when a GitHub READ may proceed, else the refusal outcome.
    Connected is enough: the armed flag gates writes only."""
    if state() == "not_connected":
        return not_connected(action)
    return None


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


# ── Reads (connected only — see the header) ─────────────────────────────────

_READ_CAP = 12000   # chars of a file handed to the model per read


def _read_error(err: str, what: str) -> str:
    """Map a _request error to the honest one-liner a read deserves:
    on a read, a 401 is a dead token, a 404 almost always a wrong
    name, a 403 the rate limit or a permissions wall — say which, in
    this module's voice. The token itself is never in any of these."""
    if "GitHub API error 401" in err:
        return ("GitHub refused the stored token (401) — it may have "
                "expired or been revoked. The user can reconnect by "
                "saying 'set up GitHub' with a fresh token.")
    if "GitHub API error 404" in err:
        return (f"I couldn't find {what} on GitHub (404) — check the "
                f"repository name and the path.")
    if "GitHub API error 403" in err:
        return ("GitHub refused the request (403) — most likely the "
                "API rate limit, or the token cannot see that "
                "repository. A rate limit usually clears by waiting.")
    return err


def list_files(repo: str, path: str = "") -> str:
    r_name = resolve_repo(repo)
    clean = (path or "").strip().strip("/")
    url = f"/repos/{r_name}/contents"
    if clean:
        url += "/" + urllib.parse.quote(clean)
    result, err = _request(url)
    if err:
        what = f"{r_name}/{clean}" if clean else f"the repository {r_name}"
        return outcome.failed("github_list_files",
                              _read_error(err, what),
                              repo=r_name, path=clean)
    if isinstance(result, dict):
        # The path names a file, not a directory.
        return outcome.done("github_list_files", repo=r_name,
                            path=clean) + (
            f"\n{clean} is a file ({result.get('size', 0):,} bytes) "
            f"— use github_read_file to read it.")
    lines = [f"Contents of {r_name}/" + (f"{clean}/" if clean else "")]
    if not result:
        lines.append("(empty directory)")
    for item in result:
        name = item.get("name", "?")
        if item.get("type") == "dir":
            lines.append(f"dir   {name}/")
        else:
            lines.append(
                f"file  {name} ({item.get('size', 0):,} bytes)")
    return outcome.done("github_list_files", repo=r_name, path=clean,
                        entries=len(result)) + "\n" + outcome.untrusted(
                            "github", "\n".join(lines))


def read_file(repo: str, filepath: str) -> str:
    r_name = resolve_repo(repo)
    enc_path = urllib.parse.quote(filepath)
    result, err = _request(f"/repos/{r_name}/contents/{enc_path}")
    if err:
        return outcome.failed(
            "github_read_file",
            _read_error(err, f"{filepath} in {r_name}"),
            repo=r_name, path=filepath)
    if isinstance(result, list):
        return outcome.failed(
            "github_read_file",
            f"{filepath} is a directory — list it with "
            f"github_list_files instead.",
            repo=r_name, path=filepath)
    size = result.get("size", 0)
    if result.get("encoding") != "base64" or not result.get("content"):
        # The contents API serves no body for files over ~1 MB.
        return outcome.done("github_read_file", repo=r_name,
                            path=filepath) + (
            f"\n{filepath} is {size:,} bytes — too large for "
            f"GitHub's contents API to hand over this way, so I "
            f"can't read it here.")
    raw = base64.b64decode(result["content"])
    try:
        text = raw.decode("utf-8")
        if "\x00" in text[:1024]:
            raise UnicodeDecodeError("utf-8", raw[:1], 0, 1, "NUL byte")
    except UnicodeDecodeError:
        return outcome.done("github_read_file", repo=r_name,
                            path=filepath) + (
            f"\n{filepath} is binary ({len(raw):,} bytes), not text "
            f"— I can't read it out.")
    total = len(text)
    head = outcome.done("github_read_file", repo=r_name, path=filepath,
                        chars=total)
    if total > _READ_CAP:
        head += (f"\n[Truncated — showing the first "
                 f"{_READ_CAP:,} of {total:,} characters "
                 f"({size:,} bytes).]")
        text = text[:_READ_CAP]
    return head + "\n" + outcome.untrusted("github", text)


def repo_info(repo: str) -> str:
    r_name = resolve_repo(repo)
    result, err = _request(f"/repos/{r_name}")
    if err:
        return outcome.failed(
            "github_repo_info",
            _read_error(err, f"the repository {r_name}"),
            repo=r_name)
    return outcome.done(
        "github_repo_info", repo=r_name,
        description=(result.get("description") or "")[:200],
        default_branch=result.get("default_branch", ""),
        language=result.get("language") or "",
        updated_at=result.get("updated_at", ""),
        open_issues=result.get("open_issues_count", 0),
        visibility="private" if result.get("private") else "public")
