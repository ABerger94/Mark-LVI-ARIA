"""
actions/untrust_routine.py — take a routine's unattended pass back.

The reverse of trust_routine: the stored flag flips to untrusted, and
from the next replay on, the routine's destructive steps stop at the
on-screen confirmation gate again (core/routines.py + core/confirm.py).
"""

from __future__ import annotations

from core import outcome, routines


def untrust_routine(parameters: dict = None, response=None, player=None,
                    session_memory=None, speak=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed("untrust_routine",
                              "no routine name was given")
    result = routines.set_trusted(name, False)
    if result.startswith("["):
        return outcome.failed("untrust_routine", result.strip("[]"),
                              routine=routines.sanitize_name(name))
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "untrust_routine",
    "description": (
        "Mark a saved routine UNTRUSTED again: when it replays, its "
        "destructive steps go back to stopping at the on-screen "
        "confirmation gate instead of running unattended. The reverse "
        "of trust_routine."
    ),
    "brief": "Mark a routine untrusted — its destructive steps ask again.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The routine to untrust, e.g. 'morning_setup'",
            },
        },
        "required": ["name"],
    },
    "handler": untrust_routine,
}
