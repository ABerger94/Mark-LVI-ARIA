"""
actions/github_push_file.py — create/update a file in a repo (armed).

ARIA's tool: an existing file's sha is fetched first and sent back, so
the same call creates a new file or updates one in place. `repo` may
be "owner/name", a bare "name" (the connected username owns it), or
empty (ARIA's own default, <username>/ARIA-Agent).
"""

from __future__ import annotations

from core import github, outcome


def github_push_file(parameters: dict = None, player=None,
                     session_memory=None) -> str:
    params = parameters or {}
    filepath = (params.get("filepath") or "").strip()
    if not filepath:
        return outcome.failed("github_push_file", "no file path was given")
    if params.get("content") is None:
        return outcome.failed("github_push_file", "no content was given")
    refusal = github.gate("github_push_file")
    if refusal:
        return refusal
    return github.push_file(params.get("repo") or "", filepath,
                            params.get("content"),
                            params.get("message") or "")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "github_push_file",
    "description": (
        "Create or update a file in a GitHub repository (connect "
        "with github_setup; the account must also be ARMED or this "
        "refuses). repo is 'owner/name', a bare repo name, or empty "
        "for the default <username>/ARIA-Agent. content is the full "
        "file text; message is the commit message."
    ),
    "brief": "Push a file to GitHub ('repo', 'filepath', 'content', 'message').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "repo": {
                "type": "STRING",
                "description": "'owner/name', a bare name, or empty for the default repo",
            },
            "filepath": {
                "type": "STRING",
                "description": "Path inside the repo, e.g. 'docs/notes.md'",
            },
            "content": {
                "type": "STRING",
                "description": "The full file content",
            },
            "message": {
                "type": "STRING",
                "description": "Commit message (default 'ARIA: update <filepath>')",
            },
        },
        "required": ["filepath", "content"],
    },
    "handler": github_push_file,
}
