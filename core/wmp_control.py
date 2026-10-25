"""
core/wmp_control.py — Windows Media Player, driven from outside.

The library plugin opens a movie with os.startfile and keeps no
handle on it, so the player it lands in — Windows Media Player, on
Alek's machine — is a stranger to the rest of the app: no position,
no duration, no seek. This module is the handle. It talks to the
RUNNING player through UI Automation (pywinauto, already a Windows
dependency of this app — game_updater and the screen_agent plugin
drive other apps' accessibility trees the same way), because the
seek bar in that tree is the very control a person would drag:

  * its range-value pattern gives a minimum, a maximum and a
    current value, in whatever units the player chose;
  * the player's own text labels ("47:30 / 1:35:00") give the
    duration in seconds, which turns the slider's unit-free
    fraction into exact seconds (core/mediacontrol does that
    maths, shared with the HUD player);
  * setting the range value IS a seek; re-reading it afterwards is
    the proof, and every public operation here re-reads and returns
    the observed state. Nothing in this module reports a position
    it did not just read.

DISCOVERY IS GENERIC, deliberately. Automation IDs and control
names differ between the modern Media Player app and the legacy
wmplayer, and between Windows builds, so nothing here hardcodes
one: top-level windows are matched by owning process (wmplayer.exe
and the modern app's process names) with the title/class heuristics
this codebase already uses for Spotify as a fallback; among the
window's sliders, the seek slider is the one named like a seek /
position control if any is, else the widest-ranged one; the volume
slider is the one named like volume, else a smaller slider that is
not the seek slider.

UNITS, HONESTLY. When labels give a duration, slider maths goes
through the fraction and is unit-free. When they do not, a slider
whose span is a plausible length in seconds (over a minute, and
not the tell-tale 100 of a percent slider) is read as seconds; a
percent slider alone can still do seek_percent and report a
fraction, but seek_to / seek_by then fail in words rather than
guess a duration.

PLAY/PAUSE prefers the player's own Play/Pause button (invoked
through its pattern). When no button is found, the Windows media
key (core/spotify.media_key) is the fallback — it needs no focus
and every Windows player honours it — and the state is re-read
afterwards either way; the button's name (it reads "Pause" while
playing) is where the playing state comes from when it can be had
at all.

FAILURE SHAPE. pywinauto is imported lazily, Windows-only, and
every automation call sits behind try/except. On another platform,
without pywinauto, with no player running, or with a player whose
tree exposes no seek slider, the answer is a state dict with
available=False and a speakable reason — never an exception into
the action, and never a fabricated position.

The pywinauto surface is wrapped in two small adapters
(_WindowAdapter / _ControlAdapter) so the discovery and selection
logic runs on plain data and tests can inject fake windows and
sliders without a desktop.
"""

from __future__ import annotations

import sys

from core import mediacontrol as _mc

_PROCESS_NAMES = ("wmplayer.exe", "microsoft.media.player.exe",
                  "mediaplayer.exe", "windowsmediaplayer.exe")
_SEEK_WORDS = ("seek", "position", "progress", "scrub", "timeline")
_VOLUME_WORDS = ("volume", "loudness")
_BUTTON_WORDS = ("play", "pause")


# ── Control records (plain data the selection logic runs on) ─────────────────

def _is_wmp_identity(process_name: str = "", title: str = "",
                     class_name: str = "") -> bool:
    """Whether a top-level window is Windows Media Player's: by its
    owning process where that resolved, else by the title/class
    heuristics (the app's own name in the title, or a window class
    carrying the player's marks). A movie's title alone — which is
    what the player's title usually IS — must never match."""
    proc = (process_name or "").lower()
    if proc:
        return proc in _PROCESS_NAMES
    text = (title or "").lower()
    if text in ("windows media player", "media player") \
            or text.startswith("windows media player"):
        return True
    cls = (class_name or "").lower()
    return "wmplayer" in cls or cls.startswith("wmp")


def _slider_score(control: dict) -> float:
    """How seek-like one slider is: named like a seek control first,
    then by range — a movie-length slider out-ranges a volume one."""
    if control.get("range") is None:
        return -1.0
    name = " ".join(str(control.get(k) or "") for k in
                    ("name", "automation_id", "class_name")).lower()
    lo, hi, _val = control["range"]
    score = float(hi) - float(lo)
    if any(w in name for w in _SEEK_WORDS):
        score += 1_000_000.0
    if any(w in name for w in _VOLUME_WORDS):
        score -= 1_000_000.0
    return score


def _pick_sliders(controls: list[dict]) -> tuple[dict | None, dict | None]:
    """(seek, volume) from a window's control records."""
    sliders = [c for c in controls if c.get("type") == "Slider"
               and c.get("range") is not None]
    if not sliders:
        return None, None
    seek = max(sliders, key=_slider_score)
    if _slider_score(seek) < 0:
        seek = None
    volume = None
    for c in sliders:
        if c is seek:
            continue
        name = " ".join(str(c.get(k) or "") for k in
                        ("name", "automation_id", "class_name")).lower()
        if any(w in name for w in _VOLUME_WORDS):
            volume = c
            break
    if volume is None:
        rest = [c for c in sliders if c is not seek]
        if rest:
            volume = min(rest, key=lambda c: float(c["range"][1])
                         - float(c["range"][0]))
    return seek, volume


def _playing_from_button(button: dict | None):
    """While playing, the transport button offers to Pause; while
    paused it offers to Play. No button → unknown (None), never a
    guess."""
    if not button:
        return None
    name = (button.get("name") or "").strip().lower()
    if name.startswith("pause"):
        return True
    if name.startswith("play"):
        return False
    return None


def state_from_parts(*, title: str = "", seek: dict | None,
                     volume_slider: dict | None,
                     label_texts: list[str],
                     button: dict | None = None,
                     reason: str = "") -> dict:
    """Assemble one state dict from a window's parts. The single
    place position/duration are derived, so a read and a post-seek
    re-read can never disagree about how."""
    state = {"ok": True, "available": True, "running": True,
             "title": title or "Windows Media Player",
             "position_s": None, "duration_s": None, "fraction": None,
             "playing": _playing_from_button(button), "volume": None,
             "reason": reason}
    pair = _mc.parse_label_timecodes(label_texts)
    if pair is not None:
        state["position_s"], state["duration_s"] = pair
    if seek is not None and seek.get("range") is not None:
        lo, hi, val = (float(x) for x in seek["range"])
        frac = _mc.slider_fraction(lo, hi, val)
        state["fraction"] = frac
        if state["duration_s"] is not None and frac is not None:
            state["position_s"] = frac * state["duration_s"]
        elif state["duration_s"] is None and lo == 0 and hi > 60 \
                and hi != 100:
            # No labels: a seconds-ranged slider reads as seconds.
            state["duration_s"] = hi
            state["position_s"] = val
    if volume_slider is not None \
            and volume_slider.get("range") is not None:
        lo, hi, val = (float(x) for x in volume_slider["range"])
        frac = _mc.slider_fraction(lo, hi, val)
        if frac is not None:
            state["volume"] = int(round(frac * 100))
    return state


def _unavailable(reason: str, running: bool = False) -> dict:
    return {"ok": False, "available": False, "running": running,
            "title": "Windows Media Player", "position_s": None,
            "duration_s": None, "fraction": None, "playing": None,
            "volume": None, "reason": reason}


# ── pywinauto adapters (the only pywinauto-shaped code) ──────────────────────

class _ControlAdapter:
    """One UI Automation element as plain data + the two verbs the
    backend needs (set a slider's value, invoke a button)."""

    def __init__(self, wrapper):
        self._w = wrapper

    def record(self) -> dict:
        w = self._w
        info = getattr(w, "element_info", None)
        rec = {"name": "", "automation_id": "", "class_name": "",
               "type": "", "range": None, "text": "", "adapter": self}
        try:
            rec["name"] = w.window_text() or ""
            rec["text"] = rec["name"]
        except Exception:
            pass
        if info is not None:
            for key, attr in (("automation_id", "automation_id"),
                              ("class_name", "class_name"),
                              ("type", "control_type")):
                try:
                    rec[key] = str(getattr(info, attr) or "")
                except Exception:
                    pass
        rng = self.range_value()
        if rng is not None:
            rec["type"] = "Slider"
            rec["range"] = rng
        return rec

    def range_value(self):
        """(minimum, maximum, value) through the range-value pattern,
        or None when the element has none."""
        try:
            iface = self._w.iface_range_value
            return (float(iface.Minimum), float(iface.Maximum),
                    float(iface.Value))
        except Exception:
            return None

    def set_range_value(self, value: float) -> bool:
        try:
            self._w.iface_range_value.SetValue(float(value))
            return True
        except Exception:
            return False

    def invoke(self) -> bool:
        try:
            self._w.iface_invoke.Invoke()
            return True
        except Exception:
            pass
        try:
            self._w.click_input()
            return True
        except Exception:
            return False


class _WindowAdapter:
    """One top-level window: identity + its descendants as records."""

    def __init__(self, wrapper):
        self._w = wrapper

    def identity(self) -> tuple[str, str, str]:
        w = self._w
        title = ""
        try:
            title = w.window_text() or ""
        except Exception:
            pass
        class_name = ""
        info = getattr(w, "element_info", None)
        if info is not None:
            try:
                class_name = str(info.class_name or "")
            except Exception:
                pass
        process_name = ""
        try:
            import psutil
            pid = w.process_id()
            if pid is None and info is not None:
                pid = info.process_id
            if pid:
                process_name = psutil.Process(int(pid)).name() or ""
        except Exception:
            pass
        return process_name, title, class_name

    def controls(self) -> list[dict]:
        try:
            return [_ControlAdapter(c).record()
                    for c in self._w.descendants()]
        except Exception:
            return []


def _enumerate_windows() -> list[_WindowAdapter]:
    """Top-level windows, Windows + pywinauto only; anywhere else an
    empty list, which the backend reports as unavailable."""
    if sys.platform != "win32":
        return []
    try:
        from pywinauto import Desktop
        return [_WindowAdapter(w)
                for w in Desktop(backend="uia").windows()]
    except Exception:
        return []


# ── The backend ──────────────────────────────────────────────────────────────

class WmpBackend:
    """The live backend. `enumerate_windows` is the seam: tests hand
    in a factory returning fake window adapters (identity() +
    controls(), with slider records carrying range triples and
    adapters that accept set_range_value / invoke)."""

    def __init__(self, enumerate_windows=None):
        self._enumerate = enumerate_windows or _enumerate_windows

    # -- discovery ------------------------------------------------------
    def _find(self):
        """(window, controls, seek, volume, button, labels) for the
        player, or a reason string when there is nothing to drive."""
        try:
            windows = self._enumerate()
        except Exception:
            windows = []
        if not windows:
            if sys.platform != "win32":
                return "Windows Media Player control needs Windows"
            return "Windows Media Player is not running"
        for window in windows:
            try:
                proc, title, cls = window.identity()
            except Exception:
                continue
            if not _is_wmp_identity(proc, title, cls):
                continue
            controls = window.controls()
            seek, volume = _pick_sliders(controls)
            if seek is None:
                return ("Windows Media Player is running, but its "
                        "seek control was not found")
            button = None
            for c in controls:
                if c.get("type") == "Button":
                    name = (c.get("name") or "").strip().lower()
                    if name.startswith(_BUTTON_WORDS):
                        button = c
                        break
            labels = [c.get("text") or "" for c in controls
                      if c.get("type") in ("Text", "")]
            return window, controls, seek, volume, button, labels, title
        return "Windows Media Player is not running"

    def _state(self):
        found = self._find()
        if isinstance(found, str):
            return _unavailable(found)
        _win, _controls, seek, volume, button, labels, title = found
        state = state_from_parts(title=title, seek=seek,
                                 volume_slider=volume,
                                 label_texts=labels, button=button)
        state["_seek"] = seek
        state["_volume_slider"] = volume
        state["_button"] = button
        return state

    @staticmethod
    def _public(state: dict) -> dict:
        return {k: v for k, v in state.items() if not k.startswith("_")}

    # -- reads ----------------------------------------------------------
    def get_state(self) -> dict:
        return self._public(self._state())

    # -- seeks ------------------------------------------------------------
    def _set_seek_fraction(self, state: dict, fraction: float) -> dict:
        seek = state.get("_seek")
        if not seek or seek.get("range") is None:
            return _unavailable("the seek control was not found",
                                running=True)
        lo, hi, _val = (float(x) for x in seek["range"])
        target = _mc.slider_value_for_fraction(lo, hi, fraction)
        adapter = seek.get("adapter")
        if target is None or adapter is None \
                or not adapter.set_range_value(target):
            out = _unavailable("the seek control refused the new "
                               "position", running=True)
            return out
        return self.get_state()                     # the re-read

    def seek_percent(self, percent) -> dict:
        state = self._state()
        if not state["available"]:
            return self._public(state)
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            return _unavailable(f"'{percent}' is not a percentage",
                                running=True)
        if pct < 0 or pct > 100:
            return _unavailable("a percentage must be between 0 "
                                "and 100", running=True)
        return self._set_seek_fraction(state, pct / 100.0)

    def seek_to(self, seconds) -> dict:
        state = self._state()
        if not state["available"]:
            return self._public(state)
        duration = state.get("duration_s")
        if duration is None:
            # A seconds-ranged slider carries its own duration.
            seek = state.get("_seek") or {}
            rng = seek.get("range")
            if rng and float(rng[0]) == 0 and float(rng[1]) > 60 \
                    and float(rng[1]) != 100:
                duration = float(rng[1])
        if duration is None:
            out = _unavailable(
                "the player's length is not readable, so a time "
                "target cannot be placed", running=True)
            return out
        target = _mc.clamp_target(float(seconds), duration)
        return self._set_seek_fraction(state, target / duration)

    def seek_by(self, seconds) -> dict:
        state = self._state()
        if not state["available"]:
            return self._public(state)
        if state.get("position_s") is None \
                or state.get("duration_s") is None:
            return _unavailable(
                "the current position is not readable, so a "
                "relative skip cannot be placed", running=True)
        try:
            target = _mc.resolve_seek_target(
                "seek_by", position_s=state["position_s"],
                duration_s=state["duration_s"], seconds=float(seconds))
        except (TypeError, ValueError) as e:
            return _unavailable(str(e), running=True)
        return self._set_seek_fraction(state,
                                       target / state["duration_s"])

    # -- transport --------------------------------------------------------
    def play_pause(self) -> dict:
        state = self._state()
        if not state["available"]:
            return self._public(state)
        if not self._press_transport(state):
            return _unavailable("the play/pause control was not "
                                "found", running=True)
        return self.get_state()

    def set_playing(self, playing: bool) -> dict:
        state = self._state()
        if not state["available"]:
            return self._public(state)
        current = state.get("playing")
        if current is not None and current == bool(playing):
            return self._public(state)              # already there
        if not self._press_transport(state):
            return _unavailable("the play/pause control was not "
                                "found", running=True)
        return self.get_state()

    @staticmethod
    def _press_transport(state: dict) -> bool:
        button = state.get("_button")
        adapter = button.get("adapter") if button else None
        if adapter is not None and adapter.invoke():
            return True
        try:
            from core import spotify as _spotify
            return bool(_spotify.media_key("play_pause"))
        except Exception:
            return False

    # -- volume -----------------------------------------------------------
    def set_volume(self, level) -> dict:
        state = self._state()
        if not state["available"]:
            return self._public(state)
        slider = state.get("_volume_slider")
        if not slider or slider.get("range") is None:
            return _unavailable("the player's volume control was "
                                "not found", running=True)
        lo, hi, _val = (float(x) for x in slider["range"])
        target = _mc.slider_value_for_fraction(
            lo, hi, max(0, min(100, int(level))) / 100.0)
        adapter = slider.get("adapter")
        if target is None or adapter is None \
                or not adapter.set_range_value(target):
            return _unavailable("the volume control refused the "
                                "new level", running=True)
        return self.get_state()                     # the re-read


_BACKEND = WmpBackend()


def get_state() -> dict:
    return _BACKEND.get_state()


def seek_percent(percent) -> dict:
    return _BACKEND.seek_percent(percent)


def seek_to(seconds) -> dict:
    return _BACKEND.seek_to(seconds)


def seek_by(seconds) -> dict:
    return _BACKEND.seek_by(seconds)


def play_pause() -> dict:
    return _BACKEND.play_pause()


def set_playing(playing: bool) -> dict:
    return _BACKEND.set_playing(playing)


def set_volume(level) -> dict:
    return _BACKEND.set_volume(level)
