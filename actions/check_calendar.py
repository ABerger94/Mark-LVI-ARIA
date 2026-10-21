"""
actions/check_calendar.py — read the schedule (ARIA's, ported).

Fetches the connected calendar's iCal feed and lists events in the
window [today, +days) in ARIA's line format. Read-only, like the
connection itself. Unconfigured answers with the same clean
not-connected outcome as the Gmail tools — pointing at
calendar_setup — and a feed that will not load is a failure with the
reason, not a shrug.
"""

from __future__ import annotations

from core import ical, outcome
from memory import config_manager


def check_calendar(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    params = parameters or {}
    url = config_manager.get_ical_url()
    if not url:
        return outcome.failed(
            "check_calendar",
            "no calendar is connected yet. Tell the user to say "
            "'set up my calendar' with the secret iCal URL from "
            "Google Calendar (Settings > the calendar > 'Secret "
            "address in iCal format'), then call calendar_setup "
            "with it.")
    try:
        days = max(1, min(14, int(params.get("days", 1))))
    except (TypeError, ValueError):
        days = 1
    try:
        events = ical.upcoming_from_feed(url, days=days)
    except Exception as e:
        return outcome.failed("check_calendar",
                              f"could not fetch the calendar: {e}")
    if not events:
        return outcome.done("check_calendar", days=days, count=0) + (
            f"\nNothing on the calendar for the next {days} day(s).")
    lines = "\n".join(ical.format_event(e) for e in events)
    return outcome.done("check_calendar", days=days,
                        count=len(events)) + "\n" + \
        outcome.untrusted("calendar", lines)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "check_calendar",
    "description": (
        "Read the user's upcoming calendar events from the connected "
        "Google Calendar (connect it first with calendar_setup). "
        "days looks ahead from today (default 1 = today only, max 14). "
        "Read-only: there is no event creation."
    ),
    "brief": "What's on the calendar ('days' ahead, default 1).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "days": {
                "type": "INTEGER",
                "description": "How many days ahead to look, starting today (1-14, default 1)",
            },
        },
        "required": [],
    },
    "handler": check_calendar,
}
