"""
actions/github_repo_info.py — the facts about a repository (read).

Reads need the connected state only — the armed flag gates writes.
Description, default branch, language, last update, open issues,
visibility — the orientation summary to start a codebase review
with. `repo` resolves exactly as it does for the write tools.
"""

from __future__ import annotations

from core import github


def github_repo_info(parameters: dict = None, player=None,
                     session_memory=None) -> str:
    params = parameters or {}
    refusal = github.read_gate("github_repo_info")
    if refusal:
        return refusal
    return github.repo_info(params.get("repo") or "")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "github_repo_info",
    "description": (
        "Get the facts about a GitHub repository: description, "
        "default branch, main language, when it last changed, open "
        "issue count, and whether it is private (connect with "
        "github_setup; reads do NOT need the account armed). repo "
        "is 'owner/name', a bare repo name, or empty for the "
        "default <username>/ARIA-Agent."
    ),
    "brief": "Repository facts from GitHub ('repo').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "repo": {
                "type": "STRING",
                "description": "'owner/name', a bare name, or empty for the default repo",
            },
        },
        "required": [],
    },
    "handler": github_repo_info,
}
