"""
actions/set_timer.py — a spoken timer (ARIA's, ported).

'duration_text' is parsed the way ARIA parsed it ('20 minutes',
'1h30m', '90 seconds', a bare number = minutes). The timer lives in
the in-app scheduler's store, so unlike ARIA's in-memory
threading.Timer it survives a restart, and it fires through her
speech: "Timer done: <label>."
"""

from __future__ import annotations

from core import outcome, scheduler


def set_timer(parameters: dict = None, response=None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    duration_text = (params.get("duration_text") or "").strip()
    label = (params.get("label") or "timer").strip() or "timer"
    if not duration_text:
        return outcome.failed("set_timer", "no duration was given")
    tid, secs = scheduler.set_timer(duration_text, label)
    if tid is None:
        return outcome.failed(
            "set_timer",
            f"I couldn't understand that duration — try '20 minutes' "
            f"or '1h30m' (got '{duration_text}')")
    return outcome.done("set_timer", task_id=tid, seconds=secs,
                        label=label) + \
        f"\nTimer set: {label}, {duration_text} from now."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "set_timer",
    "description": (
        "Set a timer that speaks when it finishes ('timer for 20 "
        "minutes', '1h30m timer for the pasta'). duration_text takes "
        "natural durations: '20 minutes', '1h30m', '90s', or a bare "
        "number meaning minutes. For a reminder with a message at a "
        "delay, reminder or the scheduler's one-shots fit better; a "
        "timer just announces itself by label."
    ),
    "brief": "Set a spoken timer ('20 minutes', '1h30m').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "duration_text": {
                "type": "STRING",
                "description": "How long: '20 minutes', '1h30m', '90s', or bare minutes",
            },
            "label": {
                "type": "STRING",
                "description": "What the timer is for (default 'timer')",
            },
        },
        "required": ["duration_text"],
    },
    "handler": set_timer,
}
