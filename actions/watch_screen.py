"""
actions/watch_screen.py — start watching the screen for a change
(ARIA's, ported). Thin wrapper over core/screenwatch: the watch
polls the screen with its question on its interval (an in-app
scheduler task) and she speaks up when the answer changes.
"""

from __future__ import annotations

from core import outcome, screenwatch


def watch_screen(parameters: dict = None, response=None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    name = params.get("name") or ""
    question = params.get("question") or ""
    try:
        interval = int(params.get("interval_s") or 300)
    except (TypeError, ValueError):
        interval = 300
    ok, message, watch = screenwatch.watch_screen(name, question, interval)
    if not ok:
        return outcome.failed("watch_screen", message)
    return outcome.done("watch_screen", name=screenwatch.sanitize_name(name),
                        interval_s=watch["interval_s"],
                        task_id=watch["sched_task_id"]) + "\n" + message


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "watch_screen",
    "description": (
        "Watch the screen for the answer to a question changing: "
        "'watch for the build to finish', 'tell me when the order "
        "status changes'. The screen is captured and asked the "
        "question every interval_s seconds (minimum 60, default "
        "300), and she speaks up when the answer changes. Give the "
        "watch a short name — unwatch_screen and list_screen_watches "
        "use it."
    ),
    "brief": "Watch the screen for an answer changing; speak on change.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "Short watch name (lowercase letters, numbers, _ or -)",
            },
            "question": {
                "type": "STRING",
                "description": "What to ask about the screen each check",
            },
            "interval_s": {
                "type": "INTEGER",
                "description": "Seconds between checks (minimum 60, default 300)",
            },
        },
        "required": ["name", "question"],
    },
    "handler": watch_screen,
}
