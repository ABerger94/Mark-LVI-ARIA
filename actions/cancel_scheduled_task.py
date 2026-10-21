"""
actions/cancel_scheduled_task.py — cancel an in-app scheduler task
(ARIA's, ported). Thin wrapper over core/scheduler.cancel_task; the
ids come from list_scheduled_tasks.
"""

from __future__ import annotations

from core import outcome, scheduler


def cancel_scheduled_task(parameters: dict = None, response=None,
                          player=None, session_memory=None) -> str:
    params = parameters or {}
    try:
        task_id = int(params.get("task_id") or 0)
    except (TypeError, ValueError):
        task_id = 0
    if not task_id:
        return outcome.failed(
            "cancel_scheduled_task",
            "no task_id was given (see list_scheduled_tasks)")
    if scheduler.cancel_task(task_id):
        return outcome.done("cancel_scheduled_task", task_id=task_id) + \
            f"\nCancelled task #{task_id}."
    return outcome.failed(
        "cancel_scheduled_task", f"there is no scheduled task "
        f"#{task_id}", task_id=task_id)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "cancel_scheduled_task",
    "description": (
        "Cancel a timer or recurring task in the in-app scheduler "
        "by its id (from list_scheduled_tasks)."
    ),
    "brief": "Cancel a scheduled task by id.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "task_id": {
                "type": "INTEGER",
                "description": "The task id from list_scheduled_tasks",
            },
        },
        "required": ["task_id"],
    },
    "handler": cancel_scheduled_task,
}
