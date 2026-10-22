"""Phase R1 — the main-process watchdog (core/watchdog.py).

What is being pinned shut:

  * a healthy pair of heartbeats never produces a verdict — the
    watchdog's ordinary day is silence;
  * a source that has never beaten is measured from the arm stamp,
    so startup gets its grace and a source that stops beating after
    having beaten is measured from its last beat;
  * a wedged loop (or a wedged UI) past the stale threshold yields a
    restart verdict, an incident written BEFORE the restart, and a
    ledger entry;
  * the loop guard: three restarts inside the hour exhaust the
    budget — the verdict becomes give_up, one gave_up incident is
    recorded per episode, and no further ledger entries accrue;
  * restarts older than the window prune away, so a healthy process
    earns its budget back;
  * check_once invokes the bound restart callback on a restart
    verdict and on nothing else;
  * the config flag round-trips and defaults on.
"""
import json

import pytest

from core import incidents, watchdog
from memory import config_manager as cm


@pytest.fixture()
def wd(tmp_path, monkeypatch):
    """A watchdog with a tmp ledger, recorded incidents, and a
    frozen arm stamp at mono=1000.0."""
    state = tmp_path / "watchdog.json"
    monkeypatch.setattr(watchdog, "STATE_PATH", state)
    seen = []
    monkeypatch.setattr(
        incidents, "record",
        lambda source, kind, message: seen.append((source, kind, message)))
    monkeypatch.setattr(watchdog, "RESTART_SETTLE_S", 0)
    watchdog.reset_for_tests()
    watchdog._arm_mono = 1000.0
    yield {"state": state, "incidents": seen}
    watchdog.reset_for_tests()


EPOCH = 1_700_000_000.0


def _beat(source, at):
    watchdog._beats[source] = at


# ── healthy days ────────────────────────────────────────────────────────

def test_unarmed_is_silent(wd):
    watchdog.disarm()
    assert watchdog.evaluate(5000.0, EPOCH) == (None, "not armed")


def test_fresh_beats_never_trigger(wd):
    _beat("loop", 1020.0)
    _beat("ui", 1029.0)
    decision, _ = watchdog.evaluate(1030.0, EPOCH)
    assert decision is None
    assert wd["incidents"] == []
    assert not wd["state"].exists()


def test_never_beaten_source_gets_arm_grace(wd):
    _beat("loop", 1029.0)          # ui has never beaten
    decision, _ = watchdog.evaluate(1030.0, EPOCH)   # 30s after arm
    assert decision is None        # ui measured from arm: exactly at
    # the threshold is not past it
    decision, _ = watchdog.evaluate(1031.0, EPOCH)
    assert decision == "restart"   # 31s of ui silence: wedged


# ── wedges ──────────────────────────────────────────────────────────────

def test_wedged_loop_restarts_and_records_first(wd):
    _beat("loop", 1005.0)
    _beat("ui", 1090.0)
    decision, detail = watchdog.evaluate(1091.0, EPOCH)
    assert decision == "restart"
    assert "loop" in detail
    kinds = [k for _s, k, _m in wd["incidents"]]
    assert kinds == ["watchdog_restart"]
    ledger = json.loads(wd["state"].read_text(encoding="utf-8"))
    assert ledger["restarts"] == [EPOCH]


def test_wedged_ui_restarts(wd):
    _beat("loop", 1090.0)
    _beat("ui", 1005.0)
    decision, detail = watchdog.evaluate(1091.0, EPOCH)
    assert decision == "restart"
    assert "ui" in detail


def test_check_once_invokes_the_restart_callback(wd):
    calls = []
    watchdog._restart_cb = lambda: calls.append("restart")
    _beat("loop", 1005.0)
    _beat("ui", 1090.0)
    assert watchdog.check_once(now_mono=1091.0, now_epoch=EPOCH) \
        == "restart"
    assert calls == ["restart"]


def test_check_once_healthy_invokes_nothing(wd):
    calls = []
    watchdog._restart_cb = lambda: calls.append("restart")
    _beat("loop", 1090.0)
    _beat("ui", 1090.0)
    assert watchdog.check_once(now_mono=1091.0, now_epoch=EPOCH) is None
    assert calls == []


# ── the loop guard ──────────────────────────────────────────────────────

def test_loop_guard_caps_restarts(wd):
    wd["state"].write_text(
        json.dumps({"restarts": [EPOCH - 100, EPOCH - 50, EPOCH - 10]}),
        encoding="utf-8")
    _beat("loop", 1005.0)          # wedged
    _beat("ui", 1090.0)
    decision, _ = watchdog.evaluate(1091.0, EPOCH)
    assert decision == "give_up"
    kinds = [k for _s, k, _m in wd["incidents"]]
    assert kinds == ["watchdog_gave_up"]
    # no fourth restart was ledgered
    ledger = json.loads(wd["state"].read_text(encoding="utf-8"))
    assert len(ledger["restarts"]) == 3
    # a second pass in the same episode does not re-record
    watchdog.evaluate(1092.0, EPOCH)
    kinds = [k for _s, k, _m in wd["incidents"]]
    assert kinds == ["watchdog_gave_up"]


def test_old_restarts_prune_out_of_the_window(wd):
    old = EPOCH - watchdog.RESTART_WINDOW_S - 60
    wd["state"].write_text(
        json.dumps({"restarts": [old, old, old]}), encoding="utf-8")
    _beat("loop", 1005.0)
    _beat("ui", 1090.0)
    decision, _ = watchdog.evaluate(1091.0, EPOCH)
    assert decision == "restart"
    ledger = json.loads(wd["state"].read_text(encoding="utf-8"))
    assert ledger["restarts"] == [EPOCH]


def test_health_resets_the_gave_up_episode(wd):
    wd["state"].write_text(
        json.dumps({"restarts": [EPOCH - 10] * 3}), encoding="utf-8")
    _beat("loop", 1005.0)
    _beat("ui", 1090.0)
    assert watchdog.evaluate(1091.0, EPOCH)[0] == "give_up"
    _beat("loop", 1091.0)          # she recovered on her own
    assert watchdog.evaluate(1092.0, EPOCH)[0] is None
    # budget is still spent within the window — recovery alone does
    # not mint new restarts; the window prunes them, in time
    _beat("loop", 1005.0)
    assert watchdog.evaluate(1093.0, EPOCH)[0] == "give_up"
    kinds = [k for _s, k, _m in wd["incidents"]]
    assert kinds.count("watchdog_gave_up") == 2   # a NEW episode


# ── arming ──────────────────────────────────────────────────────────────

def test_arm_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(watchdog, "STATE_PATH", tmp_path / "w.json")
    watchdog.reset_for_tests()
    try:
        assert watchdog.arm(restart=lambda: None) is True
        assert watchdog.arm(restart=lambda: None) is True
    finally:
        watchdog.reset_for_tests()


# ── the config flag ─────────────────────────────────────────────────────

def test_watchdog_flag_defaults_on_and_round_trips(tmp_path, monkeypatch):
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    assert cm.get_watchdog_enabled() is True
    cm.save_watchdog_enabled(False)
    assert cm.get_watchdog_enabled() is False
    cm.save_watchdog_enabled(True)
    assert cm.get_watchdog_enabled() is True
