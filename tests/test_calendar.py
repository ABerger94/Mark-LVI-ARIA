"""Phase E2 — the calendar connection, ARIA's iCal semantics pinned.

The parser fixture is ARIA's own (her tests/test_headless.py sample:
a UTC event, an all-day event, a TZID event with a folded summary)
plus a recurring event, and it pins the behaviour she shipped with:

  * UTC, TZID and date-only forms all parse; escaped commas and
    folded lines come through as text; a LOCATION joins the summary.
  * Recurring events are NOT expanded — an RRULE event appears once,
    at its DTSTART. That is ARIA's documented limitation, pinned so
    it cannot change silently in either direction.
  * upcoming() windows on local [today, +days) and localises.

Above the parser: calendar_setup validates like ARIA's did and the
URL round-trips through the config store; check_calendar without a
URL is the clean not-connected outcome; with a stubbed feed it lists
ARIA's line format.
"""
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import actions.calendar_setup as cs
import actions.check_calendar as cc
from core import ical
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent

# ARIA's fixture (tests/test_headless.py::test_ical_parse), plus one
# recurring event to pin the not-expanded rule.
ARIA_SAMPLE = (
    "BEGIN:VCALENDAR\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART:20261002T200000Z\r\n"
    "DTEND:20261003T010000Z\r\n"
    "SUMMARY:Dock of the Bay \\, Wait\r\n"
    "LOCATION:Sparrows Point\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART:20261005\r\n"
    "DTEND:20261006\r\n"
    "SUMMARY:Day off\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART;TZID=America/New_York:20261006T160000\r\n"
    "SUMMARY:Long description that folds \r\n"
    " over two lines\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "DTSTART:20261002T130000Z\r\n"
    "DTEND:20261002T140000Z\r\n"
    "RRULE:FREQ=WEEKLY\r\n"
    "SUMMARY:Weekly standup\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    return path


# ── the parser, ARIA's expectations ──────────────────────────────────────────

def test_parse_aria_sample():
    evs = ical.parse_ical(ARIA_SAMPLE)
    assert len(evs) == 4, evs
    assert evs[0]["summary"] == "Dock of the Bay , Wait @ Sparrows Point"
    assert evs[0]["start"].strftime("%Y-%m-%d %H:%M") == "2026-10-02 20:00"
    assert evs[1]["all_day"] is True
    assert (evs[1]["end"] - evs[1]["start"]).days == 1
    assert evs[2]["summary"] == "Long description that folds over two lines"
    # The TZID event keeps its zone: 16:00 in New York, not 16:00 UTC.
    assert evs[2]["start"].utcoffset() is not None
    assert evs[2]["start"].hour == 16


def test_recurring_event_is_not_expanded():
    evs = ical.parse_ical(ARIA_SAMPLE)
    standups = [e for e in evs if e["summary"] == "Weekly standup"]
    assert len(standups) == 1                      # DTSTART only
    now = datetime(2026, 10, 2, 12, 0)
    up = ical.upcoming(evs, days=21, now=now)      # three weeks of window
    assert [e["summary"] for e in up].count("Weekly standup") == 1


def test_upcoming_windows_like_aria():
    evs = ical.parse_ical(ARIA_SAMPLE)
    now = datetime(2026, 10, 2, 12, 0)
    up = ical.upcoming(evs, days=1, now=now)
    summaries = {e["summary"] for e in up}
    assert "Dock of the Bay , Wait @ Sparrows Point" in summaries
    assert "Weekly standup" in summaries
    assert "Day off" not in summaries
    up4 = ical.upcoming(evs, days=4, now=now)
    assert any(e["summary"] == "Day off" for e in up4)
    assert not any("Long description" in e["summary"] for e in up4)


# ── setup + the action ───────────────────────────────────────────────────────

def test_setup_validation_and_round_trip(cfg):
    bad = cs.calendar_setup({"ical_url": "not a url"})
    assert bad.startswith("status=failed")
    assert "doesn't look like" in bad
    url = ("https://calendar.google.com/calendar/ical/"
           "abc123/private/basic.ics")
    ok = cs.calendar_setup({"ical_url": url})
    assert ok.startswith("status=done")
    assert cm.get_ical_url() == url


def test_check_calendar_not_connected(cfg):
    result = cc.check_calendar({})
    assert result.startswith("status=failed")
    assert "calendar_setup" in result


def test_check_calendar_lists_events(cfg, monkeypatch):
    cm.save_ical_url("https://example.invalid/basic.ics")
    today = datetime.now().replace(hour=0, minute=0, second=0,
                                   microsecond=0)
    start = (today + timedelta(days=1)).strftime("%Y%m%d")
    feed = (
        "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
        f"DTSTART:{start}T150000Z\r\nDTEND:{start}T160000Z\r\n"
        "SUMMARY:Dentist\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
    monkeypatch.setattr(ical, "fetch_ical", lambda url: feed)
    result = cc.check_calendar({"days": 2})
    assert result.startswith("status=done"), result
    assert "Dentist" in result and "- " in result
    # Today only: the same feed is empty news, said plainly.
    result1 = cc.check_calendar({"days": 1})
    assert "Nothing on the calendar" in result1


def test_check_calendar_fetch_failure_is_a_failure(cfg, monkeypatch):
    cm.save_ical_url("https://example.invalid/basic.ics")

    def boom(url):
        raise OSError("network unreachable")

    monkeypatch.setattr(ical, "fetch_ical", boom)
    result = cc.check_calendar({})
    assert result.startswith("status=failed")
    assert "could not fetch" in result


def test_registry_lists_the_calendar_tools():
    import main
    from core.action_loader import discover_actions

    reserved = {t["name"] for t in main.TOOL_DECLARATIONS}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    assert actions.has("calendar_setup")
    assert actions.has("check_calendar")
