"""Phase E2 — the calendar connection, ARIA's iCal semantics pinned.

The parser fixture is ARIA's own (her tests/test_headless.py sample:
a UTC event, an all-day event, a TZID event with a folded summary)
plus a recurring event, and it pins the behaviour she shipped with:

  * UTC, TZID and date-only forms all parse; escaped commas and
    folded lines come through as text; a LOCATION joins the summary.
  * Recurring events ARE expanded (Phase H3) — an RRULE event
    appears at every occurrence inside the window. This REPLACED
    ARIA's documented not-expanded limitation in the H3 commit;
    the pin below was rewritten in that same commit to pin the
    new behaviour, so the change cannot drift silently either.
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


def test_recurring_event_expands_weekly():
    """Phase H3's pin, replacing ARIA's not-expanded one in the
    same commit that changed the behaviour: parse still yields
    ONE series event, and upcoming() expands it — Oct 2, 9 and
    16 inside a 21-day window (Oct 23 falls past the window's
    end)."""
    evs = ical.parse_ical(ARIA_SAMPLE)
    standups = [e for e in evs if e["summary"] == "Weekly standup"]
    assert len(standups) == 1                      # one series
    assert standups[0]["rrule"] == "FREQ=WEEKLY"
    now = datetime(2026, 10, 2, 12, 0)
    up = ical.upcoming(evs, days=21, now=now)      # three weeks of window
    starts = [e["start"] for e in up
              if e["summary"] == "Weekly standup"]
    assert len(starts) == 3
    assert [s.day for s in starts] == [2, 9, 16]


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


# ── Phase H3: RRULE expansion ────────────────────────────────────────────────

def _ics(*events):
    body = "".join(
        "BEGIN:VEVENT\r\n" + "".join(f"{line}\r\n" for line in lines)
        + "END:VEVENT\r\n"
        for lines in events)
    return "BEGIN:VCALENDAR\r\n" + body + "END:VCALENDAR\r\n"


def _starts(ics_text, days, now):
    evs = ical.parse_ical(ics_text)
    return [e["start"] for e in ical.upcoming(evs, days=days, now=now)]


NOW = datetime(2026, 10, 2, 12, 0)   # a Friday, local noon


def test_count_cutoff():
    ics = _ics(["DTSTART:20261002T130000Z", "DTEND:20261002T140000Z",
                "RRULE:FREQ=WEEKLY;COUNT=2", "SUMMARY:Standup"])
    starts = _starts(ics, days=60, now=NOW)   # window far past COUNT
    assert [s.day for s in starts] == [2, 9]


def test_until_cutoff():
    ics = _ics(["DTSTART:20261002T130000Z", "DTEND:20261002T140000Z",
                "RRULE:FREQ=WEEKLY;UNTIL=20261016T130000Z",
                "SUMMARY:Standup"])
    starts = _starts(ics, days=60, now=NOW)
    assert [s.day for s in starts] == [2, 9, 16]   # UNTIL is inclusive


def test_exdate_removes_one_occurrence():
    ics = _ics(["DTSTART:20261002T130000Z", "DTEND:20261002T140000Z",
                "RRULE:FREQ=WEEKLY", "EXDATE:20261009T130000Z",
                "SUMMARY:Standup"])
    starts = _starts(ics, days=21, now=NOW)
    assert [s.day for s in starts] == [2, 16]


def test_multiple_exdate_lines_accumulate():
    ics = _ics(["DTSTART:20261002T130000Z", "DTEND:20261002T140000Z",
                "RRULE:FREQ=WEEKLY", "EXDATE:20261009T130000Z",
                "EXDATE:20261016T130000Z", "SUMMARY:Standup"])
    starts = _starts(ics, days=28, now=NOW)
    assert [s.day for s in starts] == [2, 23]


def test_daily_interval_and_window_boundary():
    ics = _ics(["DTSTART:20261001T130000Z", "DTEND:20261001T140000Z",
                "RRULE:FREQ=DAILY;INTERVAL=2", "SUMMARY:Every other"])
    # occurrences Sep 29... no: Oct 1, 3, 5, 7 — the window opens
    # Oct 2, so Oct 1's occurrence only counts if it overlaps
    # (13:00 + 1h on Oct 1 does not reach Oct 2 00:00 local in
    # UTC/EDT frames) — expect 3, 5, 7 inside.
    starts = _starts(ics, days=6, now=NOW)
    assert [s.day for s in starts] == [3, 5, 7]


def test_monthly_recurrence():
    ics = _ics(["DTSTART:20260915T130000Z", "DTEND:20260915T140000Z",
                "RRULE:FREQ=MONTHLY", "SUMMARY:Monthly review"])
    later = datetime(2026, 12, 1, 12, 0)
    starts = _starts(ics, days=46, now=later)
    assert [(s.month, s.day) for s in starts] == [(12, 15), (1, 15)]


def test_rdate_adds_an_off_pattern_occurrence():
    ics = _ics(["DTSTART:20261002T130000Z", "DTEND:20261002T140000Z",
                "RRULE:FREQ=WEEKLY;COUNT=1",
                "RDATE:20261005T130000Z", "SUMMARY:Standup + extra"])
    starts = _starts(ics, days=14, now=NOW)
    assert [s.day for s in starts] == [2, 5]


def test_occurrence_cap_enforced(monkeypatch):
    monkeypatch.setattr(ical, "MAX_OCCURRENCES", 5)
    ics = _ics(["DTSTART:20261002T130000Z", "DTEND:20261002T140000Z",
                "RRULE:FREQ=DAILY", "SUMMARY:Daily"])
    starts = _starts(ics, days=21, now=NOW)
    assert len(starts) == 5


def test_all_day_weekly_with_date_exdate():
    ics = _ics(["DTSTART:20261005", "DTEND:20261006",
                "RRULE:FREQ=WEEKLY", "EXDATE:20261012",
                "SUMMARY:Day off"])
    now = datetime(2026, 10, 5, 12, 0)
    evs = ical.parse_ical(ics)
    up = ical.upcoming(evs, days=21, now=now)
    days = [e["start"].day for e in up if e["summary"] == "Day off"]
    assert days == [5, 19]                    # the 12th is excluded
    assert all(e["all_day"] for e in up if e["summary"] == "Day off")


def test_malformed_rrule_degrades_to_first_occurrence():
    ics = _ics(["DTSTART:20261002T130000Z", "DTEND:20261002T140000Z",
                "RRULE:FREQ=NONSENSE;BLAH=1", "SUMMARY:Broken rule"])
    starts = _starts(ics, days=21, now=NOW)
    assert len(starts) == 1 and starts[0].day == 2


def test_tzid_recurrence_keeps_wall_time():
    ics = _ics(["DTSTART;TZID=America/New_York:20261002T160000",
                "DTEND;TZID=America/New_York:20261002T170000",
                "RRULE:FREQ=WEEKLY;COUNT=3", "SUMMARY:Call"])
    starts = _starts(ics, days=21, now=NOW)
    assert len(starts) == 3
    # 16:00 New York every week — as local-naive starts they read
    # 16:00 in New York's frame; in a UTC sandbox that is 20:00.
    # The invariant that matters: same wall time each week in the
    # event's own zone, 7 days apart.
    assert (starts[1] - starts[0]).days == 7
    assert (starts[2] - starts[1]).days == 7
