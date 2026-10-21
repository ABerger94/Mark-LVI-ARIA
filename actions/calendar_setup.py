"""
actions/calendar_setup.py — connect Google Calendar (ARIA's, ported).

Saves the calendar's secret iCal URL into the config store (key
ical_url). The URL is a bearer credential — anyone holding it can read
the calendar — so it is stored like the other credentials here and
never echoed back. Read-only by design, as ARIA's was: this connects
her to the schedule, it does not give her a pen for it. There is
deliberately no event creation anywhere in this phase.
"""

from __future__ import annotations

from core import outcome
from memory import config_manager


def calendar_setup(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    params = parameters or {}
    url = (params.get("ical_url") or "").strip()
    if not url.startswith(("http://", "https://")) or "ics" not in url.lower():
        return outcome.failed(
            "calendar_setup",
            "that doesn't look like an iCal URL. In Google Calendar: "
            "Settings > the calendar > 'Secret address in iCal "
            "format' — copy that URL and pass it here.")
    config_manager.save_ical_url(url)
    return outcome.done("calendar_setup") + (
        "\nCalendar connected. The schedule can now be read with "
        "check_calendar.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "calendar_setup",
    "description": (
        "Connect Google Calendar (read-only): save the calendar's "
        "secret iCal URL so the schedule can be read with "
        "check_calendar. The URL is found in Google Calendar under "
        "Settings > the calendar > 'Secret address in iCal format'. "
        "Call this when the user gives you that URL."
    ),
    "brief": "Connect the calendar ('ical_url' — the secret iCal address).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "ical_url": {
                "type": "STRING",
                "description": "The secret iCal URL from Google Calendar settings",
            },
        },
        "required": ["ical_url"],
    },
    "handler": calendar_setup,
}
