"""
core/presence.py — Presence v1: does anyone sit at this computer?

WHAT THIS IS
    A standalone, stdlib-only awareness layer. A PresenceMonitor
    polls three cheap signals — how long since the last keyboard /
    mouse input, whether the workstation is locked, and whether
    media is playing — and folds them into one bit of state:
    PRESENT or AWAY. main.py owns the consequences (the HUD naps,
    a spoken greeting on a long-awaited return); scheduler and
    screenwatch consult the module-level is_present() so proactive
    speech and screen checks pause while nobody is there, while
    the background scheduler itself keeps running silently.

HARD RULES (the spec — Presence v1, approved 2026-10-24)
    * NO camera, no microphone sensing, no phone token. Input idle
      and the lock state are the only senses in v1.
    * Presence sleep is NOT wake-word sleep. This module never
      calls main.sleep()/wake(), never reads or writes _awake or
      _last_user_speech, and never gates a typed or spoken command.
      It is a visual + proactive-speech layer only — the Oct 22
      bug (booted, auto-slept, typing refused) stays impossible.
    * Never locks, sleeps, or powers the PC.
    * Fail-open: any error in any signal, or in presence itself,
      means PRESENT. Presence must never mute or hide ARIA by
      accident — the same default-ON philosophy as the proactive
      alerts flag it feeds.
    * Windows signals via ctypes only; no new dependencies.

The monitor is pure logic over injectable signals and clocks:
main drives tick() on a 5s asyncio loop, tests drive it directly
against fakes. No threads live in this module.
"""

from __future__ import annotations

import ctypes
import sys
import time
from datetime import date
from typing import Callable, Optional


# ── Windows signals (each returns None on "unknown") ─────────────────────

def idle_seconds() -> Optional[float]:
    """Seconds since the last keyboard/mouse input, via
    GetLastInputInfo. None off Windows or on any failure —
    unknown is never read as idle."""
    if sys.platform != "win32":
        return None
    try:
        class _LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint),
                        ("dwTime", ctypes.c_uint)]

        info = _LASTINPUTINFO()
        info.cbSize = ctypes.sizeof(info)
        if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(info)):
            return None
        tick = ctypes.windll.kernel32.GetTickCount() & 0xFFFFFFFF
        idle_ms = (tick - info.dwTime) & 0xFFFFFFFF
        if idle_ms > 0x7FFFFFFF:      # wrap/glitch guard
            return None
        return idle_ms / 1000.0
    except Exception:
        return None


def locked() -> Optional[bool]:
    """True when the workstation is locked (or on a secure
    desktop). OpenInputDesktop returns NULL when the input
    desktop is inaccessible, which is exactly the locked case;
    a returned handle is closed before answering. None off
    Windows — and an inspection failure also answers None only
    when the call itself could not be made; a NULL handle is a
    positive "locked", not an unknown."""
    if sys.platform != "win32":
        return None
    try:
        user32 = ctypes.windll.user32
        user32.OpenInputDesktop.restype = ctypes.c_void_p
        handle = user32.OpenInputDesktop(0, False, 0x0100)
        if not handle:
            return True
        try:
            user32.CloseDesktop(ctypes.c_void_p(handle))
        except Exception:
            pass
        return False
    except Exception:
        return None


# ── Greeting text (pure) ─────────────────────────────────────────────────

_MORNING = (
    "Good morning — welcome back.",
    "Morning. Good to see you.",
    "Good morning, you're back.",
)
_AFTERNOON = (
    "Welcome back.",
    "Hey, welcome back.",
    "Afternoon — good to see you.",
)
_EVENING = (
    "Evening — welcome back.",
    "Welcome back.",
    "Hey, you're back.",
)
_NIGHT = (
    "Back late, huh? Welcome back.",
    "Hey — still up? Welcome back.",
)
_LONG_ABSENCE = (
    "Welcome back — you were gone a while.",
    "Hey, long time. Welcome back.",
    "You're back — that was a proper break.",
)

_LONG_ABSENCE_S = 3 * 3600


def greeting_for(hour: int, away_s: float, day_index: int) -> str:
    """One short, time-aware greeting. Pure: the variant rotates
    by day so it is not the same line daily, and a long absence
    (3h+) draws from its own set. Plain greeting only — no bills,
    no pending items, no briefing content, ever."""
    if away_s >= _LONG_ABSENCE_S:
        variants = _LONG_ABSENCE
    elif 5 <= hour < 12:
        variants = _MORNING
    elif 12 <= hour < 18:
        variants = _AFTERNOON
    elif 18 <= hour < 23:
        variants = _EVENING
    else:
        variants = _NIGHT
    return variants[day_index % len(variants)]


def _in_quiet_hours(hour: int, quiet_hours) -> bool:
    try:
        start, end = int(quiet_hours[0]), int(quiet_hours[1])
    except Exception:
        return False
    if start == end:
        return False
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


# ── Session activity (main's busy probe) ─────────────────────────────────

def session_counts_as_activity(session_open: bool,
                               seconds_since_speech: float,
                               idle_after_s: float) -> bool:
    """Does an open Live session count as somebody being here?

    A session on its own proves nothing: it stays open for hours
    after he walks away, and voice input never resets the Windows
    idle clock — so the bare session WAS the veto that kept
    presence from ever seeing an empty room (live witness
    2026-10-24: away well over ten minutes, session open, ARIA
    kept talking). A session counts as activity only while his
    speech is recent — inside the same idle window the monitor
    itself uses. The boundary is stale: exactly at the window,
    the evidence has expired.

    Pure and argument-only by design: this module never reads
    main's speech clock (the hard rule in the header); main
    computes the delta and passes it in. Junk numbers are not
    activity.
    """
    if not session_open:
        return False
    try:
        return float(seconds_since_speech) < float(idle_after_s)
    except (TypeError, ValueError):
        return False


# ── The monitor ──────────────────────────────────────────────────────────

class PresenceMonitor:
    """PRESENT/AWAY folded from injectable signals. tick() is one
    synchronous, deterministic poll step; callbacks fire exactly
    once per transition, after the state has changed, and a broken
    callback can never corrupt the state."""

    def __init__(self,
                 signals=None,
                 idle_signal: Optional[Callable] = None,
                 locked_signal: Optional[Callable] = None,
                 media_signal: Optional[Callable] = None,
                 busy_probe: Optional[Callable] = None,
                 monotonic: Optional[Callable] = None,
                 hour_fn: Optional[Callable] = None,
                 idle_after_s: float = 600,
                 greet_after_s: float = 1800,
                 quiet_hours=(23, 7),
                 enabled: bool = True,
                 on_away: Optional[Callable] = None,
                 on_return: Optional[Callable] = None):
        if signals is not None:
            def _from_signals(name):
                if isinstance(signals, dict):
                    return signals.get(name)
                return getattr(signals, name, None)
            idle_signal = idle_signal or _from_signals("idle_seconds")
            locked_signal = locked_signal or _from_signals("locked")
            media_signal = media_signal or _from_signals("media_playing")
        self._idle_signal = idle_signal or (lambda: None)
        self._locked_signal = locked_signal or (lambda: None)
        self._media_signal = media_signal or (lambda: False)
        self._busy_probe = busy_probe or (lambda: False)
        self._monotonic = monotonic or time.monotonic
        self._hour_fn = hour_fn or (lambda: time.localtime().tm_hour)
        self.idle_after_s = float(idle_after_s)
        self.greet_after_s = float(greet_after_s)
        self.quiet_hours = tuple(quiet_hours)
        self.enabled = bool(enabled)
        self._on_away = on_away
        self._on_return = on_return
        self.present = True
        self._away_since: Optional[float] = None
        self.away_reason: Optional[str] = None
        # The previous tick's locked() reading. locked() is a level
        # signal (False on EVERY tick of an unlocked machine), so an
        # unlock only counts as a return when it is a True→False
        # transition — the level alone must never end an absence.
        self._prev_locked: Optional[bool] = None

    # signal reads: an exception is an unknown, never a verdict
    def _idle(self):
        try:
            return self._idle_signal()
        except Exception:
            return None

    def _locked(self):
        try:
            return self._locked_signal()
        except Exception:
            return None

    def _vetoed(self) -> bool:
        """Media playing or ARIA busy (speaking / mid-task) vetoes
        the away transition. A broken probe never vetoes."""
        for probe in (self._media_signal, self._busy_probe):
            try:
                if probe():
                    return True
            except Exception:
                continue
        return False

    def _go_away(self, reason: str) -> None:
        self.present = False
        self._away_since = self._monotonic()
        self.away_reason = reason
        if self._on_away is not None:
            try:
                self._on_away(reason)
            except Exception:
                pass

    def _come_back(self) -> None:
        away_s = 0.0
        if self._away_since is not None:
            away_s = max(0.0, self._monotonic() - self._away_since)
        self.present = True
        self._away_since = None
        self.away_reason = None
        greet = False
        if away_s >= self.greet_after_s:
            try:
                hour = int(self._hour_fn())
            except Exception:
                hour = 12
            greet = not _in_quiet_hours(hour, self.quiet_hours)
        if self._on_return is not None:
            try:
                self._on_return(away_s, greet)
            except Exception:
                pass

    def tick(self) -> None:
        """One poll step. Inert when disabled; fail-open throughout
        — when nothing can be confirmed, the state is PRESENT."""
        if not self.enabled:
            return
        if self.present:
            lk = self._locked()
            self._prev_locked = lk
            if lk is True:
                self._go_away("lock")
                return
            idle = self._idle()
            if idle is not None and idle >= self.idle_after_s:
                if not self._vetoed():
                    self._go_away("idle")
            return
        # AWAY: only a positive return signal brings her back.
        # locked() reads False on every tick of an unlocked machine,
        # so the False LEVEL is not a return (the v1 code treated it
        # as one and flapped away/back every other tick for as long
        # as the idle clock stayed high — witnessed live 2026-10-24).
        # A return is the True→False transition (a real unlock), or
        # fresh input, or both signals going unknown (fail-open).
        lk = self._locked()
        was_locked = self._prev_locked
        self._prev_locked = lk
        if lk is True:
            return                      # still locked: stay away
        if lk is False and was_locked is True:
            self._come_back()           # he unlocked it: a real return
            return
        idle = self._idle()
        if idle is not None and idle < self.idle_after_s:
            self._come_back()           # fresh input reset the clock
            return
        if lk is None and idle is None:
            # Both signals unknown: absence cannot be confirmed,
            # and fail-open means PRESENT.
            self._come_back()


# ── Module registry (scheduler / screenwatch consult this) ───────────────

_monitor: Optional[PresenceMonitor] = None


def set_monitor(monitor: Optional[PresenceMonitor]) -> None:
    global _monitor
    _monitor = monitor


def get_monitor() -> Optional[PresenceMonitor]:
    return _monitor


def is_present() -> bool:
    """True when no monitor is registered, and True on any error:
    presence can only ever SILENCE her, so doubt answers present."""
    if _monitor is None:
        return True
    try:
        return bool(_monitor.present)
    except Exception:
        return True


def today_index() -> int:
    """The day ordinal greeting_for rotates on."""
    return date.today().toordinal()
