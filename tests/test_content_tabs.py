"""UI TABS — the content panel becomes News | Inbox | Calendar.

What is being pinned shut:

  * the tab structure: exactly News, Inbox, Calendar, Fleet; News
    is the panel's original display and pushed content still lands
    there (show_content / the review renderer select it);
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


# ── panel_data: fetch_inbox (rich) ───────────────────────────────────

def _rich_entries():
    now = datetime.now()
    def stamp(dt):
        return dt.strftime("%a, %d %b %Y %H:%M:00 -0400")
    return [
        {"uid": "4", "date": stamp(now - timedelta(hours=1)),
         "from": "BGE <bge@example.com>", "seen": False,
         "subject": "Your electric bill is ready"},
        {"uid": "3", "date": stamp(now - timedelta(hours=3)),
         "from": "Sam Reyes <sam@example.com>", "seen": True,
         "subject": "Newsletter notes from Sam"},
        {"uid": "2", "date": stamp(now - timedelta(days=1)),
         "from": "Mega Store <deals@example.com>", "seen": True,
         "subject": "SALE: half off everything"},
        {"uid": "1", "date": stamp(now - timedelta(days=4)),
         "from": "Mum <mum@example.com>", "seen": False,
         "subject": "Sunday dinner?"},
    ]


def _stub_inbox_rich(monkeypatch, entries=None, verdict=_VERDICT,
                     count=(2, None)):
    entries = _rich_entries() if entries is None else entries
    monkeypatch.setattr(gmail, "connected", lambda: True)
    monkeypatch.setattr(gmail, "list_summaries",
                        lambda **kw: (entries, None))
    monkeypatch.setattr(gmail, "unread_count", lambda **kw: count)
    monkeypatch.setattr(panel_data, "_classify",
                        lambda system, user_text: verdict)
    return entries


def test_fetch_inbox_rich_blocks_and_count(monkeypatch):
    _stub_inbox_rich(monkeypatch)
    result = panel_data.fetch_inbox()
    assert result["unread"] == 2
    # the plain rendering of the same fetch rides along, unchanged
    assert result["text"].splitlines()[0] == \
        "4 most recent emails, newest first:"
    html = result["html"]
    # blocks: sender, subject, the IMPORTANT chip as a chip
    assert "BGE &lt;bge@example.com&gt;" in html
    assert "Your electric bill is ready" in html
    assert "&nbsp;IMPORTANT&nbsp;" in html
    assert "Newsletter notes from Sam" in html
    # no flat pipe lines anywhere in the rich rendering
    assert " | " not in html


def test_fetch_inbox_day_groups_newest_first(monkeypatch):
    entries = _stub_inbox_rich(monkeypatch)
    html = panel_data.fetch_inbox()["html"]
    older = entries[3]
    moment = panel_data._entry_moment(older)
    older_label = panel_data._day_label(moment.date(),
                                        datetime.now().date())
    assert html.index("Today") < html.index("Yesterday") \
        < html.index(older_label)
    # within Today, the newer message's block comes first
    assert html.index("electric bill") < html.index("Newsletter notes")


def test_fetch_inbox_unread_distinction_follows_seen_flag(monkeypatch):
    _stub_inbox_rich(monkeypatch)
    html = panel_data.fetch_inbox()["html"]
    # exactly the two seen=False entries get the unread dot
    assert html.count("●") == 2
    # and an unread subject is bolded; a read one is not
    i = html.index("Your electric bill is ready")
    assert "<b>" in html[max(0, i - 80):i]
    j = html.index("Newsletter notes from Sam")
    assert "<b>" not in html[max(0, j - 80):j]


def test_fetch_inbox_escapes_markup_in_mail_text(monkeypatch):
    evil = [{"uid": "9", "date": "Wed, 21 Oct 2026 09:41:00 -0400",
             "from": "Evil <script@x.example>", "seen": False,
             "subject": "<b>fake</b> & <script>alert(1)</script>"}]
    _stub_inbox_rich(monkeypatch, entries=evil, verdict="",
                     count=(1, None))
    html = panel_data.fetch_inbox()["html"]
    assert "&lt;b&gt;fake&lt;/b&gt;" in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "<script>" not in html
    assert "<b>fake</b>" not in html


def test_fetch_inbox_unconfigured_is_a_quiet_block(monkeypatch):
    monkeypatch.setattr(gmail, "connected", lambda: False)
    result = panel_data.fetch_inbox()
    assert result["unread"] is None          # never an invented zero
    assert "Gmail isn't connected yet" in result["html"]
    assert "set up Gmail" in result["html"]
    assert result["text"].startswith("Gmail isn't connected yet")


def test_fetch_inbox_count_failure_degrades_to_unknown(monkeypatch):
    _stub_inbox_rich(monkeypatch, count=(None, "status=failed x"))
    result = panel_data.fetch_inbox()
    assert result["unread"] is None
    assert "electric bill" in result["html"]   # the list still renders


def test_fetch_inbox_empty_still_counts(monkeypatch):
    _stub_inbox_rich(monkeypatch, entries=[], count=(0, None))
    result = panel_data.fetch_inbox()
    assert result["unread"] == 0
    assert "Inbox is empty" in result["html"]


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
    assert labels == ["News", "Inbox", "Calendar", "Fleet"]
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
    assert "_panel_data.fetch_inbox" in src
    assert "fetch_calendar_text" in src


# ── rich results + the dynamic panel header ──────────────────────────

def _rich_result(unread=3, html="<b>rich inbox</b>", text="plain inbox"):
    return {"text": text, "html": html, "unread": unread}


def test_dict_result_renders_html_and_feeds_the_header():
    view = _view()
    view.set_providers(inbox=lambda: _rich_result(unread=3))
    assert view.header_title("NEWS — TOP") == "NEWS — TOP"
    view.tabs.setCurrentIndex(1)
    # in flight, the count is not a count yet — bare title
    assert view.header_title("NEWS — TOP") == "Gmail Inbox"
    disp = view._displays["inbox"]
    assert _pump_until(lambda: "rich inbox" in disp.toHtml())
    assert (view.header_title("NEWS — TOP")
            == "Gmail Inbox - 3 Unread Emails")
    view.tabs.setCurrentIndex(2)
    assert view.header_title("NEWS — TOP") == "Calendar"
    view.tabs.setCurrentIndex(0)
    assert view.header_title("NEWS — TOP") == "NEWS — TOP"


def test_header_singular_and_unknown_counts():
    view = _view()
    view.set_providers(inbox=lambda: _rich_result(unread=1))
    view.tabs.setCurrentIndex(1)
    assert _pump_until(lambda: view._inbox_unread == 1)
    assert view.header_title("N") == "Gmail Inbox - 1 Unread Email"

    view2 = _view()
    view2.set_providers(inbox=lambda: _rich_result(unread=None, html=""))
    view2.tabs.setCurrentIndex(1)
    disp2 = view2._displays["inbox"]
    assert _pump_until(lambda: disp2.toPlainText() == "plain inbox")
    assert view2.header_title("N") == "Gmail Inbox"


def test_refresh_recounts_and_drops_the_stale_count_while_loading():
    view = _view()
    gate = threading.Event()
    counts = iter([3, 5])

    def provider():
        n = next(counts)
        if n == 5:
            gate.wait(10)
        return _rich_result(unread=n)
    view.set_providers(inbox=provider)
    view.tabs.setCurrentIndex(1)
    assert _pump_until(lambda: view._inbox_unread == 3)
    assert view.header_title("N") == "Gmail Inbox - 3 Unread Emails"
    view._refresh_btns["inbox"].click()
    # mid-refresh the old number must not linger in the header
    assert view.header_title("N") == "Gmail Inbox"
    gate.set()
    assert _pump_until(lambda: view._inbox_unread == 5)
    assert view.header_title("N") == "Gmail Inbox - 5 Unread Emails"


def test_header_changed_fires_on_switch_and_on_data():
    view = _view()
    seen = []
    view.header_changed.connect(lambda: seen.append(1))
    view.set_providers(inbox=lambda: _rich_result(unread=2))
    view.tabs.setCurrentIndex(1)
    assert _pump_until(lambda: view._inbox_unread == 2)
    assert len(seen) >= 2          # the switch, and the count landing


def test_mainwindow_header_wiring():
    build = inspect.getsource(ui_module.MainWindow._build_content_panel)
    assert "header_changed" in build
    assert "_content_news_title" in build
    sync = inspect.getsource(ui_module.MainWindow._sync_content_title)
    assert "header_title" in sync
    show = inspect.getsource(ui_module.MainWindow._show_content)
    assert "_content_news_title" in show
    review = inspect.getsource(ui_module.MainWindow._show_review)
    assert "_content_news_title" in review


def test_show_content_and_review_select_the_news_tab():
    assert "show_news" in inspect.getsource(
        ui_module.MainWindow._show_content)
    assert "show_news" in inspect.getsource(
        ui_module.MainWindow._show_review)
    assert "ContentTabs" in inspect.getsource(
        ui_module.MainWindow._build_content_panel)
