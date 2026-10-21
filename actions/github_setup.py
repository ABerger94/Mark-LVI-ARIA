"""
actions/github_setup.py — connect GitHub, and arm it (ARIA's model).

Two separate acts, because ARIA kept them separate and she was right
to: saving the token says who she is on GitHub; arming says she may
create repositories and push files with it. This action does either
or both — pass github_token + github_username to connect, arm=true to
arm (or arm=false to disarm). A stored value is kept when a call only
changes the other half.
"""

from __future__ import annotations

from core import github, outcome
from memory import config_manager


def github_setup(parameters: dict = None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    token = (params.get("github_token") or "").strip()
    username = (params.get("github_username") or "").strip()
    arm = params.get("arm")

    if token or username:
        config_manager.save_github_credentials(
            token or config_manager.get_github_token(),
            username or config_manager.get_github_username())
    if arm is not None:
        config_manager.save_github_armed(bool(arm))

    s = github.state()
    if s == "not_connected":
        return outcome.failed(
            "github_setup",
            "nothing is stored yet — a personal access token and "
            "the GitHub username are both needed to connect")
    return outcome.done(
        "github_setup",
        username=config_manager.get_github_username(),
        armed="true" if s == "armed" else "false") + (
        "\nGitHub is connected"
        + (" and armed — repositories can be created and files "
           "pushed." if s == "armed" else
           " but NOT armed: creating repos and pushing files will "
           "refuse until the user says 'arm GitHub'."))


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "github_setup",
    "description": (
        "Connect GitHub: save a personal access token and the GitHub "
        "username. Connecting does NOT arm her — pass arm=true (only "
        "when the user explicitly says to arm GitHub) to allow "
        "github_create_repo and github_push_file to write; arm=false "
        "disarms. Values not passed are kept as stored."
    ),
    "brief": "Connect/arm GitHub ('github_token', 'github_username', 'arm').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "github_token": {
                "type": "STRING",
                "description": "GitHub personal access token",
            },
            "github_username": {
                "type": "STRING",
                "description": "GitHub username, e.g. 'ABerger94'",
            },
            "arm": {
                "type": "BOOLEAN",
                "description": "true = allow repo creation and file pushes; false = disarm",
            },
        },
        "required": [],
    },
    "handler": github_setup,
}
