"""
actions/github_create_repo.py — create a repository (gated by arming).

ARIA's tool, behind ARIA's gate: GitHub must be connected AND armed
(core/github.py) before this does anything; both refusal states come
back as their own outcomes with the fix in them.
"""

from __future__ import annotations

from core import github, outcome


def github_create_repo(parameters: dict = None, player=None,
                       session_memory=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed("github_create_repo",
                              "no repository name was given")
    refusal = github.gate("github_create_repo")
    if refusal:
        return refusal
    private = params.get("private", True)
    return github.create_repo(name,
                              description=params.get("description") or "",
                              private=True if private is None
                              else bool(private))


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "github_create_repo",
    "description": (
        "Create a new GitHub repository with the connected account "
        "(connect with github_setup; the account must also be ARMED "
        "or this refuses). Private by default, initialised with a "
        "README (auto_init, as ARIA's was)."
    ),
    "brief": "Create a GitHub repo ('name', 'description', 'private').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "Repository name",
            },
            "description": {
                "type": "STRING",
                "description": "Repository description (default empty)",
            },
            "private": {
                "type": "BOOLEAN",
                "description": "Private repository (default true)",
            },
        },
        "required": ["name"],
    },
    "handler": github_create_repo,
}
