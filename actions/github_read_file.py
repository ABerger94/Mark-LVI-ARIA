"""
actions/github_read_file.py — read one file out of a repo (read).

Reads need the connected state only — the armed flag gates writes.
The file comes back base64-decoded as text, capped at ~12,000
characters with the total stated when truncated; a binary file is
named as binary, never dumped. `repo` resolves exactly as it does
for the write tools.
"""

from __future__ import annotations

from core import github, outcome


def github_read_file(parameters: dict = None, player=None,
                     session_memory=None) -> str:
    params = parameters or {}
    filepath = (params.get("filepath") or "").strip()
    if not filepath:
        return outcome.failed("github_read_file",
                              "no file path was given")
    refusal = github.read_gate("github_read_file")
    if refusal:
        return refusal
    return github.read_file(params.get("repo") or "", filepath)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "github_read_file",
    "description": (
        "Read one file from a GitHub repository as text (connect "
        "with github_setup; reads do NOT need the account armed). "
        "repo is 'owner/name', a bare repo name, or empty for the "
        "default <username>/ARIA-Agent. Long files are truncated "
        "at ~12,000 characters with the full size stated; binary "
        "files are reported as binary, not dumped. Use "
        "github_list_files first to find paths."
    ),
    "brief": "Read a file from GitHub ('repo', 'filepath').",
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
                "description": "Path inside the repo, e.g. 'core/github.py'",
            },
        },
        "required": ["filepath"],
    },
    "handler": github_read_file,
}
