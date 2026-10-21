"""
actions/trust_routine.py — let a routine run unattended.

Trusting is the human answering the confirmation gate in advance, for
one named routine: while a trusted routine replays, its destructive
steps run inside confirm.trusted_scope() instead of parking for a
press. The flag is stored on the routine itself (core/routines.py), so
it survives restarts and travels with describe_routine's report — but
never with an export: imported routines land untrusted.
"""

from __future__ import annotations

from core import outcome, routines


def trust_routine(parameters: dict = None, response=None, player=None,
                  session_memory=None, speak=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed("trust_routine",
                              "no routine name was given")
    result = routines.set_trusted(name, True)
    if result.startswith("["):
        return outcome.failed("trust_routine", result.strip("[]"),
                              routine=routines.sanitize_name(name))
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "trust_routine",
    "description": (
        "Mark a saved routine TRUSTED: when it replays, its "
        "destructive steps (sending, deleting, shutting down and the "
        "like) run WITHOUT the on-screen confirmation each would "
        "normally need. Only do this for a routine the user has "
        "inspected with describe_routine and explicitly wants to run "
        "unattended. untrust_routine reverses it."
    ),
    "brief": "Mark a routine trusted — its steps replay without asking.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The routine to trust, e.g. 'morning_setup'",
            },
        },
        "required": ["name"],
    },
    "handler": trust_routine,
}
