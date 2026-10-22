"""
core/watchdog.py — the supervisor that notices when she has wedged.

WHY THIS EXISTS
    The failure this build inherited from the old assistant is a main
    process that stops answering without dying: a COM call wedges a
    thread, the window stays on screen, the session stays "connected",
    and nothing anywhere says a word — the user finds out by talking
    to a corpse. The stall watcher in main.py covers ONE shape of
    that (a Live session that stops answering while the loop itself
    is alive). This module covers the deeper one: a loop that can no
    longer run at all.

WHAT IT WATCHES
    Two heartbeats, because the app has two loops and either can
    wedge on its own:

      "loop" — the asyncio thread. JarvisLive.run() starts a
               heartbeat coroutine that beats every 2 seconds for
               the life of the process, outside the session
               TaskGroup, so reconnects and session rebuilds do not
               starve it. Everything the assistant DOES rides this
               loop; if it cannot run a 2-second sleep, it can do
               nothing.
      "ui"   — the Qt thread. The main window's 1-second clock timer
               beats from _tick_clock: a handler that provably runs
               only while Qt's event loop is turning. The COM-freeze
               signature — a pycaw call wedging the thread that owns
               the window — stops this beat first.

    Beats are cheap stamps in a dict. Nothing in either loop waits
    on this module.

THE THRESHOLD
    STALE_S = 30 — roughly fifteen missed beats on either source.
    Long work does not move it: tool calls run on executor threads,
    network calls carry their own timeouts and run off-loop (the
    session summary, the self-edit smoke test), and the loop's own
    awaits are queue waits and short sleeps. A loop that cannot beat
    for 30 seconds is not busy; it is wedged. (The one deliberate
    exception: a source that has never beaten is measured from the
    arm stamp, so startup — the API-key wait, the first session —
    gets the same 30 seconds before it can be called wedged.)

THE RESPONSE
    Detection + restart, nothing more. On a wedged verdict the
    supervisor (a daemon thread of its own — it must live somewhere
    neither loop can take down with it):

      1. records an incident, kind "watchdog_restart", naming the
         wedged source and how long it has been silent — BEFORE
         acting, so the evidence exists even if the restart fails;
      2. appends the restart to the ledger (below);
      3. calls the restart callback main.py bound — the self-edit
         relaunch's shape (spawn the replacement first, then
         os._exit) but NOT routed through the event loop, because
         the loop is the thing that is wedged. The session summary
         the self-edit restart saves is deliberately skipped: a
         wedged loop cannot produce it, and waiting for it would
         wait forever. The incident record is the evidence instead.

    This is NOT the shelved COM process isolation: no COM moves
    anywhere, no helper process appears. The watchdog assumes the
    wedge will recur and makes it survivable instead — a restart
    with a record, versus a corpse with none.

THE LOOP GUARD
    A watchdog that restarts a deterministically-wedging app forever
    is a fork bomb with good intentions. Restarts are ledgered in
    memory/watchdog.json (atomic write, the stores' own idiom):
    at most MAX_RESTARTS (3) inside RESTART_WINDOW_S (an hour). Past
    that the supervisor stays its hand, records ONE
    "watchdog_gave_up" incident per episode, and leaves the app down
    — a human reads the incident and decides. The ledger lives on
    disk precisely so the guard survives the restarts it authorises;
    a healthy process outlives the window and the count prunes away
    on its own.

    The whole module never raises: it sits in the failure path of
    last resort, and a watchdog that can crash is worse than none.
    Alek can disable it entirely with "watchdog_enabled": false in
    config — main.py checks before arming.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from pathlib import Path

CHECK_INTERVAL_S = 2.0    # the supervisor's own poll
STALE_S = 30.0            # silence longer than this is a wedge
MAX_RESTARTS = 3          # per window, then a human decides
RESTART_WINDOW_S = 3600.0
RESTART_SETTLE_S = 15.0   # after asking for a restart, let it happen

SOURCES = ("loop", "ui")


def _base_dir() -> Path:
    # Same rule as core/scheduler.py: beside the executable when
    # frozen, the repo root when running from source.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch this constant to a tmp path (the scheduler pattern).
STATE_PATH = _base_dir() / "memory" / "watchdog.json"

_lock = threading.Lock()
_beats: dict[str, float] = {}
_arm_mono: float | None = None
_restart_cb = None
_supervisor: threading.Thread | None = None
_gave_up_episode = False


# ── Heartbeats ─────────────────────────────────────────────────────────────

def beat(source: str) -> None:
    """Stamp `source` alive. Called from the asyncio loop's
    heartbeat coroutine and the window's clock tick; costs a dict
    write under a lock, never blocks, never raises."""
    try:
        with _lock:
            _beats[source] = time.monotonic()
    except Exception:
        pass


def last_beat(source: str) -> float | None:
    with _lock:
        return _beats.get(source)


# ── The restart ledger ─────────────────────────────────────────────────────

def _ledger_load() -> list[float]:
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        stamps = data.get("restarts") if isinstance(data, dict) else None
        if isinstance(stamps, list):
            return [float(t) for t in stamps
                    if isinstance(t, (int, float))]
    except Exception:
        pass
    return []


def _ledger_save(stamps: list[float]) -> None:
    """Atomic, the stores' idiom (temp file in the same directory +
    os.replace). Guarded: a ledger that cannot be written costs the
    guard its memory, never the app its supervisor."""
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_PATH.with_name(STATE_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"restarts": stamps}),
                       encoding="utf-8")
        os.replace(tmp, STATE_PATH)
    except Exception:
        pass


def _recent_restarts(now_epoch: float) -> list[float]:
    return [t for t in _ledger_load()
            if now_epoch - t < RESTART_WINDOW_S]


# ── The verdict ────────────────────────────────────────────────────────────

def _record(source: str, kind: str, message: str) -> None:
    try:
        from core import incidents
        incidents.record(source, kind, message)
    except Exception:
        pass


def evaluate(now_mono: float, now_epoch: float):
    """One supervisor pass's verdict: (decision, detail) where
    decision is None (all sources alive), "restart" (a source is
    wedged and the ledger allows it) or "give_up" (wedged, but the
    restart budget is spent). Side effects — ledger appends and
    incident records — happen here, at verdict time, so the
    supervisor thread and the tests walk the same path. Never
    raises."""
    global _gave_up_episode
    try:
        with _lock:
            arm = _arm_mono
            beats = dict(_beats)
        if arm is None:
            return None, "not armed"
        wedged = []
        for source in SOURCES:
            baseline = beats.get(source, arm)
            silent = now_mono - baseline
            if silent > STALE_S:
                wedged.append((source, silent))
        if not wedged:
            with _lock:
                _gave_up_episode = False
            return None, "all sources beating"
        who = ", ".join(f"{name} silent {int(silent)}s"
                        for name, silent in wedged)
        recent = _recent_restarts(now_epoch)
        if len(recent) >= MAX_RESTARTS:
            with _lock:
                first = not _gave_up_episode
                _gave_up_episode = True
            if first:
                _record("watchdog", "watchdog_gave_up",
                        f"{who}; restart budget spent "
                        f"({len(recent)} restarts in the last hour) — "
                        f"staying down for a human decision")
            return "give_up", who
        recent.append(now_epoch)
        _ledger_save(recent)
        _record("watchdog", "watchdog_restart",
                f"{who}; restarting "
                f"({len(recent)} of {MAX_RESTARTS} in the last hour)")
        return "restart", who
    except Exception:
        return None, "verdict failed — treated as alive"


def check_once(now_mono: float | None = None,
               now_epoch: float | None = None) -> str | None:
    """Evaluate once and, on a "restart" verdict, invoke the bound
    restart callback. The callback is expected to end the process;
    if it returns, the supervisor settles before the next pass so a
    slow exit is not mistaken for a fresh wedge."""
    decision, _detail = evaluate(
        time.monotonic() if now_mono is None else now_mono,
        time.time() if now_epoch is None else now_epoch)
    if decision == "restart":
        cb = _restart_cb
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
            time.sleep(RESTART_SETTLE_S)
    return decision


# ── The supervisor thread ────────────────────────────────────────────────────

def _supervise() -> None:
    while True:
        try:
            if _restart_cb is None:
                return
            check_once()
        except Exception:
            pass
        time.sleep(CHECK_INTERVAL_S)


def arm(restart) -> bool:
    """Bind the restart callback and start the supervisor thread.
    Idempotent: a second call keeps the first binding. Returns
    whether the supervisor is running."""
    global _arm_mono, _restart_cb, _supervisor
    with _lock:
        if _supervisor is not None and _supervisor.is_alive():
            return True
        _restart_cb = restart
        _arm_mono = time.monotonic()
        _supervisor = threading.Thread(
            target=_supervise, daemon=True, name="watchdog")
        _supervisor.start()
        return True


def disarm() -> None:
    """Stop supervising (tests, shutdown paths). The thread notices
    the unbound callback and exits on its next poll."""
    global _restart_cb, _arm_mono, _supervisor
    with _lock:
        _restart_cb = None
        _arm_mono = None
        _supervisor = None


def reset_for_tests() -> None:
    """Clear every module global a test could have dirtied."""
    global _gave_up_episode
    disarm()
    with _lock:
        _beats.clear()
        _gave_up_episode = False
