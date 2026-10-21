"""Phase G4 — the proactive joins: existing engines, wired to speech.

Nothing here is a new engine. The scheduler already ticked, the
price pass already ran hourly, screen watches already judged, and
the briefing composer already composed. What was missing was the
joins, and each is pinned where it was made:

  * the daily briefing: a scheduler task of kind "briefing" (a
    time of day + an enabled flag in the record) whose due action
    is main's briefing hook — the startup greeting's own
    composition and delivery, so the salutation is the composer's
    time-aware one (pinned in test_briefing / test_startup_briefing)
    and never the tool's fixed "Good morning";
  * price crossings: check_watches used to latch `alerted` once,
    forever — a watch that dropped, recovered and dropped again
    could never speak twice. The record now carries the side of
    the target the price sits on; rising re-arms, re-crossing
    speaks again, staying below stays silent;
  * the speech itself: watch hits go through speak_alert(),
    gated by config's proactive_alerts flag (off = recorded and
    logged, not spoken), and through main's awake-gated say hook —
    asleep means queued, and wake() delivers the queue once.

Seams, as elsewhere: tick(now) with an injected clock, stores on
tmp paths, fetch/ask stubbed, JarvisLive built with __new__ and
only the attributes the hooks read.
"""
import asyncio
import inspect
import json
from datetime import datetime

import pytest

import main
from core import pricecheck, scheduler, screenwatch
from memory import config_manager as cm

T0 = 1_800_000_000.0  # a fixed epoch; all firing math is relative


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture()
def sched(tmp_path, monkeypatch):
    """Scheduler store on tmp + recording hooks. The briefing
    hook's verdict is controllable via the returned dict."""
    monkeypatch.setattr(scheduler, "STORE_PATH",
                        tmp_path / "scheduler.json")
    said, ran, briefed = [], [], []
    state = {"briefing_ok": True}

    def _briefing():
        briefed.append(True)
        return state["briefing_ok"]

    scheduler.bind(say=lambda t: (said.append(t), True)[1],
                   run_prompt=lambda t: (ran.append(t), True)[1],
                   briefing=_briefing)
    yield {"said": said, "ran": ran, "briefed": briefed,
           "state": state}
    scheduler.bind(None, None)


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    return path


@pytest.fixture()
def prices(tmp_path, monkeypatch):
    monkeypatch.setattr(pricecheck, "STORE_PATH",
                        tmp_path / "price_watches.json")
    pages = {}
    monkeypatch.setattr(pricecheck, "fetch_page",
                        lambda url: pages[url])
    return pages


def _page(price):
    return f'<html><script type="application/ld+json">' \
           f'{{"price": "{price:.2f}"}}</script></html>'


# ── daily time grammar ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,want", [
    ("7:30", (7, 30)), ("07:30", (7, 30)), ("19:30", (19, 30)),
    ("7:30 AM", (7, 30)), ("7:30 PM", (19, 30)), ("7 PM", (19, 0)),
    ("12 AM", (0, 0)), ("12 PM", (12, 0)), ("12:15 am", (0, 15)),
])
def test_parse_daily_time(text, want):
    assert scheduler.parse_daily_time(text) == want


@pytest.mark.parametrize("text", ["24:00", "7:60", "13 PM", "teatime",
                                  "", "7:5", None])
def test_parse_daily_time_rejects(text):
    assert scheduler.parse_daily_time(text) is None


# ── the briefing task record ─────────────────────────────────────────────────

def test_briefing_task_round_trip(sched, tmp_path):
    tid, normalised = scheduler.set_daily_briefing("7:30 AM", now=T0)
    assert normalised == "07:30"
    # Straight off the disk: the record carries its whole contract.
    raw = json.loads((tmp_path / "scheduler.json").read_text())
    (task,) = [t for t in raw["tasks"] if t["id"] == tid]
    assert task["kind"] == "briefing"
    assert task["time"] == "07:30"
    assert task["enabled"] is True
    assert T0 < task["next_run"] <= T0 + 86400
    # And the module reads its own writing back.
    got = scheduler.get_briefing_task()
    assert got["id"] == tid and got["time"] == "07:30"


def test_briefing_next_run_is_the_next_occurrence(sched):
    local = datetime.fromtimestamp(T0)
    later = f"{(local.hour + 1) % 24:02d}:{local.minute:02d}"
    tid, _ = scheduler.set_daily_briefing(later, now=T0)
    task = scheduler.get_briefing_task()
    assert task["next_run"] == pytest.approx(T0 + 3600, abs=2)
    earlier = f"{(local.hour - 1) % 24:02d}:{local.minute:02d}"
    scheduler.set_daily_briefing(earlier, now=T0)
    task = scheduler.get_briefing_task()
    assert task["id"] == tid                       # moved, not stacked
    assert T0 + 23 * 3600 - 2 <= task["next_run"] <= T0 + 86400


def test_briefing_bad_time_creates_nothing(sched):
    assert scheduler.set_daily_briefing("teatime", now=T0) == (None, None)
    assert scheduler.get_briefing_task() is None
    assert scheduler.set_briefing_enabled(False, now=T0) is False


# ── the briefing task firing ─────────────────────────────────────────────────

def _due_briefing(now=T0):
    tid, _ = scheduler.set_daily_briefing("7:30 AM", now=now - 90000)
    return tid


def test_briefing_due_fires_hook_once_and_advances(sched):
    tid = _due_briefing()
    logs = scheduler.tick(T0)
    assert sched["briefed"] == [True]
    assert any(f"briefing #{tid} fired" in line for line in logs)
    task = scheduler.get_briefing_task()
    assert task["next_run"] > T0                   # tomorrow, not +2s
    scheduler.tick(T0)                             # same instant again
    assert sched["briefed"] == [True]              # no double fire


def test_briefing_not_due_is_quiet(sched):
    scheduler.set_daily_briefing("7:30 AM", now=T0)
    logs = scheduler.tick(T0)
    assert sched["briefed"] == []
    assert not any("briefing" in line for line in logs)


def test_briefing_undelivered_stays_due_and_retries(sched):
    tid = _due_briefing()
    sched["state"]["briefing_ok"] = False          # no session / asleep
    before = scheduler.get_briefing_task()["next_run"]
    logs = scheduler.tick(T0)
    assert any("undelivered" in line for line in logs)
    assert scheduler.get_briefing_task()["next_run"] == before
    sched["state"]["briefing_ok"] = True
    scheduler.tick(T0 + 2)                         # a later tick retries
    assert sched["briefed"] == [True, True]
    assert scheduler.get_briefing_task()["next_run"] > T0


def test_briefing_disabled_skips_and_advances(sched):
    tid = _due_briefing()
    assert scheduler.set_briefing_enabled(False, now=T0 - 100)
    # disabling re-arms from its own moment; make it due again
    data = scheduler._load()
    data["tasks"][0]["next_run"] = T0 - 10
    scheduler._save(data)
    logs = scheduler.tick(T0)
    assert sched["briefed"] == []                  # never delivered
    assert any("disabled" in line for line in logs)
    task = scheduler.get_briefing_task()
    assert task["next_run"] > T0                   # advanced silently
    assert "(disabled)" in task["prompt"]          # the list is honest


# ── price crossings ──────────────────────────────────────────────────────────

def _price_task(now=T0):
    scheduler.add_task("pricecheck", "pricecheck",
                       interval_s=3600, now=now - 3600)


def test_price_crossing_speaks_once_per_crossing(sched, prices):
    url = "https://example.com/widget"
    pricecheck.add_watch(url, 50.0, "Widget")
    _price_task()

    prices[url] = _page(60.0)                      # above: silent
    scheduler.tick(T0)
    assert sched["said"] == []
    watch = pricecheck.list_watches()[0]
    assert watch["side"] == "above" and watch["alerted"] is False

    prices[url] = _page(45.0)                      # the crossing
    scheduler.tick(T0 + 3600)
    assert sched["said"] == [
        "Price alert: Widget is $45.00, at or below your "
        "$50.00 target."]
    watch = pricecheck.list_watches()[0]
    assert watch["side"] == "below" and watch["alerted"] is True

    prices[url] = _page(44.0)                      # still below: silent
    scheduler.tick(T0 + 7200)
    assert len(sched["said"]) == 1

    prices[url] = _page(55.0)                      # rise re-arms
    scheduler.tick(T0 + 10800)
    assert len(sched["said"]) == 1
    watch = pricecheck.list_watches()[0]
    assert watch["side"] == "above" and watch["alerted"] is False

    prices[url] = _page(40.0)                      # a NEW crossing speaks
    scheduler.tick(T0 + 14400)
    assert len(sched["said"]) == 2
    assert "$40.00" in sched["said"][1]


def test_price_hits_are_structured_and_report_unchanged(prices):
    url = "https://example.com/gadget"
    pricecheck.add_watch(url, 10.0, "Gadget")
    hits = []
    summary, alerts = pricecheck.check_watches(
        fetch=lambda u: _page(9.5), hits=hits)
    assert hits == [{"label": "Gadget", "price": 9.5,
                     "target": 10.0, "url": url}]
    assert "DROP:" in alerts[0] and "DROP:" in summary
    assert pricecheck.spoken_alert(hits[0]) == (
        "Price alert: Gadget is $9.50, at or below your $10.00 "
        "target.")


def test_price_alert_muted_by_flag_logs_instead(sched, cfg, prices):
    cm.save_proactive_alerts(False)
    assert scheduler.proactive_alerts_enabled() is False
    url = "https://example.com/widget"
    pricecheck.add_watch(url, 50.0, "Widget")
    _price_task()
    prices[url] = _page(45.0)
    logs = scheduler.tick(T0)
    assert sched["said"] == []                     # nothing spoken
    assert any("not spoken (proactive alerts off)" in line
               for line in logs)                   # but logged
    watch = pricecheck.list_watches()[0]           # and still recorded
    assert watch["last_price"] == 45.0 and watch["alerted"] is True


# ── screen-watch hits ────────────────────────────────────────────────────────

@pytest.fixture()
def screens(tmp_path, monkeypatch, sched):
    monkeypatch.setattr(screenwatch, "STORE_PATH",
                        tmp_path / "screen_watches.json")
    return sched


def test_screen_hit_speaks_once_then_baseline_moves(screens):
    ok, _msg, _w = screenwatch.watch_screen(
        "build", "has the build finished?", 60)
    assert ok
    first = screenwatch.check_watch(
        "build", ask=lambda q: "The build is still running.")
    assert "no change" in first                    # baseline, not a hit
    assert screens["said"] == []
    hit = screenwatch.check_watch(
        "build",
        ask=lambda q: "The build finished successfully and every "
                      "single test in the suite passed.")
    assert "change detected" in hit
    assert len(screens["said"]) == 1
    assert "build" in screens["said"][0]
    again = screenwatch.check_watch(
        "build",
        ask=lambda q: "The build finished successfully and every "
                      "single test in the suite passed.")
    assert "no change" in again                    # spoken once only
    assert len(screens["said"]) == 1


def test_screen_hit_muted_by_flag_is_marked_not_spoken(screens, cfg):
    cm.save_proactive_alerts(False)
    screenwatch.watch_screen("deploy", "is the deploy live?", 60)
    screenwatch.check_watch("deploy",
                            ask=lambda q: "Deployment is pending.")
    result = screenwatch.check_watch(
        "deploy",
        ask=lambda q: "Deployment completed: version 9 is live "
                      "for all users now.")
    assert "change detected" in result
    assert "not spoken" in result                  # the log carries it
    assert screens["said"] == []


# ── main's joins: the awake gate and the briefing hook ──────────────────────

class _FakeUI:
    muted = False

    def __init__(self):
        self.logs = []
        self.states = []

    def write_log(self, msg):
        self.logs.append(msg)

    def set_state(self, state):
        self.states.append(state)


class _FakeSession:
    def __init__(self):
        self.sent = []

    async def send_client_content(self, turns=None, turn_complete=True):
        self.sent.append(turns["parts"][0]["text"])


def _live(awake=True, session=True):
    live = main.JarvisLive.__new__(main.JarvisLive)
    live.ui = _FakeUI()
    live.session = _FakeSession() if session else None
    live._awake = awake
    live._loop = None
    live._failover_notice = None
    live._pending_announce = []
    live.said = []
    live.plugin_say = live.said.append
    return live


def test_say_hook_speaks_and_logs_when_awake():
    live = _live(awake=True)
    assert live._scheduler_say("Timer done: pasta.") is True
    assert len(live.said) == 1 and "pasta" in live.said[0]
    assert "SYS: Timer done: pasta." in live.ui.logs


def test_say_hook_without_session_refuses():
    live = _live(session=False)
    assert live._scheduler_say("Timer done: pasta.") is False
    assert live._pending_announce == [] and live.said == []


def test_say_hook_queues_while_asleep_and_wake_delivers_once():
    live = _live(awake=False)
    assert live._scheduler_say("Price alert: Widget is $45.00.") \
        is True                                    # accepted, not lost
    assert live.said == []                         # but NOT spoken
    assert live._pending_announce == ["Price alert: Widget is $45.00."]
    assert "SYS: Price alert: Widget is $45.00." in live.ui.logs
    live.wake(reason="wake word")
    assert len(live.said) == 1 and "Widget" in live.said[0]
    assert live._pending_announce == []
    live._awake = False                            # sleep, wake again:
    live.wake(reason="wake word")                  # nothing repeats
    assert len(live.said) == 1


def test_briefing_hook_delivery_gates():
    assert _live(session=False)._scheduler_briefing() is False
    assert _live(awake=False)._scheduler_briefing() is False
    live = _live(awake=True)                       # loop not running
    assert live._scheduler_briefing() is False


def test_briefing_hook_schedules_the_shared_delivery(monkeypatch):
    live = _live(awake=True)
    live._loop = object()                          # only truthiness read
    scheduled = []
    real = main.asyncio.run_coroutine_threadsafe

    def _fake(coro, loop):
        scheduled.append(coro)
        return None

    monkeypatch.setattr(main.asyncio, "run_coroutine_threadsafe",
                        _fake)
    try:
        assert live._scheduler_briefing() is True
    finally:
        monkeypatch.setattr(main.asyncio,
                            "run_coroutine_threadsafe", real)
    (coro,) = scheduled
    assert coro.cr_code.co_name == "_send_startup_briefing"
    coro.close()


def test_scheduled_briefing_delivers_the_composed_turn(monkeypatch):
    """The scheduled path IS the startup path: fake the composer,
    and one [STARTUP_BRIEFING] turn carrying its text goes out —
    the time-aware salutation is the composer's own business
    (pinned in the briefing suites)."""
    live = _live(awake=True)
    calls = []

    async def _fake_compose(**kwargs):
        calls.append(kwargs)
        return "Good evening, Alek. Today is Wednesday.", None

    monkeypatch.setattr(main._briefing, "compose_startup_briefing",
                        _fake_compose)
    monkeypatch.setattr(main, "load_memory",
                        lambda: {"identity": {}})
    monkeypatch.setattr(main, "list_monitors", lambda: [])
    asyncio.run(live._send_startup_briefing())
    assert len(calls) == 1
    assert len(live.session.sent) == 1
    turn = live.session.sent[0]
    assert turn.startswith("[STARTUP_BRIEFING]")
    assert "Good evening, Alek." in turn


def test_main_wires_the_briefing_hook():
    src = inspect.getsource(main)
    assert "briefing=self._scheduler_briefing" in src
    assert "def _flush_pending_announce" in src
