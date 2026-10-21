"""
actions/list_scheduled_tasks.py — what the in-app scheduler holds
(ARIA's, ported).

Timers, one-shots and recurring tasks from core/scheduler's store,
soonest first, in ARIA's line format. The listing ends with the
honest boundary: reminders set through the `reminder` tool live in
the OS task scheduler and are NOT here — this list is only what
the in-app scheduler owns.
"""

from __future__ import annotations

from core import outcome, scheduler


def list_scheduled_tasks(parameters: dict = None, response=None,
                         player=None, session_memory=None) -> str:
    rows = scheduler.list_rows()
    listing = scheduler.render_list(rows)
    return outcome.done("list_scheduled_tasks", count=len(rows)) + \
        "\n" + outcome.untrusted("scheduler", listing)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "list_scheduled_tasks",
    "description": (
        "List the in-app scheduler's tasks: timers, one-shot "
        "reminders and recurring tasks, with ids and next-run times. "
        "Ids here are what cancel_scheduled_task takes. Reminders "
        "set with the reminder tool are OS-scheduled and do not "
        "appear — the listing says so itself."
    ),
    "brief": "List timers and recurring tasks in the in-app scheduler.",
    "tier": "extra",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": list_scheduled_tasks,
}
