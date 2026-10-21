"""Phase F2 — the startup greeting IS the composed briefing.

The bug (Alek's witness, 2026-10-21 12:33): on launch she greeted
him with "I'm gathering today's news for you right now" and one
news item — the old two-phase greeting told the model to greet
warmly, mention the time and say it was fetching the news, and the
model improvised the rest. A second specimen (12:53) showed where
that leads: the freelance greeting promoted a niece he had
mentioned in conversation into a definite plan — "you'll be
catching up with your niece later on" — with no calendar item
behind it.

The fix pinned here: main.py's _send_startup_briefing composes the
briefing in code (actions/morning_briefing.compose_startup_briefing
— the same composition the morning_briefing tool serves) and sends
it as ONE [STARTUP_BRIEFING] turn confined to the composed
sections. Calendar / mail / weather / news data are the only
things the opening can assert.

The seam: _send_startup_briefing talks only to self.session's
send_client_content — the one interface both engines implement
(Gemini Live's session and the providers pipeline's Engine
protocol, providers/base.py) — so a fake session drives the real
method here for both engines at once. JarvisLive is built with
__new__ (it cannot fully instantiate headless); the attributes the
method touches are set by hand.
"""
import asyncio
import inspect
import threading
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import main
import actions.morning_briefing as mb
from core import gmail, ical
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent

WTTR = {
    "current_condition": [{"temp_F": "61",
                           "weatherDesc": [{"value": "Sunny"}]}],
    "weather": [{"maxtempF": "68", "mintempF": "49"}],
}

NEWS_TEXT = (
    "Latest news: space exploration\n\n"
    "1. Rocket Lab launches a new satellite batch  [SpaceNews]\n"
    "   The launch window opened early this morning.\n\n"
    "2. City council approves the harbour plan  [Local]\n"
    "   The vote was unanimous.\n\n"
    "3. Markets steady ahead of the jobs report  [Wire]\n"
)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    cm.save_assistant_config("ARIA", "Alek")
    return path


def _event(summary, when, all_day=False, hours=1):
    start = when.replace(minute=0, second=0, microsecond=0)
    return {"summary": summary, "description": "", "start": start,
            "end": start + timedelta(hours=hours), "all_day": all_day}


@pytest.fixture()
def sources(cfg, monkeypatch):
    """Calendar + inbox + weather stubbed the way test_briefing does."""
    now = datetime.now()
    events = [
        _event("Dentist", now.replace(hour=15)),
        _event("Shift at the Dock", now.replace(hour=9) + timedelta(days=1)),
    ]
    monkeypatch.setattr(ical, "upcoming_from_feed",
                        lambda url, days=1, now=None: events)
    cm.save_ical_url("https://example.invalid/basic.ics")
    cm.save_gmail_credentials("alek@example.com", "pw")
    entries = [
        {"uid": "1", "date": "today", "from": "Boss <boss@work.example>",
         "subject": "Q3 report due"},
    ]
    monkeypatch.setattr(gmail, "list_summaries",
                        lambda **kw: (entries, None))
    monkeypatch.setattr(mb, "_fetch_json", lambda url: WTTR)
    monkeypatch.setattr(mb, "_fetch_text", lambda url: "")
    return now


# ── The composer (async, bounded) ────────────────────────────────────────────

def test_startup_composition_has_every_section(sources):
    async def go():
        return await mb.compose_startup_briefing(
            topics=["space exploration"],
            news_fetch=lambda q: NEWS_TEXT)
    text, panel = asyncio.run(go())
    # time-aware salutation (not the tool's fixed "Good morning")
    assert text.startswith(f"{mb.salutation_for(datetime.now())}, Alek.")
    assert "You've got: Dentist (3 PM to 4 PM)." in text
    assert "Tomorrow: Shift at the Dock (9 AM to 10 AM)." in text
    assert "On email: 1 unread — notable: Boss regarding 'Q3 report due'." \
        in text
    assert "In Dundalk it's 61 degrees and Sunny" in text
    # headlines composed in, source tags stripped, panel carries the list
    assert "In the news: Rocket Lab launches a new satellite batch." in text
    assert "[SpaceNews]" not in text
    assert panel is not None
    assert panel[0] == "NEWS — space exploration"
    assert "City council approves the harbour plan" in panel[1]


def test_startup_default_query_when_no_topics(sources):
    seen = []

    def fetch(q):
        seen.append(q)
        return NEWS_TEXT

    text, panel = asyncio.run(mb.compose_startup_briefing(
        topics=[], news_fetch=fetch))
    assert seen == ["top world news today"]
    assert panel[0] == "NEWS — top world news today"
    assert "In the news:" in text


def test_startup_unconnected_sources_say_so(cfg, monkeypatch):
    monkeypatch.setattr(mb, "_fetch_json", lambda url: WTTR)
    monkeypatch.setattr(mb, "_fetch_text", lambda url: "")
    text, panel = asyncio.run(mb.compose_startup_briefing(
        topics=[], news_fetch=lambda q: NEWS_TEXT))
    assert "Your calendar isn't connected yet." in text
    assert "Email isn't connected yet." in text


def test_startup_failing_sources_still_greet(sources, monkeypatch):
    def boom_feed(url, days=1, now=None):
        raise OSError("feed down")

    def boom_mail(**kw):
        raise OSError("imap down")

    def boom_news(q):
        raise OSError("ddg down")

    monkeypatch.setattr(ical, "upcoming_from_feed", boom_feed)
    monkeypatch.setattr(gmail, "list_summaries", boom_mail)
    monkeypatch.setattr(mb, "_fetch_json",
                        lambda url: (_ for _ in ()).throw(OSError("down")))
    monkeypatch.setattr(mb, "_fetch_text",
                        lambda url: (_ for _ in ()).throw(OSError("down")))
    text, panel = asyncio.run(mb.compose_startup_briefing(
        topics=[], news_fetch=boom_news))
    assert "Alek." in text and "Today is " in text      # she still greets
    assert "I couldn't check your calendar just now." in text
    assert "I couldn't check your email just now." in text
    assert "I couldn't fetch the weather just now." in text
    assert "I couldn't fetch the news right now." in text
    assert panel is None


def test_startup_slow_section_is_skipped_not_awaited(sources, monkeypatch):
    blocker = threading.Event()

    def slow_mail(**kw):
        blocker.wait(30)
        return ([], None)

    monkeypatch.setattr(gmail, "list_summaries", slow_mail)
    bounds = dict(mb.SECTION_TIMEOUTS, email=0.2)
    try:
        text, panel = asyncio.run(mb.compose_startup_briefing(
            topics=[], news_fetch=lambda q: NEWS_TEXT, timeouts=bounds))
    finally:
        blocker.set()          # release the abandoned worker thread
    assert "Your email is taking too long to check" in text
    assert "You've got: Dentist (3 PM to 4 PM)." in text   # rest composed


def test_salutation_follows_the_clock():
    assert mb.salutation_for(datetime(2026, 10, 21, 8, 0)) == "Good morning"
    assert mb.salutation_for(datetime(2026, 10, 21, 12, 33)) == \
        "Good afternoon"
    assert mb.salutation_for(datetime(2026, 10, 21, 21, 5)) == "Good evening"


# ── The confinement pin ──────────────────────────────────────────────────────

def test_empty_calendar_greeting_asserts_no_plans(sources, monkeypatch):
    """The 12:53 specimen, pinned: with an empty calendar the composed
    greeting says there is nothing scheduled — it cannot carry a
    plan (a niece, a catch-up, anything) promoted out of memory or
    conversation, because the composer has no such input."""
    monkeypatch.setattr(ical, "upcoming_from_feed",
                        lambda url, days=1, now=None: [])
    text, _panel = asyncio.run(mb.compose_startup_briefing(
        topics=[], news_fetch=lambda q: NEWS_TEXT))
    assert "Nothing on the schedule today." in text
    assert "You've got:" not in text
    assert "catching up" not in text
    assert "later on" not in text


# ── The main.py seam (both engines share it) ─────────────────────────────────

class _FakeUI:
    def __init__(self):
        self.content = []
        self.logs = []

    def show_content(self, title, text):
        self.content.append((title, text))

    def write_log(self, msg):
        self.logs.append(msg)


class _FakeSession:
    """The Engine/Live session surface the startup path uses — nothing
    else. Both real engines implement exactly this method."""

    def __init__(self):
        self.sent = []

    async def send_client_content(self, turns=None, turn_complete=True):
        self.sent.append(turns["parts"][0]["text"])


def _live(monkeypatch):
    live = main.JarvisLive.__new__(main.JarvisLive)
    live.session = _FakeSession()
    live.ui = _FakeUI()
    monkeypatch.setattr(main, "load_memory", lambda: {"identity": {}})
    monkeypatch.setattr(main, "list_monitors",
                        lambda: ["space exploration"])
    monkeypatch.setattr(main, "_fetch_news_sync", lambda q: NEWS_TEXT)
    return live


def test_startup_turn_is_the_composed_briefing(sources, monkeypatch):
    live = _live(monkeypatch)
    asyncio.run(live._send_startup_briefing())
    assert len(live.session.sent) == 1
    turn = live.session.sent[0]
    assert turn.startswith("[STARTUP_BRIEFING]")
    assert "no facts beyond it" in turn          # the confinement rule
    assert "fetching today's news" not in turn   # the freelance line is gone
    # the briefing itself, verbatim at the tail of the turn
    briefing = turn.split("\n\n", 1)[1]
    assert "You've got: Dentist (3 PM to 4 PM)." in briefing
    assert "In the news: Rocket Lab launches" in briefing
    # the on-screen news list still works
    assert live.ui.content and \
        live.ui.content[0][0] == "NEWS — space exploration"


def test_startup_turn_empty_calendar_no_plan_claims(sources, monkeypatch):
    monkeypatch.setattr(ical, "upcoming_from_feed",
                        lambda url, days=1, now=None: [])
    live = _live(monkeypatch)
    asyncio.run(live._send_startup_briefing())
    turn = live.session.sent[0]
    assert "Nothing on the schedule today." in turn
    assert "You've got:" not in turn
    assert "catching up" not in turn


def test_startup_path_uses_only_the_shared_session_interface():
    """Both engines greet through this method; it must stay on the
    shared session surface (providers/base.py's Engine protocol) and
    on the shared composer — no engine-specific calls, no freelance
    greeting text, no last-session riffing."""
    from providers.base import Engine
    assert hasattr(Engine, "send_client_content")
    src = inspect.getsource(main.JarvisLive._send_startup_briefing)
    assert "compose_startup_briefing" in src
    assert "_engine" not in src
    assert "pop_last_session" not in src
    assert "Greet the user warmly" not in src
