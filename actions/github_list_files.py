"""
actions/github_list_files.py — list a directory inside a repo (read).

Reads need the connected state only — the armed flag gates writes.
With github_read_file this is the read path that makes "review the
codebase" possible at all: list a directory, then read the files.
`repo` resolves exactly as it does for the write tools.
"""

from __future__ import annotations

from core import github


def github_list_files(parameters: dict = None, player=None,
                      session_memory=None) -> str:
    params = parameters or {}
    refusal = github.read_gate("github_list_files")
    if refusal:
        return refusal
    return github.list_files(params.get("repo") or "",
                             params.get("path") or "")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "github_list_files",
    "description": (
        "List the files and folders at a path inside a GitHub "
        "repository (connect with github_setup; reads do NOT need "
        "the account armed). repo is 'owner/name', a bare repo name, "
        "or empty for the default <username>/ARIA-Agent. path is a "
        "directory inside the repo, empty for the root. Pair with "
        "github_read_file to actually review code."
    ),
    "brief": "List a GitHub repo directory ('repo', 'path').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "repo": {
                "type": "STRING",
                "description": "'owner/name', a bare name, or empty for the default repo",
            },
            "path": {
                "type": "STRING",
                "description": "Directory inside the repo (default: the root)",
            },
        },
        "required": [],
    },
    "handler": github_list_files,
}
