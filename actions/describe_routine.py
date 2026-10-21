"""
actions/describe_routine.py — read a routine before running it.

The inspection half of the trust system: describe_routine renders the
recorded steps with the exact arguments each will run with, so the user
can see precisely what a routine does — and what its {{parameters}}
are — before running it or trusting it.
"""

from __future__ import annotations

from core import outcome, routines


def describe_routine(parameters: dict = None, response=None, player=None,
                     session_memory=None, speak=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed("describe_routine",
                              "no routine name was given")
    result = routines.describe_routine(name)
    if result.startswith("["):
        return outcome.failed("describe_routine", result.strip("[]"),
                              routine=routines.sanitize_name(name))
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "describe_routine",
    "description": (
        "Show a saved routine's steps in order, with the exact "
        "arguments each step will run with, whether the routine is "
        "trusted, and the {{parameters}} it needs at replay. The way to "
        "inspect a routine before running or trusting it."
    ),
    "brief": "Show a routine's recorded steps and its {{parameters}}.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The routine to describe, e.g. 'morning_setup'",
            },
        },
        "required": ["name"],
    },
    "handler": describe_routine,
}
