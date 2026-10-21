"""Phase F-REST group 6 — screen watches (ARIA's Module 7, ported).

Vision is stubbed at check_watch's ask seam — no display, no API.
Pinned shut:
  * the comparator keeps ARIA's 0.85 rule (near-identical rewording
    is NOT a change; a different answer is);
  * the first check sets the baseline and stays quiet; a change
    speaks through the scheduler's one bound hook;
  * a failed check changes NO state — a broken poll must not become
    the baseline;
  * watch/unwatch create and cancel the real scheduler task, and
    the scheduler tick drives check_watch on schedule.
"""
import pytest

from core import scheduler, screenwatch
from actions import watch_screen as watch_action
from actions import unwatch_screen as unwatch_action
from actions import list_screen_watches as list_action


T0 = 1_800_000_000.0


@pytest.fixture(autouse=True)
def _stores(tmp_path, monkeypatch):
    monkeypatch.setattr(screenwatch, "STORE_PATH",
                        tmp_path / "screen_watches.json")
    monkeypatch.setattr(scheduler, "STORE_PATH",
                        tmp_path / "scheduler.json")
    said = []
    scheduler.bind(say=lambda t: (said.append(t), True)[1],
                   run_prompt=lambda t: True)
    yield said
    scheduler.bind(None, None)


# ── the comparator ───────────────────────────────────────────────────────────

def test_answers_differ_aria_rule():
    assert screenwatch.answers_differ("same", "same") is False
    assert screenwatch.answers_differ("", "anything") is True
    assert screenwatch.answers_differ("The build is running", "") is True
    # A light rewording stays under the threshold.
    assert screenwatch.answers_differ(
        "The build is still running.", "The build is still running") is False
    # A genuinely different answer crosses it.
    assert screenwatch.answers_differ(
        "The build is still running", "The build finished successfully") \
        is True


# ── creating watches ─────────────────────────────────────────────────────────

def test_watch_creates_scheduler_task(_stores):
    result = watch_action.watch_screen(
        {"name": "build", "question": "Has the build finished?",
         "interval_s": 120})
    assert "status=done" in result and "interval_s=120" in result
    rows = scheduler.list_rows()
    assert len(rows) == 1
    assert rows[0]["kind"] == "screenwatch" and rows[0]["prompt"] == "build"
    watches = screenwatch.list_watches()
    assert watches["build"]["sched_task_id"] == rows[0]["id"]


def test_watch_validation(_stores):
    assert "status=failed" in watch_action.watch_screen(
        {"name": "x", "question": "q", "interval_s": 30})      # too fast
    assert "status=failed" in watch_action.watch_screen(
        {"name": "x", "question": "  "})                        # no question
    assert "status=failed" in watch_action.watch_screen(
        {"name": "!!!", "question": "q"})                       # no valid name
    watch_action.watch_screen({"name": "dup", "question": "q?"})
    assert "status=failed" in watch_action.watch_screen(
        {"name": "dup", "question": "again?"})                  # duplicate


def test_watch_name_sanitised_aria_style(_stores):
    # ARIA strips invalid characters (it does not underscore them).
    assert screenwatch.sanitize_name("My Build Watch!") == "mybuildwatch"


# ── checking ─────────────────────────────────────────────────────────────────

def _make_watch():
    screenwatch.watch_screen("orders", "What is the order status?", 300)


def test_first_check_sets_baseline_quietly(_stores):
    _make_watch()
    result = screenwatch.check_watch("orders", ask=lambda q: "Order shipped")
    assert "no change" in result
    assert _stores == []
    watch = screenwatch.list_watches()["orders"]
    assert watch["last_answer"] == "Order shipped"
    assert watch["last_checked"] is not None


def test_change_speaks_through_the_scheduler_hook(_stores):
    _make_watch()
    screenwatch.check_watch("orders", ask=lambda q: "Order shipped")
    result = screenwatch.check_watch(
        "orders", ask=lambda q: "Order delivered to the front door")
    assert "change detected" in result
    assert len(_stores) == 1
    assert "orders" in _stores[0] and "delivered" in _stores[0]
    # Same answer again: quiet.
    result = screenwatch.check_watch(
        "orders", ask=lambda q: "Order delivered to the front door")
    assert "no change" in result and len(_stores) == 1


def test_failed_check_changes_no_state(_stores):
    _make_watch()
    screenwatch.check_watch("orders", ask=lambda q: "Order shipped")

    def _boom(q):
        raise RuntimeError("no display")

    result = screenwatch.check_watch("orders", ask=_boom)
    assert "Screen check failed" in result
    watch = screenwatch.list_watches()["orders"]
    assert watch["last_answer"] == "Order shipped"


def test_check_unknown_watch(_stores):
    assert "No screen watch named" in screenwatch.check_watch(
        "ghost", ask=lambda q: "x")


def test_tick_drives_the_check(_stores, monkeypatch):
    _make_watch()
    monkeypatch.setattr(screenwatch, "ask_screen",
                        lambda q: "Order shipped")
    row = scheduler.list_rows()[0]
    logs = scheduler.tick(row["next_run"] + 1)
    watch = screenwatch.list_watches()["orders"]
    assert watch["last_answer"] == "Order shipped"
    # The task advanced instead of re-firing every tick.
    assert scheduler.list_rows()[0]["next_run"] > row["next_run"]


# ── unwatch / list ───────────────────────────────────────────────────────────

def test_unwatch_cancels_task(_stores):
    _make_watch()
    assert scheduler.count() == 1
    result = unwatch_action.unwatch_screen({"name": "orders"})
    assert "status=done" in result
    assert screenwatch.list_watches() == {}
    assert scheduler.count() == 0
    assert "status=failed" in unwatch_action.unwatch_screen(
        {"name": "orders"})


def test_list_watches(_stores):
    empty = list_action.list_screen_watches({})
    assert "No active screen watches" in empty
    _make_watch()
    result = list_action.list_screen_watches({})
    assert "count=1" in result
    assert "orders" in result and "What is the order status?" in result
