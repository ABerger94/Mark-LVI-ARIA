"""Phase E5 — the morning briefing, ARIA's digest composed on demand.

The composition is ARIA's (aria/scheduler.py::tool_briefing): greeting
+ date, today's schedule, tomorrow's, the email section, the weather —
one paragraph. All three sources are stubbed:

  * the calendar through core/ical.upcoming_from_feed,
  * the inbox through core/gmail.list_summaries (ARIA's section logic,
    including skipping Google's own security alerts),
  * the weather through the action's wttr.in seams.

And the two honesty rules the port added: a source that is not
connected says so in the paragraph (ARIA's unconfigured inbox
parse-failed into "Inbox is clear"), and delivery is tool-only —
there is no scheduler here for it to register with.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import actions.morning_briefing as mb
from core import gmail, ical
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent

WTTR = {
    "current_condition": [{"temp_F": "61",
                           "weatherDesc": [{"value": "Sunny"}]}],
    "weather": [{"maxtempF": "68", "mintempF": "49"}],
}


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
        {"uid": "2", "date": "today", "from": "Google <no-reply@google.example>",
         "subject": "Security alert"},
        {"uid": "3", "date": "today", "from": "Mum <mum@family.example>",
         "subject": "Sunday dinner?"},
        {"uid": "4", "date": "today", "from": "Shop <deals@shop.example>",
         "subject": "Your receipt"},
    ]
    monkeypatch.setattr(
        gmail, "list_summaries",
        lambda **kw: (entries, None))
    monkeypatch.setattr(mb, "_fetch_json", lambda url: WTTR)
    monkeypatch.setattr(mb, "_fetch_text", lambda url: "")
    return now


def test_briefing_composes_arias_shape(cfg, sources):
    result = mb.morning_briefing({})
    assert result.startswith("status=done"), result
    text = result.split("\n", 1)[1]
    assert text.startswith("Good morning, Alek. Today is ")
    assert "You've got: Dentist (3 PM to 4 PM)." in text
    assert "Tomorrow: Shift at the Dock (9 AM to 10 AM)." in text
    # 4 unread; Google's security alert skipped as ARIA skipped it;
    # the first two actionable are named, the rest counted.
    assert "On email: 4 unread — notable: Boss regarding 'Q3 report due'; " \
           "Mum regarding 'Sunday dinner?' and 2 other(s)." in text
    assert "In Dundalk it's 61 degrees and Sunny, with a high of 68 " \
           "and a low of 49." in text


def test_briefing_clear_inbox_and_empty_day(cfg, sources, monkeypatch):
    monkeypatch.setattr(ical, "upcoming_from_feed",
                        lambda url, days=1, now=None: [])
    monkeypatch.setattr(gmail, "list_summaries", lambda **kw: ([], None))
    text = mb.morning_briefing({}).split("\n", 1)[1]
    assert "Nothing on the schedule today." in text
    assert "Inbox is clear." in text


def test_briefing_unconnected_sources_say_so(cfg, monkeypatch):
    # Fresh config apart from the user's name: nothing connected.
    monkeypatch.setattr(mb, "_fetch_json", lambda url: WTTR)
    monkeypatch.setattr(mb, "_fetch_text", lambda url: "")
    text = mb.morning_briefing({}).split("\n", 1)[1]
    assert "Your calendar isn't connected yet." in text
    assert "Email isn't connected yet." in text
    assert "Inbox is clear" not in text        # the claim ARIA made blind


def test_briefing_weather_falls_back_to_short_form(cfg, sources,
                                                   monkeypatch):
    def no_json(url):
        raise OSError("wttr down")

    monkeypatch.setattr(mb, "_fetch_json", no_json)
    monkeypatch.setattr(mb, "_fetch_text", lambda url: "Clear +61°F")
    text = mb.morning_briefing({}).split("\n", 1)[1]
    assert "In Dundalk it's Clear +61°F." in text


def test_registry_lists_morning_briefing():
    import main
    from core.action_loader import discover_actions

    reserved = {t["name"] for t in main.TOOL_DECLARATIONS}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    assert actions.has("morning_briefing")
