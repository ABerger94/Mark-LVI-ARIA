"""
actions/morning_briefing.py — ARIA's morning digest, one composition.

ARIA's tool_briefing (aria/scheduler.py) composed one spoken
paragraph from live sources — the calendar feed, the unread inbox,
the weather — in a fixed shape:

    "Good morning, Alek. Today is Wednesday, October 21. You've got:
    Dentist (3 PM to 4 PM). Tomorrow: … On email: 4 unread —
    notable: … In Dundalk it's 61 degrees and clear, with a high
    of 68 and a low of 49."

That composition lives here, section by section, on this build's
own sources: check_calendar's feed (core/ical), read_email's reader
(core/gmail — the email section works from the structured entries
instead of ARIA's re-parsing of her own summary lines), wttr.in
(ARIA's weather source — LVI's weather_report action opens a browser
page and returns no data a briefing could compose from), and, at
startup, headlines for the monitored topics (the manage_monitor
flow) fetched through main.py's news seam.

ONE composition, two deliveries, so they can never diverge:

  * the morning_briefing tool below, on demand (ARIA's fixed
    "Good morning" salutation — it is the morning digest);
  * the startup greeting (compose_startup_briefing, called by
    main.py's _send_startup_briefing): a time-aware salutation and
    a news section join the same calendar / email / weather
    sections. The greeting is confined to these composed sections —
    nothing from memory or earlier conversation is promoted into
    the opening, and a plan is asserted only when the calendar or
    the inbox actually carries it.

Honesty rules, both deliveries:

  * A source that is not connected says so in one short clause
    ("Your calendar isn't connected yet") instead of ARIA's
    behaviour of letting an unconfigured inbox parse-fail into
    "Inbox is clear" — a claim she could not back.
  * A source that ERRORS says that too ("I couldn't check your
    calendar just now") instead of silently vanishing.
  * At startup each section is bounded (SECTION_TIMEOUTS): a slow
    source degrades to a "taking too long, skipped it" note rather
    than stalling or killing the greeting.
"""

from __future__ import annotations

import asyncio
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
        return " I couldn't check your calendar just now."
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
        return " I couldn't check your email just now."
    if err:
        return " I couldn't check your email just now."
    if not entries:
        return " Inbox is clear."

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
        return " I couldn't fetch the weather just now."


# ── News (startup section; headlines from the monitored topics) ─────────────

_NEWS_FAILURE_PREFIXES = ("No news found", "Search failed", "Please provide")


def _news_failed(text) -> bool:
    return (not text) or text.startswith(_NEWS_FAILURE_PREFIXES)


def headlines_from_news_text(text: str, limit: int = 3) -> list[str]:
    """The numbered headline titles in _news output, source tags off."""
    titles = []
    for line in (text or "").splitlines():
        m = re.match(r"^\d+\.\s+(.+)$", line.strip())
        if m:
            title = re.sub(r"\s{2,}\[.*\]\s*$", "", m.group(1)).strip()
            if title:
                titles.append(title)
        if len(titles) >= limit:
            break
    return titles


def news_section_from_text(text: str) -> str:
    """One spoken news section from one fetch's text: the headline
    titles when the fetch came back in headline form, else the first
    sentences of the prose synthesis, bounded."""
    titles = headlines_from_news_text(text)
    if titles:
        return " In the news: " + ". ".join(titles) + "."
    prose = " ".join((text or "").split())
    if not prose:
        return ""
    body = " ".join(re.split(r"(?<=[.!?])\s+", prose)[:2])[:300].strip()
    if len(body) == 300 and " " in body:
        body = body.rsplit(" ", 1)[0]
    return f" In the news: {body}" if body else ""


def _fetch_news_block(topics, news_fetch):
    """Fetch every monitored topic (or the default world-news query
    when nothing is monitored). Returns (panel_title, panel_text,
    spoken_section); panel_text is "" when every fetch failed."""
    queries = list(topics) if topics else ["top world news today"]
    blocks, titles, prose = [], [], ""
    for q in queries:
        try:
            text = news_fetch(q)
        except Exception:
            text = ""
        if _news_failed(text):
            continue
        blocks.append(text)
        for t in headlines_from_news_text(text, limit=3):
            if len(titles) < 3:
                titles.append(t)
        if not titles and not prose:
            prose = text
    if not blocks:
        return "", "", ""
    section = (" In the news: " + ". ".join(titles) + "." if titles
               else news_section_from_text(prose))
    title = "NEWS — " + ", ".join(queries)
    return title, "\n\n".join(blocks), section


# ── The one composition ─────────────────────────────────────────────────────

def salutation_for(now: datetime) -> str:
    """The startup greeting's salutation, from the clock."""
    if now.hour < 12:
        return "Good morning"
    if now.hour < 18:
        return "Good afternoon"
    return "Good evening"


def compose_briefing_text(now: datetime = None,
                          city: str = "Dundalk,Maryland",
                          news_section: str = "",
                          salutation: str = None) -> str:
    """Salutation + date, calendar, email, weather (and news, when a
    caller supplies the section) — the whole briefing as one text.
    Every caller gets the same sections from the same code."""
    now = now or datetime.now()
    name = config_manager.get_user_name()
    greeting = (f"{salutation or salutation_for(now)}"
                f"{', ' + name if name else ''}.")
    return (f"{greeting} Today is {now.strftime('%A, %B %d')}."
            + _calendar_sections(now)
            + _email_section()
            + _weather_section(city)
            + (news_section or ""))


# Per-section bounds for the startup composition, in seconds. The
# sections run concurrently on worker threads, so the greeting waits
# at most the slowest section's bound (~12s worst case, a few seconds
# typically) — and a section that overruns is abandoned, not awaited:
# its thread finishes in the background and the greeting carries the
# skip note instead. A greeting that waits on the slowest network on
# the machine is the bug this replaced, one level up.
SECTION_TIMEOUTS = {"calendar": 8.0, "email": 10.0,
                    "weather": 12.0, "news": 12.0}

_TIMEOUT_NOTES = {
    "calendar": " Your calendar is taking too long to check, "
                "so I've skipped it this time.",
    "email":    " Your email is taking too long to check, "
                "so I've skipped it this time.",
    "weather":  " The weather is taking too long to load, "
                "so I've skipped it this time.",
    "news":     " The news is taking too long to load, "
                "so I've skipped it this time.",
}


async def compose_startup_briefing(now: datetime = None,
                                   city: str = "Dundalk,Maryland",
                                   topics=(), news_fetch=None,
                                   timeouts: dict = None):
    """The startup greeting's composition: compose_briefing_text's
    sections, each bounded per SECTION_TIMEOUTS, plus the news
    section from the monitored topics. Returns (text, panel), where
    panel is (title, body) for the on-screen news list, or None when
    no fetch succeeded. Never raises for a source failure — every
    failure lands in its section as an honest note."""
    now = now or datetime.now()
    bounds = timeouts or SECTION_TIMEOUTS

    async def _section(key, fn, *args):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(fn, *args), timeout=bounds[key])
        except Exception:
            return _TIMEOUT_NOTES[key]

    jobs = {
        "calendar": _section("calendar", _calendar_sections, now),
        "email":    _section("email", _email_section),
        "weather":  _section("weather", _weather_section, city),
    }
    panel = None
    if news_fetch is not None:
        async def _news():
            try:
                title, body, section = await asyncio.wait_for(
                    asyncio.to_thread(_fetch_news_block, topics,
                                      news_fetch),
                    timeout=bounds["news"])
            except Exception:
                return _TIMEOUT_NOTES["news"], None
            if not body:
                return " I couldn't fetch the news right now.", None
            return section, (title, body)
        jobs["news"] = _news()

    results = dict(zip(jobs, await asyncio.gather(*jobs.values())))
    news_section, panel = results.get("news", ("", None))
    name = config_manager.get_user_name()
    greeting = f"{salutation_for(now)}{', ' + name if name else ''}."
    text = (f"{greeting} Today is {now.strftime('%A, %B %d')}."
            + results["calendar"] + results["email"]
            + results["weather"] + (news_section or ""))
    return text, panel


def morning_briefing(parameters: dict = None, player=None,
                     session_memory=None) -> str:
    params = parameters or {}
    city = (params.get("city") or "Dundalk,Maryland").strip()
    # The on-demand digest keeps ARIA's fixed salutation — it is the
    # MORNING briefing whenever it is asked for. The startup greeting
    # composes with salutation_for(now) instead; the sections are
    # the same code either way.
    text = compose_briefing_text(city=city, salutation="Good morning")
    return outcome.done("morning_briefing") + "\n" + text


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "morning_briefing",
    "description": (
        "Compose and deliver the morning briefing: today's and "
        "tomorrow's calendar, the unread inbox's notable mail, and "
        "the weather, in one spoken paragraph (ARIA's digest). "
        "Sources that are not connected yet say so in the briefing. "
        "The same composition is the startup greeting, with the "
        "news headlines added. "
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
