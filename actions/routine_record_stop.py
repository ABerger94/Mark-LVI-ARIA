"""
actions/routine_record_stop.py — end capture and save the routine.

The saved routine is born UNTRUSTED (core/routines.py): when it replays,
destructive steps stop at the on-screen confirmation gate until the
user trusts it with trust_routine. Arguments recorded with {{param}}
placeholders become the routine's parameters at replay time.
"""

from __future__ import annotations

from core import outcome, routines


def routine_record_stop(parameters: dict = None, response=None,
                        player=None, session_memory=None,
                        speak=None) -> str:
    result = routines.stop_recording()
    if result.startswith("["):
        return outcome.failed("routine_record_stop", result.strip("[]"))
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "routine_record_stop",
    "description": (
        "Stop recording and save the routine with every step captured "
        "since routine_record_start. The saved routine is not trusted: "
        "its destructive steps stop for on-screen confirmation when it "
        "replays, until the user trusts it with trust_routine."
    ),
    "brief": "Stop recording and save the routine.",
    "tier": "extra",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": routine_record_stop,
}
