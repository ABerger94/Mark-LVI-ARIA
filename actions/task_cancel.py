"""
actions/task_cancel.py — stop a background task.

Cancellation is cooperative (core/delegation.py has the why):
a queued task is cancelled in its record and never runs; a
running task is flagged and stops between model turns — a call
already in flight finishes first, because there is no honest
way to un-send it. The answer says which of those happened.
"""

from __future__ import annotations

from core import delegation, outcome


def task_cancel(parameters: dict = None, player=None,
                session_memory=None) -> str:
    raw = (parameters or {}).get("task_id")
    try:
        task_id = int(raw)
    except (TypeError, ValueError):
        return outcome.failed("task_cancel",
                              "a numeric task_id is required")
    ok, message = delegation.cancel(task_id)
    if not ok:
        return outcome.failed("task_cancel", message)
    return outcome.done("task_cancel", task=task_id) + f"\n{message}."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "task_cancel",
    "description": (
        "Cancel a background (delegated) task by id. A queued "
        "task never runs; a running task stops between steps (a "
        "tool call already in flight finishes). Cancelling a "
        "finished or unknown task is reported, not faked."
    ),
    "brief": "Cancel a background task ('task_id').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "The task's id",
            },
        },
        "required": ["task_id"],
    },
    "handler": task_cancel,
}
