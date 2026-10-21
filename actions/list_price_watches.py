"""
actions/list_price_watches.py — the active price watches (ARIA's,
ported). Thin wrapper over core/pricecheck's store and renderer.
"""

from __future__ import annotations

from core import outcome, pricecheck


def list_price_watches(parameters: dict = None, response=None, player=None,
                       session_memory=None) -> str:
    watches = pricecheck.list_watches()
    listing = pricecheck.render_watches(watches)
    return outcome.done("list_price_watches", count=len(watches)) + \
        "\n" + outcome.untrusted("price_watches", listing)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "list_price_watches",
    "description": (
        "List the active price watches: label, target price, last "
        "price seen, and whether the drop alert has already fired."
    ),
    "brief": "List active price watches.",
    "tier": "extra",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": list_price_watches,
}
