"""
actions/morning_briefing.py — ARIA's morning digest, composed on demand.

ARIA's tool_briefing (aria/scheduler.py) composed one spoken
paragraph from three live sources — the calendar feed, the unread
inbox, the weather — in a fixed shape:

    "Good morning, Alek. Today is Wednesday, October 21. You've got:
    Dentist (3 PM to 4 PM). Tomorrow: … On email: 4 unread —
    notable: … In Dundalk it's 61 degrees and clear, with a high
    of 68 and a low of 49."

That composition is ported, section by section, onto this build's
own sources: check_calendar's feed (core/ical), read_email's reader
(core/gmail — the email section works from the structured entries
instead of ARIA's re-parsing of her own summary lines), and wttr.in,
which was ARIA's weather source. (LVI's weather_report action opens
a browser page; it returns no data a briefing could compose from.)

Two honest differences from ARIA:

  * A source that is not connected says so in one short clause
    ("Your calendar isn't connected yet") instead of ARIA's
    behaviour of letting an unconfigured inbox parse-fail into
    "Inbox is clear" — a claim she could not back.
  * TOOL-ONLY delivery. ARIA also fired this from her scheduler;
    this build has no scheduler a tool can register with — main.py's
    startup briefing is its own model-composed greeting, and the
    reminder action speaks fixed text via the OS scheduler — so the
    briefing here is on-demand: ask for it and it composes.
"""

from __future__ import annotations

import json
import re
import urllib.request
from datetime import datetime, timedelta

from core import gmail, ical, outcome
from memory import config_manager

_UA = {"User-Agent": "ARIA-Agent/8.0"}

# Network seams — replaced by tests, never by production code.
_fetch_json = None   # (url) -> dict
_fetch_text = None   # (url) -> str


def _get_json(url: str):
    if _fetch_json is not None:
        return _fetch_json(url)
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8", errors="replace"))


def _get_text(url: str) -> str:
    if _fetch_text is not None:
        return _fetch_text(url)
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.read().decode("utf-8", errors="replace").strip()


def _fmt_time(dt: datetime) -> str:
    """ARIA's: '3 PM' on the hour, '3:30 PM' otherwise."""
    suffix = "PM" if dt.hour >= 12 else "AM"
    h = dt.hour % 12 or 12
    return f"{h}:{dt.minute:02d} {suffix}" if dt.minute else f"{h} {suffix}"


def _entries_line(events) -> str:
    parts = []
    for e in events:
        sm = e["summary"]
        if e["all_day"]:
            parts.append(sm)
        else:
            tm = f"{_fmt_time(e['start'])} to {_fmt_time(e['end'])}"
            parts.append(f"{sm} ({tm})")
    return ", ".join(parts)


def _calendar_sections(now: datetime) -> str:
    url = config_manager.get_ical_url()
    if not url:
        return " Your calendar isn't connected yet."
    try:
        events = ical.upcoming_from_feed(url, days=2, now=now)
    except Exception:
        return ""
    today = now.date()
    tomorrow = today + timedelta(days=1)
    todays = [e for e in events if e["start"].date() == today]
    tmrows = [e for e in events if e["start"].date() == tomorrow]
    sched = (f"You've got: {_entries_line(todays)}." if todays
             else "Nothing on the schedule today.")
    tmrw = (f" Tomorrow: {_entries_line(tmrows)}." if tmrows else "")
    return " " + sched + tmrw


def _email_section() -> str:
    """ARIA's _email_brief_section, over structured entries."""
    if not gmail.connected():
        return " Email isn't connected yet."
    try:
        entries, err = gmail.list_summaries(unread_only=True, limit=10,
                                            action="morning_briefing")
    except Exception:
        return ""
    if err or not entries:
        return "" if err else " Inbox is clear."

    actionable = []
    for e in entries:
        sender = e["from"]
        name_match = re.match(r"^([^<]+)", sender)
        sender_name = (name_match.group(1).strip().strip('"')
                       if name_match else sender)
        subj = e["subject"]
        if "security alert" in subj.lower() \
                and "google" in sender_name.lower():
            continue                    # ARIA skipped Google's own alerts
        actionable.append(f"{sender_name} regarding '{subj}'")

    count = len(entries)
    if not actionable:
        return (f" You have {count} unread "
                f"email{'s' if count != 1 else ''}, mostly automated "
                f"alerts.")
    first_few = "; ".join(actionable[:2])
    rest = (f" and {count - len(actionable[:2])} other(s)"
            if count > len(actionable[:2]) else "")
    return f" On email: {count} unread — notable: {first_few}{rest}."


def _weather_section(city: str) -> str:
    """ARIA's wttr.in weather, full form with the short form behind it."""
    query = city.replace(" ", "+")
    display = city.split(",")[0].strip()
    try:
        d = _get_json(f"https://wttr.in/{query}?format=j1")
        cur = d["current_condition"][0]
        day = d["weather"][0]
        desc = (cur.get("weatherDesc") or [{}])[0].get("value", "").strip()
        temp = str(cur.get("temp_F", "")).strip()
        if temp:
            cond = f" and {desc}" if desc else ""
            return (f" In {display} it's {temp} degrees{cond}, with a "
                    f"high of {day.get('maxtempF', '')} and a low of "
                    f"{day.get('mintempF', '')}.")
    except Exception:
        pass
    try:
        now = _get_text(f"https://wttr.in/{query}?format=%C+%t")
        return f" In {display} it's {now}." if now else ""
    except Exception:
        return ""


def morning_briefing(parameters: dict = None, player=None,
                     session_memory=None) -> str:
    params = parameters or {}
    city = (params.get("city") or "Dundalk,Maryland").strip()
    now = datetime.now()
    name = config_manager.get_user_name()
    greeting = f"Good morning{', ' + name if name else ''}."
    day = now.strftime("%A, %B %d")
    text = (f"{greeting} Today is {day}."
            + _calendar_sections(now)
            + _email_section()
            + _weather_section(city))
    return outcome.done("morning_briefing") + "\n" + text


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "morning_briefing",
    "description": (
        "Compose and deliver the morning briefing: today's and "
        "tomorrow's calendar, the unread inbox's notable mail, and "
        "the weather, in one spoken paragraph (ARIA's digest). "
        "Sources that are not connected yet say so in the briefing. "
        "city defaults to Dundalk,Maryland, ARIA's home setting."
    ),
    "brief": "The morning briefing — calendar, email, weather ('city').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "city": {
                "type": "STRING",
                "description": "City for the weather section, e.g. 'Dundalk,Maryland' (default)",
            },
        },
        "required": [],
    },
    "handler": morning_briefing,
}
