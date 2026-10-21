"""
actions/delete_routine.py — remove a saved routine, behind the gate.

A routine is the user's own recorded work and deleting it cannot be
undone, so the deletion parks at core/confirm.py's on-screen gate —
the same one file deletion and shutdown use, and file_controller's
_delete_gated shape: refuse to stack a second banner, and let
confirm.request's return be the answer. Nothing is deleted until the
user presses CONFIRM; the removal itself is core/routines.delete_routine.
"""

from __future__ import annotations

from core import confirm, outcome, routines


def delete_routine(parameters: dict = None, response=None, player=None,
                   session_memory=None, speak=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed("delete_routine",
                              "no routine name was given")
    sname = routines.sanitize_name(name)
    if not sname:
        return outcome.failed("delete_routine",
                              "that name has no valid characters")
    if not routines.routine_exists(sname):
        return outcome.failed("delete_routine",
                              f"routine '{sname}' not found",
                              routine=sname)
    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen — "
                "answer that one first. Nothing was deleted.")
    return confirm.request(
        key="routine_delete",
        title=f"Delete routine '{sname}'",
        detail=(f"'{sname}' and everything it recorded will be "
                f"removed. This cannot be undone — re-recording it "
                f"means performing the task again."),
        run=lambda: routines.delete_routine(sname),
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "delete_routine",
    "description": (
        "Delete a saved routine by name. This puts a confirmation on "
        "the user's screen and does NOT happen until they press "
        "CONFIRM — a routine is the user's own recorded work and "
        "deleting it cannot be undone. Never claim it is deleted before "
        "the user confirms."
    ),
    "brief": "Delete a saved routine ('name') — asks on screen first.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The routine to delete, e.g. 'morning_setup'",
            },
        },
        "required": ["name"],
    },
    "handler": delete_routine,
}
