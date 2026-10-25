"""
actions/delegate_task.py — hand a prompt to a background worker.

core/delegation.py has the whole design (Phase 2): the task is
planned first, runs step by step with a checkpoint persisted
after every step, can park as blocked (model down, needs the
user, died mid-call) and resume from its checkpoint, and its
result is verified by a separate pass before it may call itself
done. The record lives in memory/delegated_tasks.json, its
completion announced once through the proactive speech seam.
This action is the thin surface: queue it, report the id — or
the honest refusal (the bound is four in flight, and a refusal
beats a silent backlog). Check on it with task_status; stop it
with task_cancel; resume a blocked one with task_resume.
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
        "that is planned, then run step by step through the model "
        "with the usual tools on a worker thread while the "
        "conversation continues. Progress is checkpointed, so a "
        "task survives a restart and can be resumed when blocked; "
        "its result is verified by a separate pass before it is "
        "reported done (unverified results are labelled "
        "done_unverified). The task gets an id; its result is "
        "announced when it finishes and can be read with "
        "task_status. At most four tasks may be in flight; beyond "
        "that this refuses. A delegated task stops at the "
        "confirmation gate like any other call — it never "
        "bypasses a confirmation."
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
