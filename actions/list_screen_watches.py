"""
actions/list_screen_watches.py — the active screen watches
(ARIA's, ported). Thin wrapper over core/screenwatch's store.
"""

from __future__ import annotations

from core import outcome, screenwatch


def list_screen_watches(parameters: dict = None, response=None,
                        player=None, session_memory=None) -> str:
    watches = screenwatch.list_watches()
    listing = screenwatch.render_list(watches)
    return outcome.done("list_screen_watches", count=len(watches)) + \
        "\n" + outcome.untrusted("screen_watches", listing)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "list_screen_watches",
    "description": (
        "List the active screen watches: name, question, check "
        "interval, and when each was last checked."
    ),
    "brief": "List active screen watches.",
    "tier": "extra",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": list_screen_watches,
}
