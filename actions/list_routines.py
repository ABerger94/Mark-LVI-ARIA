"""
actions/list_routines.py — what routines exist.

Thin wrapper over core/routines.list_routines: name, step count,
creation date and trust state per routine, read from the saved files
themselves so the list can never describe a routine that is not there.
"""

from __future__ import annotations

from core import routines


def list_routines(parameters: dict = None, response=None, player=None,
                  session_memory=None, speak=None) -> str:
    return routines.list_routines()


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "list_routines",
    "description": (
        "List the saved routines: for each, its name, how many steps it "
        "has, when it was created, and whether it is trusted (trusted "
        "routines replay destructive steps without asking)."
    ),
    "brief": "List saved routines with step counts and trust state.",
    "tier": "extra",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": list_routines,
}
