"""
actions/delegate_task.py — hand a prompt to a background worker.

core/delegation.py has the whole design: the task runs through
the app's model with the app's tools on a worker thread, its
record in memory/delegated_tasks.json, its completion announced
once through the proactive speech seam. This action is the thin
surface: queue it, report the id — or the honest refusal (the
bound is four in flight, and a refusal beats a silent backlog).
Check on it with task_status; stop it with task_cancel.
"""

from __future__ import annotations

from core import delegation, outcome


def delegate_task(parameters: dict = None, player=None,
                  session_memory=None) -> str:
    prompt = (parameters or {}).get("prompt") or ""
    task_id, refusal = delegation.delegate(prompt)
    if task_id is None:
        return outcome.failed("delegate_task", refusal)
    return outcome.done("delegate_task", task=task_id) + (
        f"\nBackground task #{task_id} started. Its result lands "
        f"in the task record and is announced when it finishes — "
        f"check it any time with task_status.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "delegate_task",
    "description": (
        "Start a background task: a self-contained job (research "
        "something, check several things, draft something long) "
        "that runs through the model with the usual tools on a "
        "worker thread while the conversation continues. The task "
        "gets an id; its result is announced when it finishes and "
        "can be read with task_status. At most four tasks may be "
        "in flight; beyond that this refuses. A delegated task "
        "stops at the confirmation gate like any other call — it "
        "never bypasses a confirmation."
    ),
    "brief": "Delegate a background task ('prompt').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "prompt": {
                "type": "STRING",
                "description": "The whole job, stated completely — the worker has no other context",
            },
        },
        "required": ["prompt"],
    },
    "handler": delegate_task,
}
