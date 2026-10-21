"""Phase F-REST group 5 — the in-app scheduler (ARIA's, ported).

Everything fires off tick(now) with an injected clock — no test
sleeps for a duration it is testing. Pinned shut:
  * ARIA's duration grammar ('1h30m', bare number = minutes);
  * a task fires ONLY when its delivery hook says the line went
    out — undelivered one-shots stay due and retry, never get
    eaten (the reconnect case);
  * recurring tasks advance from fire time and re-fire;
  * timers and the break toggle PERSIST (ARIA kept both in memory
    only — here the store is re-read from disk to prove it);
  * the list is honest about its boundary: OS-scheduler reminders
    are separate and the listing says so.
  * main.py's wiring is present (bind beside routines, the tick
    task in the session TaskGroup beside proactive mode).
"""
import inspect

import pytest

from core import scheduler
from actions import set_timer as timer_action
from actions import set_recurring_task as recurring_action
from actions import list_scheduled_tasks as list_action
from actions import cancel_scheduled_task as cancel_action
from actions import break_reminders as break_action


T0 = 1_800_000_000.0  # a fixed epoch; all firing math is relative


@pytest.fixture(autouse=True)
def _store(tmp_path, monkeypatch):
    monkeypatch.setattr(scheduler, "STORE_PATH", tmp_path / "scheduler.json")
    said, ran = [], []

    def _say(text):
        said.append(text)
        return True

    def _run(text):
        ran.append(text)
        return True

    scheduler.bind(say=_say, run_prompt=_run)
    yield said, ran
    scheduler.bind(None, None)


# ── duration grammar ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,seconds", [
    ("20 minutes", 1200),
    ("1h30m", 5400),
    ("90s", 90),
    ("1 hour", 3600),
    ("45", 2700),          # bare number = minutes (ARIA's rule)
    ("2m 30s", 150),
    ("nonsense", 0),
    ("", 0),
])
def test_parse_duration_aria_grammar(text, seconds):
    assert scheduler.parse_duration(text) == seconds


# ── timers ───────────────────────────────────────────────────────────────────

def test_timer_fires_once_with_aria_line(_store):
    said, _ran = _store
    tid, secs = scheduler.set_timer("20 minutes", "pasta", now=T0)
    assert secs == 1200
    assert scheduler.tick(T0 + 1199) == []
    logs = scheduler.tick(T0 + 1200)
    assert said == ["Timer done: pasta."]
    assert any(f"timer #{tid} fired" in line for line in logs)
    # Consumed: a later tick says nothing more.
    assert scheduler.tick(T0 + 9999) == []
    assert scheduler.list_rows() == []


def test_timer_action_round_trip(_store):
    result = timer_action.set_timer(
        {"duration_text": "1h30m", "label": "laundry"})
    assert "status=done" in result and "seconds=5400" in result
    assert "Timer set: laundry, 1h30m from now." in result
    bad = timer_action.set_timer({"duration_text": "whenever"})
    assert "status=failed" in bad


def test_timer_persists_in_the_store(_store):
    scheduler.set_timer("10 minutes", "tea", now=T0)
    rows = scheduler.list_rows()   # re-read from disk, not memory
    assert len(rows) == 1 and rows[0]["kind"] == "timer"
    assert rows[0]["prompt"] == "tea"


# ── delivery discipline ──────────────────────────────────────────────────────

def test_undelivered_timer_is_not_eaten(_store, monkeypatch):
    scheduler.set_timer("5 minutes", "eggs", now=T0)
    monkeypatch.setattr(scheduler, "_say_hook", lambda text: False)
    logs = scheduler.tick(T0 + 301)
    assert any("undelivered" in line for line in logs)
    assert len(scheduler.list_rows()) == 1     # still due
    # When delivery recovers, it fires.
    said, _ = _store
    monkeypatch.setattr(scheduler, "_say_hook", lambda text: (said.append(text), True)[1])
    scheduler.tick(T0 + 302)
    assert said == ["Timer done: eggs."]
    assert scheduler.list_rows() == []


def test_unbound_scheduler_fires_nothing(_store, monkeypatch):
    scheduler.set_timer("5 minutes", "eggs", now=T0)
    monkeypatch.setattr(scheduler, "_say_hook", None)
    scheduler.tick(T0 + 600)
    assert len(scheduler.list_rows()) == 1


# ── recurring tasks ──────────────────────────────────────────────────────────

def test_recurring_fires_and_advances(_store):
    _said, ran = _store
    tid, kind = scheduler.set_recurring(3600, "check the build", now=T0)
    assert kind == "interval"
    scheduler.tick(T0 + 3600)
    assert ran == ["[Scheduled task] check the build"]
    rows = scheduler.list_rows()
    assert rows[0]["next_run"] == T0 + 7200
    scheduler.tick(T0 + 7200)
    assert len(ran) == 2


def test_recurring_brief_classification(_store):
    _tid, kind = scheduler.set_recurring(3600, "give me my morning brief",
                                         now=T0)
    assert kind == "brief"
    result = recurring_action.set_recurring_task(
        {"interval_seconds": 3600, "prompt": "morning briefing please"})
    assert "Morning-brief task set" in result


def test_recurring_min_interval_clamp(_store):
    tid, _kind = scheduler.set_recurring(5, "too fast", now=T0)
    row = scheduler.list_rows()[0]
    assert row["interval_s"] == 60
    assert row["next_run"] == T0 + 60


def test_recurring_action_validation(_store):
    assert "status=failed" in recurring_action.set_recurring_task(
        {"interval_seconds": 0, "prompt": "x"})
    assert "status=failed" in recurring_action.set_recurring_task(
        {"interval_seconds": 60, "prompt": "  "})


# ── list / cancel ────────────────────────────────────────────────────────────

def test_list_and_cancel(_store):
    scheduler.set_timer("10 minutes", "tea", now=T0)
    scheduler.set_recurring(600, "check mail", now=T0)
    result = list_action.list_scheduled_tasks({})
    assert "count=2" in result
    assert "[timer]" in result and "[interval]" in result
    # The OS boundary is stated on the listing itself.
    assert "OS task" in result and "separate" in result

    rows = scheduler.list_rows()
    tid = rows[0]["id"]
    assert "status=done" in cancel_action.cancel_scheduled_task(
        {"task_id": tid})
    assert scheduler.count() == 1
    assert "status=failed" in cancel_action.cancel_scheduled_task(
        {"task_id": tid})


def test_empty_list_is_honest(_store):
    result = list_action.list_scheduled_tasks({})
    assert "No scheduled tasks." in result
    assert "OS task" in result


# ── break reminders ──────────────────────────────────────────────────────────

def test_break_toggle_persists_and_nudges_on_cadence(_store):
    said, _ran = _store
    result = break_action.break_reminders({"enabled": True})
    assert "enabled" in result
    assert scheduler.break_reminders_enabled() is True   # re-read from disk
    # The cadence is measured from the stamp the toggle wrote.
    last = scheduler._load()["last_break_nudge"]
    assert last is not None
    # No nudge before a full interval has passed since enabling.
    scheduler.tick(last + scheduler.BREAK_INTERVAL_S - 1)
    assert said == []
    scheduler.tick(last + scheduler.BREAK_INTERVAL_S)
    assert len(said) == 1 and "Break reminder" in said[0]
    # And not again until the next interval.
    scheduler.tick(last + scheduler.BREAK_INTERVAL_S + 60)
    assert len(said) == 1

    break_action.break_reminders({"enabled": False})
    assert scheduler.break_reminders_enabled() is False
    scheduler.tick(last + 3 * scheduler.BREAK_INTERVAL_S)
    assert len(said) == 1


def test_break_toggle_via_core_stamps_enable_time(_store):
    scheduler.set_break_reminders(True, now=T0)
    data = scheduler._load()
    assert data["last_break_nudge"] == T0


# ── main.py wiring ───────────────────────────────────────────────────────────

def test_main_wires_the_scheduler():
    import main
    src = inspect.getsource(main)
    assert "scheduler as scheduler_core" in src
    assert "scheduler_core.bind(say=self._scheduler_say" in src
    assert "tg.create_task(self._run_scheduler())" in src
    assert "tg.create_task(self._run_proactive_mode())" in src
    # The scheduler task starts beside proactive, both session-scoped.
    assert src.index("tg.create_task(self._run_scheduler())") > \
        src.index("tg.create_task(self._run_proactive_mode())")
