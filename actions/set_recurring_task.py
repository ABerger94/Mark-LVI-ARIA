"""
actions/set_recurring_task.py — a prompt that runs itself on an
interval (ARIA's, ported).

The task lives in the in-app scheduler; when it fires, the prompt
is injected as a turn ('[Scheduled task] ...') and she acts on it —
checks the thing, reports the change, whatever the prompt says.
Minimum interval 60 seconds, as ARIA enforced. List and cancel
with list_scheduled_tasks / cancel_scheduled_task.
"""

from __future__ import annotations

from core import outcome, scheduler


def set_recurring_task(parameters: dict = None, response=None, player=None,
                       session_memory=None) -> str:
    params = parameters or {}
    prompt = (params.get("prompt") or "").strip()
    if not prompt:
        return outcome.failed("set_recurring_task", "no prompt was given")
    try:
        interval = int(params.get("interval_seconds") or 0)
    except (TypeError, ValueError):
        interval = 0
    if interval <= 0:
        return outcome.failed(
            "set_recurring_task",
            "interval_seconds must be a positive number of seconds "
            "(minimum 60)")
    tid, kind = scheduler.set_recurring(interval, prompt)
    interval = max(60, interval)
    if kind == "brief":
        return outcome.done("set_recurring_task", task_id=tid,
                            interval_seconds=interval, kind=kind) + (
            f"\nMorning-brief task set (#{tid}): I'll speak your "
            f"morning briefing every {interval} seconds.")
    return outcome.done("set_recurring_task", task_id=tid,
                        interval_seconds=interval, kind=kind) + (
        f"\nRecurring task set (#{tid}): '{prompt}' every {interval} "
        f"seconds.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "set_recurring_task",
    "description": (
        "Run a prompt on a repeating interval (minimum 60 seconds): "
        "'every hour, check whether the build finished'. When it "
        "fires, the prompt runs as a turn and she reports back in "
        "conversation. Persists across restarts. Cancel with "
        "cancel_scheduled_task; see them with list_scheduled_tasks."
    ),
    "brief": "Run a prompt every interval (min 60s).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "interval_seconds": {
                "type": "INTEGER",
                "description": "Seconds between runs (minimum 60)",
            },
            "prompt": {
                "type": "STRING",
                "description": "What to do each time, as an instruction",
            },
        },
        "required": ["interval_seconds", "prompt"],
    },
    "handler": set_recurring_task,
}
