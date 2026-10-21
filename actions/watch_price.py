"""
actions/watch_price.py — watch a product page for a price drop
(ARIA's, ported).

Adds the watch and makes sure the hourly in-app check task exists
(ensure_pricecheck_task), so 'checks run hourly' is a fact about
this build and not just a line in the reply — the gap ARIA's own
module header admitted hers once had.
"""

from __future__ import annotations

from core import outcome, pricecheck


def watch_price(parameters: dict = None, response=None, player=None,
                session_memory=None) -> str:
    params = parameters or {}
    url = (params.get("url") or "").strip()
    label = (params.get("label") or "item").strip() or "item"
    raw_target = str(params.get("target_price") or "").replace("$", "").strip()
    if not url:
        return outcome.failed("watch_price", "no URL was given to watch")
    try:
        target = float(raw_target)
    except ValueError:
        return outcome.failed(
            "watch_price",
            f"target_price must be a number, e.g. 40 — got "
            f"'{params.get('target_price')}'")
    watch = pricecheck.add_watch(url, target, label)
    pricecheck.ensure_pricecheck_task()
    return outcome.done("watch_price", watch_id=watch["id"], label=label,
                        target=f"{target:.2f}") + (
        f"\nWatching '{label}' (#{watch['id']}): I'll alert you when "
        f"it's at or below ${target:.2f}. Checks run hourly.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "watch_price",
    "description": (
        "Watch a product page and alert when its price drops to or "
        "below a target. Checks run hourly in the app and the alert "
        "fires once per drop. List them with list_price_watches, "
        "check now with check_price_watches, remove with "
        "unwatch_price."
    ),
    "brief": "Watch a product URL for a price at or below a target.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "url": {
                "type": "STRING",
                "description": "The product page URL to watch",
            },
            "target_price": {
                "type": "STRING",
                "description": "Alert at or below this price, e.g. '40' or '39.99'",
            },
            "label": {
                "type": "STRING",
                "description": "A short name for the item (default 'item')",
            },
        },
        "required": ["url", "target_price"],
    },
    "handler": watch_price,
}
