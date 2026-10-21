"""
actions/check_price_watches.py — run one price-check pass now
(ARIA's, ported).

The same pass the hourly scheduler task runs; this is the manual
trigger. A drop found here alerts in the reply and marks the watch
alerted, so the hourly pass will not announce the same drop again.
"""

from __future__ import annotations

from core import outcome, pricecheck


def check_price_watches(parameters: dict = None, response=None, player=None,
                        session_memory=None) -> str:
    watches = pricecheck.list_watches()
    if not watches:
        return outcome.done("check_price_watches", checked=0) + \
            "\n[No price watches to check]"
    summary, alerts = pricecheck.check_watches()
    return outcome.done("check_price_watches", checked=len(watches),
                        alerts=len(alerts)) + "\n" + \
        outcome.untrusted("price_watches", summary)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "check_price_watches",
    "description": (
        "Check every price watch right now (the same pass that runs "
        "hourly): fetches each watched page, updates the last seen "
        "price, and reports any that have dropped to or below their "
        "target."
    ),
    "brief": "Run a price-watch check pass now.",
    "tier": "extra",
    "parameters": {"type": "OBJECT", "properties": {}},
    "handler": check_price_watches,
}
