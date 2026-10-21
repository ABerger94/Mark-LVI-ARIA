"""
core/spotify.py — Spotify as the user sees it: what is playing, and the
keys that control it.

Ported from ARIA (aria/spotify.py + the hud's window-title telemetry) under
the Phase C blueprint (P2). There is no Spotify account integration on
either side and nothing here pretends otherwise — everything is local:

  * NOW PLAYING comes from the Spotify window's title. The desktop app
    puts "Artist - Track" there while something plays and shows its own
    name ("Spotify" / "Spotify Free" / "Spotify Premium") when idle, so
    the title IS the telemetry. No window → not running. A title that
    will not parse → still never an error, just the quiet state.

  * TRANSPORT is the Windows media keys (VK_MEDIA_*), the same global
    keys a keyboard's play button sends. They need no focus and no API.

  * SPOTIFY'S OWN VOLUME is its per-app audio session (pycaw), read and
    set in-process — the mechanism actions/computer_settings.py already
    uses for the master endpoint in this codebase. (ARIA isolated hers
    behind a helper process; the migration ruling keeps LVI's stack as
    it ships, so this calls pycaw the way the rest of LVI does.)

Everything platform-specific is decided here, once: on a platform with
no Spotify window or no pycaw, callers get the quiet state / None, never
an exception. parse_spotify_title is pure and carries the whole parsing
contract, so the title grammar is testable without a window in sight.
"""

from __future__ import annotations

import platform
import time

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"

# The titles the app gives its own window when nothing is playing.
_APP_TITLES = {"spotify", "spotify free", "spotify premium"}

# Windows media virtual keys — the same codes ARIA's VK_MEDIA_MAP used.
_VK_MEDIA = {
    "play":       0xB3,
    "pause":      0xB3,
    "play_pause": 0xB3,
    "next":       0xB0,
    "previous":   0xB1,
    "prev":       0xB1,
    "stop":       0xB2,
}


def _state(state: str, title: str = "", artist: str = "", track: str = "") -> dict:
    return {
        "state":   state,                    # "playing" | "idle" | "offline"
        "running": state != "offline",
        "playing": state == "playing",
        "title":   title,
        "artist":  artist,
        "track":   track,
    }


def parse_spotify_title(title) -> dict:
    """One window title → the now-playing state it represents.

    The grammar, exactly as ARIA's telemetry read it:
      * blank / None            → offline (there is no such window)
      * the app's own name      → running, idle ("Spotify", "... Free",
                                  "... Premium")
      * "Artist - Track"        → playing, split on the FIRST " - " (a
                                  track name may contain more dashes)
      * anything else           → playing, the whole title as the track
                                  (single-name titles: some ads, podcasts
                                  and local files present this way, and
                                  claiming a track is more useful than
                                  claiming silence)
    """
    if not title or not str(title).strip():
        return _state("offline")
    text = str(title).strip()
    if text.lower() in _APP_TITLES:
        return _state("idle", title=text)
    if " - " in text:
        artist, track = text.split(" - ", 1)
        return _state("playing", title=text,
                      artist=artist.strip(), track=track.strip())
    return _state("playing", title=text, track=text)


# ── Window enumeration (the OS layer) ────────────────────────────────────────

def _is_spotify_window(hwnd, user32) -> bool:
    """Decide by the owning process where that can be asked — a playing
    track's title does not contain the word 'Spotify' at all, so ARIA's
    title-substring test alone missed exactly the case that matters.
    psutil is already a hard dependency of this app (the SYS rail is
    built on it). Where the process cannot be resolved, fall back to
    ARIA's substring test."""
    try:
        import ctypes

        import psutil
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = psutil.Process(pid.value).name() or ""
        return name.lower().startswith("spotify")
    except Exception:
        return False


def window_titles() -> list[str]:
    """Titles of Spotify's visible windows. Windows only; anywhere else
    the list is empty and now_playing() reports the quiet state."""
    if _OS != "Windows":
        return []
    try:
        import ctypes
        user32 = ctypes.windll.user32
        titles: list[str] = []

        def _cb(hwnd, _extra):
            if user32.IsWindowVisible(hwnd):
                n = user32.GetWindowTextLengthW(hwnd)
                if n > 0:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(hwnd, buf, n + 1)
                    txt = buf.value.strip()
                    if txt and (_is_spotify_window(hwnd, user32)
                                or "spotify" in txt.lower()):
                        titles.append(txt)
            return True

        proc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.c_int)
        user32.EnumWindows(proc(_cb), 0)
        return titles
    except Exception:
        return []


# ── Now playing ──────────────────────────────────────────────────────────────

_CACHE: dict = {"at": 0.0, "info": _state("offline")}
_CACHE_SECS = 1.0


def now_playing(titles: list[str] | None = None) -> dict:
    """The current state. `titles` may be injected (tests, or a caller
    that already enumerated); otherwise windows are enumerated here,
    cached for a second so the UI's poll costs nothing like an
    enumeration per frame.

    A playing title wins over an idle one when several Spotify windows
    exist — the app keeps helper windows that sit at the app-name title
    while the main window carries the track."""
    if titles is None:
        now = time.time()
        if now - _CACHE["at"] < _CACHE_SECS:
            return _CACHE["info"]
        titles = window_titles()
        info = _pick(titles)
        _CACHE.update(at=now, info=info)
        return info
    return _pick(titles)


def _pick(titles: list[str]) -> dict:
    best = _state("offline")
    for title in titles:
        info = parse_spotify_title(title)
        if info["playing"]:
            return info
        if info["running"]:
            best = info
    return best


# ── Transport ────────────────────────────────────────────────────────────────

def media_key(key: str) -> bool:
    """Send one Windows media key. True when the key was dispatched —
    the effect on playback is the user's to see, so callers report a
    dispatch, not a verified state change. False on any other platform
    (nothing in this tree sends media keys there) or a bad key name."""
    vk = _VK_MEDIA.get((key or "").lower().strip())
    if vk is None or _OS != "Windows":
        return False
    try:
        import ctypes
        user32 = ctypes.windll.user32
        user32.keybd_event(vk, 0, 0, 0)
        user32.keybd_event(vk, 0, 2, 0)   # KEYEVENTF_KEYUP
        return True
    except Exception:
        return False


def launch_spotify() -> bool:
    """Open the app: the spotify: URI on Windows (ARIA's way), the app
    bundle on macOS, the binary / URI handler on Linux."""
    try:
        if _OS == "Windows":
            import os
            os.startfile("spotify:")
            return True
        import shutil
        import subprocess
        if _OS == "Darwin":
            subprocess.Popen(["open", "-a", "Spotify"])
            return True
        exe = shutil.which("spotify")
        subprocess.Popen([exe] if exe else ["xdg-open", "spotify:"])
        return True
    except Exception:
        return False


# ── Per-app volume (Windows, pycaw in-process) ───────────────────────────────

def _spotify_volume_iface():
    """Spotify's ISimpleAudioVolume, or None — not running, no session,
    or not Windows. Resolved per call: sessions die with the app, so a
    cached interface would go stale exactly when it matters."""
    if _OS != "Windows":
        return None
    try:
        from pycaw.pycaw import AudioUtilities
        for session in AudioUtilities.GetAllSessions():
            try:
                proc = session.Process
                if proc is not None and \
                        (proc.name() or "").lower().startswith("spotify"):
                    return session.SimpleAudioVolume
            except Exception:
                continue
    except Exception:
        return None
    return None


def spotify_volume() -> int | None:
    """Spotify's own volume 0-100, or None when it cannot be read."""
    iface = _spotify_volume_iface()
    if iface is None:
        return None
    try:
        return max(0, min(100, round(iface.GetMasterVolume() * 100)))
    except Exception:
        return None


def set_spotify_volume(level: int) -> int | None:
    """Set Spotify's own volume; returns the level read back afterwards,
    or None when there was nothing to set. The master volume is never
    touched — this is the per-app session only."""
    iface = _spotify_volume_iface()
    if iface is None:
        return None
    try:
        iface.SetMasterVolume(max(0, min(100, int(level))) / 100.0, None)
    except Exception:
        return None
    return spotify_volume()
