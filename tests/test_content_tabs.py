"""UI TABS — the content panel becomes News | Inbox | Calendar.

What is being pinned shut:

  * the tab structure: exactly News, Inbox, Calendar; News is the
    panel's original display and pushed content still lands there
    (show_content / the review renderer select it);
  * the data tabs FETCH NOTHING before they are first shown, load
    off-thread behind a visible "Loading…" state, and Refresh
    re-fetches;
  * the Inbox tab renders list_summaries newest-first with the
    triage classifier's labels tied to the right messages, and
    degrades honestly (unconfigured / fetch error / classifier
    down) in Phase E's language;
  * the Calendar tab renders the iCal feed chronologically, with
    the same honest unconfigured and error states.
"""
import inspect
import threading
import time
from datetime import datetime, timedelta

import core.gmail as gmail
import core.ical as ical
import core.panel_data as panel_data
import main as main_module
import ui as ui_module
from core import outcome
from memory import config_manager


# ── panel_data: inbox ────────────────────────────────────────────────

_ENTRIES = [
    {"uid": "3", "date": "Wed, 21 Oct 2026 09:41:00 -0400",
     "from": "BGE <bge@example.com>",
     "subject": "Your electric bill is ready"},
    {"uid": "2", "date": "Wed, 21 Oct 2026 08:15:00 -0400",
     "from": "Sam Reyes <sam@example.com>",
     "subject": "Newsletter notes from Sam"},
    {"uid": "1", "date": "Tue, 20 Oct 2026 22:02:00 -0400",
     "from": "Mega Store <deals@example.com>",
     "subject": "SALE: half off everything"},
]

_VERDICT = ("IMPORTANT (act now): electric bill is ready to pay\n"
            "FYI: newsletter notes from Sam\n"
            "NOISE (ignored): 1")


def _stub_inbox(monkeypatch, entries=_ENTRIES, verdict=_VERDICT):
    monkeypatch.setattr(gmail, "connected", lambda: True)
    monkeypatch.setattr(gmail, "list_summaries",
                        lambda **kw: (entries, None))
    monkeypatch.setattr(panel_data, "_classify",
                        lambda system, user_text: verdict)


def test_inbox_renders_labelled_newest_first(monkeypatch):
    _stub_inbox(monkeypatch)
    text = panel_data.fetch_inbox_text()
    lines = text.splitlines()
    assert lines[0] == "3 most recent emails, newest first:"
    body = [ln for ln in lines if "|" in ln]
    assert len(body) == 3
    # order is the reader's order — newest first, untouched
    assert "electric bill" in body[0]
    assert "Newsletter notes" in body[1]
    assert "SALE" in body[2]
    # labels tied to the right messages; the noise one unlabelled
    assert body[0].startswith("[IMPORTANT] ")
    assert body[1].startswith("[FYI] ")
    assert not body[2].startswith("[")


def test_inbox_unconfigured_state(monkeypatch):
    monkeypatch.setattr(gmail, "connected", lambda: False)
    text = panel_data.fetch_inbox_text()
    assert "Gmail isn't connected yet" in text
    assert "set up Gmail" in text


def test_inbox_error_state_shows_reason(monkeypatch):
    monkeypatch.setattr(gmail, "connected", lambda: True)
    err = outcome.failed("inbox_tab", "could not read Gmail: boom")
    monkeypatch.setattr(gmail, "list_summaries", lambda **kw: (None, err))
    text = panel_data.fetch_inbox_text()
    assert "Couldn't read your Gmail just now" in text
    assert "boom" in text
    assert "Refresh" in text


def test_inbox_empty(monkeypatch):
    _stub_inbox(monkeypatch, entries=[])
    assert "Inbox is empty" in panel_data.fetch_inbox_text()


def test_inbox_classifier_down_degrades_to_unlabelled(monkeypatch):
    def boom(system, user_text):
        raise RuntimeError("no answer")
    monkeypatch.setattr(gmail, "connected", lambda: True)
    monkeypatch.setattr(gmail, "list_summaries",
                        lambda **kw: (_ENTRIES, None))
    monkeypatch.setattr(panel_data, "_classify", boom)
    text = panel_data.fetch_inbox_text()
    assert "electric bill" in text          # the list still renders
    assert "[IMPORTANT]" not in text         # no invented labels
    assert "Triage labels unavailable" in text


# ── panel_data: calendar ─────────────────────────────────────────────

def _ics():
    now = datetime.now()
    d1, d2 = now + timedelta(days=1), now + timedelta(days=2)

    def stamp(d, h):
        return d.strftime("%Y%m%d") + f"T{h:02d}0000"

    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\n"
        "BEGIN:VEVENT\r\nUID:b\r\n"
        f"DTSTART:{stamp(d2, 10)}\r\nDTEND:{stamp(d2, 11)}\r\n"
        "SUMMARY:Later Event\r\nEND:VEVENT\r\n"
        "BEGIN:VEVENT\r\nUID:a\r\n"
        f"DTSTART:{stamp(d1, 9)}\r\nDTEND:{stamp(d1, 10)}\r\n"
        "SUMMARY:Earlier Event\r\nEND:VEVENT\r\n"
        "END:VCALENDAR\r\n")


def test_calendar_renders_chronological(monkeypatch):
    monkeypatch.setattr(config_manager, "get_ical_url",
                        lambda: "https://example.invalid/cal.ics")
    monkeypatch.setattr(ical, "fetch_ical", lambda url, timeout=20: _ics())
    text = panel_data.fetch_calendar_text()
    assert "Earlier Event" in text and "Later Event" in text
    assert text.index("Earlier Event") < text.index("Later Event")


def test_calendar_unconfigured_state(monkeypatch):
    monkeypatch.setattr(config_manager, "get_ical_url", lambda: "")
    text = panel_data.fetch_calendar_text()
    assert "Calendar isn't connected yet" in text
    assert "set up my calendar" in text


def test_calendar_error_state(monkeypatch):
    monkeypatch.setattr(config_manager, "get_ical_url",
                        lambda: "https://example.invalid/cal.ics")

    def boom(url, timeout=20):
        raise RuntimeError("socket gone")
    monkeypatch.setattr(ical, "fetch_ical", boom)
    text = panel_data.fetch_calendar_text()
    assert "Couldn't load your calendar just now" in text
    assert "socket gone" in text


# ── the ContentTabs widget ───────────────────────────────────────────

_APP = None   # held for the process: dropping the last Python
              # reference to the QApplication destroys the app,
              # and widget creation after that aborts.


def _app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


def _pump_until(pred, timeout=5.0):
    app = _app()
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if pred():
            return True
        time.sleep(0.01)
    app.processEvents()
    return pred()


def _view():
    _app()
    return ui_module.ContentTabs()


def test_tab_structure_and_news_is_the_original_display():
    view = _view()
    labels = [view.tabs.tabText(i) for i in range(view.tabs.count())]
    assert labels == ["News", "Inbox", "Calendar"]
    assert view.tabs.currentIndex() == 0
    assert view.tabs.widget(0) is view.news_display


def test_no_fetch_before_a_data_tab_is_shown():
    view = _view()
    calls = []
    view.set_providers(inbox=lambda: calls.append("inbox") or "IN",
                       calendar=lambda: calls.append("cal") or "CAL")
    _app().processEvents()
    assert calls == []
    assert view._displays["inbox"].toPlainText() == ""


def test_loading_state_precedes_data():
    view = _view()
    gate = threading.Event()

    def provider():
        gate.wait(10)
        return "INBOX DATA"
    view.set_providers(inbox=provider)
    view.tabs.setCurrentIndex(1)
    disp = view._displays["inbox"]
    assert disp.toPlainText() == "Loading…"
    assert not view._refresh_btns["inbox"].isEnabled()
    gate.set()
    assert _pump_until(lambda: disp.toPlainText() == "INBOX DATA")
    assert view._refresh_btns["inbox"].isEnabled()


def test_tab_fetches_once_then_refresh_refetches():
    view = _view()
    count = {"n": 0}

    def provider():
        count["n"] += 1
        return f"fetch {count['n']}"
    view.set_providers(calendar=provider)
    disp = view._displays["calendar"]
    view.tabs.setCurrentIndex(2)
    assert _pump_until(lambda: disp.toPlainText() == "fetch 1")
    view.tabs.setCurrentIndex(0)
    view.tabs.setCurrentIndex(2)          # already loaded — no refetch
    _app().processEvents()
    assert count["n"] == 1
    view._refresh_btns["calendar"].click()
    assert _pump_until(lambda: disp.toPlainText() == "fetch 2")


def test_provider_failure_is_an_honest_tab_state():
    view = _view()

    def boom():
        raise RuntimeError("kaput")
    view.set_providers(inbox=boom)
    view.tabs.setCurrentIndex(1)
    disp = view._displays["inbox"]
    assert _pump_until(lambda: "kaput" in disp.toPlainText())
    assert "Couldn't load this tab just now" in disp.toPlainText()


def test_unbound_tab_says_so():
    view = _view()
    view.tabs.setCurrentIndex(1)
    assert (view._displays["inbox"].toPlainText()
            == "This tab isn't available in this build.")


def test_show_news_returns_to_the_news_tab():
    view = _view()
    view.set_providers(calendar=lambda: "CAL")
    view.tabs.setCurrentIndex(2)
    assert _pump_until(
        lambda: view._displays["calendar"].toPlainText() == "CAL")
    view.show_news()
    assert view.tabs.currentIndex() == 0


# ── wiring pins ──────────────────────────────────────────────────────

def test_main_binds_panel_data_to_the_panel():
    src = inspect.getsource(main_module.JarvisLive.run)
    assert "panel_data" in src
    assert "set_content_tab_providers" in src
    assert "fetch_inbox_text" in src
    assert "fetch_calendar_text" in src


def test_show_content_and_review_select_the_news_tab():
    assert "show_news" in inspect.getsource(
        ui_module.MainWindow._show_content)
    assert "show_news" in inspect.getsource(
        ui_module.MainWindow._show_review)
    assert "ContentTabs" in inspect.getsource(
        ui_module.MainWindow._build_content_panel)
