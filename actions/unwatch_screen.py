"""
actions/unwatch_screen.py — stop a screen watch (ARIA's, ported).
Removes the watch and cancels its in-app scheduler task.
"""

from __future__ import annotations

from core import outcome, screenwatch


def unwatch_screen(parameters: dict = None, response=None, player=None,
                   session_memory=None) -> str:
    params = parameters or {}
    ok, message = screenwatch.unwatch_screen(params.get("name") or "")
    if not ok:
        return outcome.failed("unwatch_screen", message)
    return outcome.done("unwatch_screen",
                        name=screenwatch.sanitize_name(
                            params.get("name") or "")) + "\n" + message


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "unwatch_screen",
    "description": (
        "Stop a screen watch by name (from list_screen_watches) and "
        "cancel its scheduled checks."
    ),
    "brief": "Stop a screen watch by name.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The watch name from list_screen_watches",
            },
        },
        "required": ["name"],
    },
    "handler": unwatch_screen,
}
