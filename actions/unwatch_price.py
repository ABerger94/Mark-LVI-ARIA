"""
actions/unwatch_price.py — remove a price watch (ARIA's, ported).

Deliberately NOT confirm-gated, following ARIA: her unwatch deleted
without asking, and the migration keeps her behaviour here. A watch
is not user data of consequence — removing one loses a URL and a
target number, both trivially re-added with watch_price — so it
does not belong in the destructive class Phase C gated (file
deletes, sends, installs, shutdowns). Noted as a conscious choice,
not an omission.
"""

from __future__ import annotations

from core import outcome, pricecheck


def unwatch_price(parameters: dict = None, response=None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    try:
        watch_id = int(params.get("watch_id") or 0)
    except (TypeError, ValueError):
        watch_id = 0
    if not watch_id:
        return outcome.failed(
            "unwatch_price", "no watch_id was given (see list_price_watches)")
    if pricecheck.remove_watch(watch_id):
        return outcome.done("unwatch_price", watch_id=watch_id) + \
            f"\nRemoved price watch #{watch_id}."
    return outcome.failed(
        "unwatch_price", f"there is no price watch #{watch_id}",
        watch_id=watch_id)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "unwatch_price",
    "description": (
        "Remove a price watch by its id (from list_price_watches). "
        "Not confirmation-gated — a watch is just a URL and a target, "
        "re-added in one call if removed by mistake."
    ),
    "brief": "Remove a price watch by id.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "watch_id": {
                "type": "INTEGER",
                "description": "The watch id from list_price_watches",
            },
        },
        "required": ["watch_id"],
    },
    "handler": unwatch_price,
}
