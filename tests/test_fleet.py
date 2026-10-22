"""Fleet view (core/fleet.py) — the old ARIA Tasks view's rule,
restored: every row is runtime state, never model narration.

What is pinned here:

  * the activity registry: begin/finish/current/recent semantics,
    durations recorded, the finish-in-a-finally hook pattern, the
    tail cap, and never-raises behaviour on bad tokens;
  * snapshot composition from the owning modules' real surfaces
    (delegation records, scheduler rows, watches, routines, MCP
    status rows, the two loop ledgers), with each broken source
    degrading to its empty shape instead of sinking the view;
  * the header pill rule: hidden at zero, visible with the count
    above zero — and the count it shows is the snapshot's count;
  * the Fleet tab as the fourth content tab, rendering a running
    shell command's actual command line;
  * update_status carrying the fleet section from the same
    snapshot the tab paints.
"""
import json
import time

import pytest

import ui as ui_module
from actions import background_monitor, update_status
from core import delegation, fleet, mcp_client, routines, scheduler, \
    screenwatch


@pytest.fixture(autouse=True)
def _clean_registry():
    fleet.reset_for_tests()
    yield
    fleet.reset_for_tests()


# ── the activity registry ──────────────────────────────────────────

def test_begin_current_finish_recent():
    tok = fleet.begin("read_file", "")
    assert tok is not None
    cur = fleet.current()
    assert len(cur) == 1
    assert cur[0]["tool"] == "read_file"
    assert cur[0]["elapsed"] >= 0
    fleet.finish(tok, ok=True)
    assert fleet.current() == []
    tail = fleet.recent()
    assert len(tail) == 1
    assert tail[0]["tool"] == "read_file"
    assert tail[0]["ok"] is True
    assert tail[0]["duration"] >= 0


def test_finish_records_failure():
    tok = fleet.begin("web_search")
    fleet.finish(tok, ok=False)
    assert fleet.recent()[0]["ok"] is False


def test_finish_in_finally_when_the_call_raises():
    # The hook pattern in JarvisLive._execute_tool: begin, then
    # finish from a finally — an exception must still close the
    # record, as a failure.
    def call():
        tok = fleet.begin("run_shell_command", "git status")
        try:
            raise RuntimeError("boom")
        finally:
            fleet.finish(tok, ok=False)
    with pytest.raises(RuntimeError):
        call()
    assert fleet.current() == []
    assert fleet.recent()[0]["ok"] is False
    assert fleet.recent()[0]["detail"] == "git status"


def test_bad_tokens_are_noops_and_never_raise():
    fleet.finish(None)
    fleet.finish(999999)          # never begun
    tok = fleet.begin("x")
    fleet.finish(tok)
    fleet.finish(tok)             # already finished
    assert len(fleet.recent()) == 1
    assert fleet.begin(None) is not None   # junk tool name tolerated


def test_recent_tail_is_capped_newest_first():
    for i in range(fleet.RECENT_CAP + 5):
        fleet.finish(fleet.begin(f"tool{i}"))
    tail = fleet.recent()
    assert len(tail) == fleet.RECENT_CAP
    assert tail[0]["tool"] == f"tool{fleet.RECENT_CAP + 4}"
    assert len(fleet.recent(3)) == 3


def test_detail_for_shell_only_and_truncated():
    cmd = "git log --oneline " + "x" * 200
    detail = fleet.detail_for("run_shell_command", {"command": cmd})
    assert detail == cmd[:fleet.DETAIL_LIMIT]
    assert fleet.detail_for("read_file", {"command": "nope"}) == ""
    assert fleet.detail_for("run_shell_command", None) == ""
    assert fleet.detail_for("run_shell_command", {"command": ""}) == ""


def test_result_ok_reads_the_funnels_own_failure_lines():
    assert fleet.result_ok("status=done action=read_file") is True
    assert fleet.result_ok("status=unverified action=open_app") is True
    assert fleet.result_ok("plain prose result") is True
    assert fleet.result_ok("status=failed action=run_shell_command") \
        is False
    assert fleet.result_ok("Tool 'dj' failed: boom") is False
    assert fleet.result_ok("Unknown tool: frobnicate") is False
    assert fleet.result_ok(None) is True


# ── snapshot composition ───────────────────────────────────────────

_NOW = None


def _tasks(now):
    return [
        {"id": 3, "prompt": "research the supplier options",
         "status": "running", "result": "",
         "created": now - 300, "started": now - 240, "finished": None},
        {"id": 2, "prompt": "book the thing",
         "status": "blocked", "result": "",
         "created": now - 200, "started": now - 180, "finished": None},
        {"id": 1, "prompt": "summarise the inbox",
         "status": "done", "result": "ok",
         "created": now - 900, "started": now - 890,
         "finished": now - 800},
    ]


def _stub_sources(monkeypatch, tmp_path, tasks=None):
    now = time.time()
    monkeypatch.setattr(delegation, "list_tasks",
                        lambda limit=8: _tasks(now) if tasks is None
                        else tasks)
    _stub_rest(monkeypatch, tmp_path)
    return now


def _stub_rest(monkeypatch, tmp_path):
    now = time.time()
    monkeypatch.setattr(background_monitor, "list_monitors",
                        lambda: ["MTG Secret Lair drops"])
    monkeypatch.setattr(screenwatch, "list_watches", lambda: {
        "prices": {"interval_s": 300, "last_checked": "2026-10-22 10:00",
                   "question": "did the price drop?"}})
    monkeypatch.setattr(scheduler, "list_rows", lambda: [
        {"id": 2, "kind": "recurring", "prompt": "check the thing",
         "next_run": now + 600},
        {"id": 1, "kind": "timer", "prompt": "tea",
         "next_run": now + 3600}])
    monkeypatch.setattr(routines, "routine_names",
                        lambda: ["morning", "shutdown"])
    monkeypatch.setattr(routines, "replay_state",
                        lambda: {"replaying": False, "name": None,
                                 "since": None})
    monkeypatch.setattr(routines, "is_recording", lambda: False)
    monkeypatch.setattr(mcp_client, "status_rows", lambda: [
        {"name": "docs", "kind": "command", "target": "x",
         "state": "connected", "tools": 7, "error": ""},
        {"name": "old", "kind": "command", "target": "y",
         "state": "configured", "tools": 0, "error": ""}])
    cw = tmp_path / "codewatch.json"
    cw.write_text(json.dumps({
        "boot": {"head": "abcdef1234567890", "watched": 71},
        "checks": 5, "last_check_ts": now - 120,
        "restart_pending": False,
        "events": [{"kind": "change", "detail": "ui.py",
                    "ts": now - 60}]}), encoding="utf-8")
    monkeypatch.setattr(fleet, "CODEWATCH_PATH", cw)
    wd = tmp_path / "watchdog.json"
    wd.write_text(json.dumps({"restarts": [now - 600]}),
                  encoding="utf-8")
    monkeypatch.setattr(fleet, "WATCHDOG_PATH", wd)
    return now


def test_snapshot_composes_every_source(monkeypatch, tmp_path):
    now = _stub_sources(monkeypatch, tmp_path)
    tok = fleet.begin("run_shell_command", "git status")
    snap = fleet.snapshot()
    # 2 active delegated (running + blocked) + 1 in flight
    assert snap["running_count"] == 3
    assert snap["delegated_active"] == 2
    assert fleet.running_count() == snap["running_count"]
    # active delegated float to the top, newest first within group
    assert [r["id"] for r in snap["delegated"]] == [3, 2, 1]
    assert snap["delegated"][0]["elapsed"] == pytest.approx(240, abs=2)
    assert snap["delegated"][2]["elapsed"] == pytest.approx(90, abs=2)
    assert [r["tool"] for r in snap["shell"]] == ["run_shell_command"]
    assert snap["monitors"] == ["MTG Secret Lair drops"]
    assert snap["screen_watches"][0]["name"] == "prices"
    assert snap["schedule"]["total"] == 2
    assert snap["schedule"]["next"][0]["id"] == 2
    assert snap["routines"]["saved"] == 2
    assert snap["mcp"]["connected"] == [{"name": "docs", "tools": 7}]
    assert snap["mcp"]["not_connected"] == ["old"]
    assert snap["codewatch"]["boot_head"] == "abcdef1234567890"
    assert snap["codewatch"]["checks"] == 5
    assert snap["watchdog"]["restarts_last_hour"] == 1
    fleet.finish(tok)


def test_snapshot_renders_all_sections(monkeypatch, tmp_path):
    _stub_sources(monkeypatch, tmp_path)
    fleet.begin("run_shell_command", "git status")
    text = fleet.render_text(fleet.snapshot())
    for section in ("RUNNING", "SHELL", "WATCHES & SCHEDULE",
                    "SYSTEM", "RECENT"):
        assert section in text
    assert text.index("RUNNING") < text.index("RECENT")
    assert "#3 [running] research the supplier options" in text
    assert "#2 [blocked] book the thing" in text
    assert "git status" in text            # the actual command
    assert "MTG Secret Lair drops" in text
    assert "Screen watch 'prices'" in text
    assert "Code watcher: booted abcdef123456" in text
    assert "Watchdog: 1 restart in the last hour" in text
    assert "MCP: 'docs' connected (7 tools)" in text
    assert "Routines: 2 saved" in text


def test_snapshot_missing_and_corrupt_ledgers_degrade(monkeypatch,
                                                      tmp_path):
    _stub_sources(monkeypatch, tmp_path)
    missing = tmp_path / "nope.json"
    monkeypatch.setattr(fleet, "CODEWATCH_PATH", missing)
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(fleet, "WATCHDOG_PATH", corrupt)
    snap = fleet.snapshot()               # must not raise
    assert snap["codewatch"] is None
    assert snap["watchdog"]["restarts_last_hour"] == 0
    assert "Code watcher: no record yet" in fleet.render_text(snap)


def test_snapshot_broken_source_costs_only_its_section(monkeypatch,
                                                      tmp_path):
    _stub_sources(monkeypatch, tmp_path)

    def boom(limit=8):
        raise RuntimeError("store gone")
    monkeypatch.setattr(delegation, "list_tasks", boom)
    snap = fleet.snapshot()               # must not raise
    assert snap["delegated"] == []
    assert snap["schedule"]["total"] == 2  # other sources intact


def test_empty_snapshot_says_so_plainly():
    snap = {
        "now": time.time(), "running_count": 0,
        "delegated": [], "delegated_active": 0,
        "inflight": [], "shell": [], "recent": [],
        "monitors": [], "screen_watches": [],
        "schedule": {"total": 0, "next": []},
        "routines": {"saved": 0, "replaying": None,
                     "recording": False},
        "mcp": {"connected": [], "not_connected": [], "failed": []},
        "codewatch": None,
        "watchdog": {"restarts_last_hour": 0, "last_restart_ts": None,
                     "beats": {}, "enabled": None},
    }
    text = fleet.render_text(snap)
    assert ("Nothing running. If she says she's working in the "
            "background, it will appear here.") in text
    assert "No tool calls recorded yet this session." in text
    assert fleet.render_summary(snap).startswith(
        "Fleet: nothing is running")


# ── the header pill ────────────────────────────────────────────────

_APP = None


def _app():
    global _APP
    from PyQt6.QtWidgets import QApplication
    _APP = QApplication.instance() or QApplication([])
    return _APP


class _Pill:
    def __init__(self):
        self.text = ""
        self.visible = False

    def setText(self, t):
        self.text = t

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False


def _bare_window():
    win = ui_module.MainWindow.__new__(ui_module.MainWindow)
    win._tasks_pill = _Pill()
    win._content_tabs_view = None
    return win


def test_pill_hidden_at_zero_visible_with_count(monkeypatch):
    _app()
    win = _bare_window()
    monkeypatch.setattr(fleet, "running_count", lambda now=None: 0)
    win._refresh_fleet_pill()
    assert win._tasks_pill.visible is False
    monkeypatch.setattr(fleet, "running_count", lambda now=None: 3)
    win._refresh_fleet_pill()
    assert win._tasks_pill.visible is True
    assert win._tasks_pill.text == "TASKS: 3"
    monkeypatch.setattr(fleet, "running_count", lambda now=None: 0)
    win._refresh_fleet_pill()
    assert win._tasks_pill.visible is False


def test_pill_tick_refreshes_the_fleet_tab_only_when_visible(
        monkeypatch):
    _app()
    view = ui_module.ContentTabs()
    calls = {"n": 0}

    def provider():
        calls["n"] += 1
        return f"render {calls['n']}"
    view.set_providers(fleet=provider)
    win = _bare_window()
    win._content_tabs_view = view
    monkeypatch.setattr(fleet, "running_count", lambda now=None: 1)
    win._refresh_fleet_pill()            # News in front: no render
    assert calls["n"] == 0
    view.tabs.setCurrentIndex(3)         # showing the tab renders it
    assert calls["n"] == 1
    win._refresh_fleet_pill()            # ticking while visible re-renders
    assert calls["n"] == 2
    assert view._displays["fleet"].toPlainText() == "render 2"


# ── the Fleet tab ──────────────────────────────────────────────────

def _view():
    _app()
    return ui_module.ContentTabs()


def test_fleet_is_the_fourth_tab():
    view = _view()
    labels = [view.tabs.tabText(i) for i in range(view.tabs.count())]
    assert labels == ["News", "Inbox", "Calendar", "Fleet"]
    assert view.header_title("NEWS — TOP") == "NEWS — TOP"
    view.tabs.setCurrentIndex(3)
    assert view.header_title("NEWS — TOP") == "Fleet"


def test_unbound_fleet_tab_says_so():
    view = _view()
    view.tabs.setCurrentIndex(3)
    assert (view._displays["fleet"].toPlainText()
            == "This tab isn't available in this build.")


def test_fleet_tab_renders_a_running_shell_command(monkeypatch,
                                                   tmp_path):
    _stub_sources(monkeypatch, tmp_path)
    fleet.begin("run_shell_command",
                fleet.detail_for("run_shell_command",
                                 {"command": "git status"}))
    view = _view()
    view.set_providers(fleet=lambda: fleet.render_text(fleet.snapshot()))
    view.tabs.setCurrentIndex(3)
    text = view._displays["fleet"].toPlainText()
    assert "RUNNING" in text
    assert "SHELL" in text
    assert "git status" in text


# ── update_status speaks the same snapshot ─────────────────────────

def test_update_status_carries_the_fleet_section(monkeypatch, tmp_path):
    # Every source stubbed except delegation itself: the task
    # record comes through the real store path, end to end.
    _stub_rest(monkeypatch, tmp_path)
    now = time.time()
    store = tmp_path / "delegated_tasks.json"
    store.write_text(json.dumps({
        "next_id": 4,
        "tasks": [{"id": 3, "prompt": "research the supplier options",
                   "status": "running", "result": "",
                   "created": now - 300, "started": now - 240,
                   "finished": None}]}), encoding="utf-8")
    monkeypatch.setattr(delegation, "STORE_PATH", store)
    fleet.begin("read_file")
    snap = fleet.snapshot()
    assert snap["running_count"] == 2
    out = update_status.update_status()
    assert "fleet_running=2" in out
    assert "Fleet: 2 running" in out
    assert "delegated task #3" in out
    assert "research the supplier options" in out
