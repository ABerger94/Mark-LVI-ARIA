"""
actions/task_resume.py — return a blocked background task to work.

core/delegation.py has the whole design (Phase 2): a blocked
task holds a checkpoint, and resume requeues it from exactly
there — completed steps are never replayed. blocked_model,
blocked_input and blocked_unknown resume; a confirmation-gate
block does not (its outcome belongs to the banner), and terminal
or already-active tasks are refused in words. Resuming a
blocked_unknown task skips the tool call that was in flight when
the app died — its outcome is unknowable and the record names it
in skipped_calls — and continues from the step's next turn.
"""

from __future__ import annotations

from core import delegation, outcome


def task_resume(parameters: dict = None, player=None,
                session_memory=None) -> str:
    raw = (parameters or {}).get("task_id")
    try:
        task_id = int(raw)
    except (TypeError, ValueError):
        return outcome.failed("task_resume",
                              "a numeric task_id is required")
    ok, message = delegation.resume(task_id)
    if not ok:
        return outcome.failed("task_resume", message)
    return outcome.done("task_resume", task=task_id) + f"\n{message}."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "task_resume",
    "description": (
        "Resume a blocked background (delegated) task from its "
        "checkpoint: use when task_status shows blocked_model "
        "(the model was unreachable — also retried automatically "
        "on the next launch), blocked_input (the worker needed "
        "the user; resume once the need is satisfied), or "
        "blocked_unknown (the app died mid-tool-call; resuming "
        "skips that call — its outcome is unknown — and "
        "continues, never replaying it). Completed steps are "
        "never replayed. A task blocked at the confirmation gate "
        "cannot be resumed — its outcome belongs to the banner. "
        "Terminal (done/failed/cancelled) and already-active "
        "tasks are refused."
    ),
    "brief": "Resume a blocked background task ('task_id').",
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
    "handler": task_resume,
}
