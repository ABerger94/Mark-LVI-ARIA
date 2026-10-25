"""
actions/task_status.py — read a background task's record back.

The record (memory/delegated_tasks.json, core/delegation.py) is
the only source: a status is what the worker wrote, never a
guess about what it might be doing. With a task_id, the one
record; without, the most recent tasks with their states.
"""

from __future__ import annotations

from core import delegation, outcome


def task_status(parameters: dict = None, player=None,
                session_memory=None) -> str:
    params = parameters or {}
    raw = params.get("task_id")
    if raw in (None, ""):
        return outcome.done("task_status") + "\n" + \
            delegation.render_list(delegation.list_tasks())
    try:
        task_id = int(raw)
    except (TypeError, ValueError):
        return outcome.failed("task_status",
                              f"'{raw}' is not a task id")
    task = delegation.get_task(task_id)
    if task is None:
        return outcome.failed("task_status",
                              f"no background task #{task_id}")
    return outcome.done("task_status", task=task_id,
                        status=task["status"]) + "\n" + \
        delegation.render_task(task)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "task_status",
    "description": (
        "Read a background (delegated) task's record: its status "
        "(queued, running, done, done_unverified, failed, "
        "cancelled, or blocked — at the confirmation gate, "
        "blocked_model, blocked_input, blocked_unknown), its "
        "prompt, its plan and step progress, its last checkpoint, "
        "any blocked reason, its verification status, and its "
        "result when finished. Pass task_id for one task; omit "
        "it to list the most recent tasks."
    ),
    "brief": "Check a background task ('task_id', optional).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "The task's id (omit to list recent tasks)",
            },
        },
        "required": [],
    },
    "handler": task_status,
}
