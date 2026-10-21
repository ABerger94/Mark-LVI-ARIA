"""
actions/break_reminders.py — the 90-minute activity break nudge,
on or off (ARIA's, ported).

ARIA's toggle was a module global that forgot itself on restart;
here the state persists in the scheduler's store, and the nudge
fires from the scheduler tick through the bound speak hook — a
short spoken 'stretch, rest your eyes, get some water' every 90
minutes while enabled.
"""

from __future__ import annotations

from core import outcome, scheduler


def break_reminders(parameters: dict = None, response=None, player=None,
                    session_memory=None) -> str:
    params = parameters or {}
    raw = params.get("enabled", True)
    if isinstance(raw, str):
        enabled = raw.strip().lower() in ("1", "true", "on", "yes")
    else:
        enabled = bool(raw)
    state = scheduler.set_break_reminders(enabled)
    return outcome.done("break_reminders", enabled=state) + \
        f"\nBreak reminders {'enabled' if state else 'disabled'}."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "break_reminders",
    "description": (
        "Turn the activity break nudge on or off. While on, she "
        "speaks a short stretch-and-rest reminder every 90 minutes. "
        "The setting persists across restarts."
    ),
    "brief": "Toggle the 90-minute break nudge.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "enabled": {
                "type": "BOOLEAN",
                "description": "true to enable the nudge, false to stop it",
            },
        },
        "required": [],
    },
    "handler": break_reminders,
}
