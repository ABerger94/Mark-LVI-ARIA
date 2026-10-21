"""
actions/spotify.py — Spotify control: transport, launch, now playing,
and Spotify's own volume.

The Phase C port of ARIA's Spotify surface (blueprint P2). All of the
mechanism lives in core/spotify.py — window-title telemetry, Windows
media keys, the per-app pycaw session — and this module is only the
tool-shaped front of it, returning core/outcome lines like every other
action. What ARIA had beyond this (search, play_uri, DJ mode over
playlists saved in her memory) is deliberately not here: it depended on
ARIA's own memory store, and the blueprint's Phase C scope is the
transport + telemetry surface. It can come back as its own phase.

Honesty notes, because the outcomes differ per action:
  * play/pause/next/previous are a keystroke the user watches happen —
    the same class computer_settings reports as done.
  * open is `unverified`: the launch is issued, whether the window
    appeared is not read back.
  * volume_set reads Spotify's session level back after setting it, so
    it reports the observed number.
"""

from __future__ import annotations

import core.spotify as _core
from core import outcome

_TRANSPORT = {"play", "pause", "play_pause", "next", "previous", "prev"}


def spotify(parameters: dict = None, response=None, player=None,
            session_memory=None, speak=None) -> str:
    params = parameters or {}
    action = (params.get("action") or "now_playing").lower().strip()

    if player:
        print(f"[Spotify] {action}")          # terminal, not the HUD

    if action == "open":
        if _core.launch_spotify():
            return outcome.unverified(
                "spotify",
                "the launch was issued; whether the app appeared was not "
                "read back", wanted="open")
        return outcome.failed(
            "spotify", "Spotify could not be launched on this machine",
            wanted="open")

    if action in _TRANSPORT:
        key = "previous" if action == "prev" else action
        if _core.media_key(key):
            return outcome.done("spotify", wanted=action)
        return outcome.failed(
            "spotify",
            f"the {action} media key could not be sent on this platform",
            wanted=action)

    if action == "now_playing":
        info = _core.now_playing()
        if info["playing"]:
            return outcome.done("spotify", wanted=action, playing=True,
                                track=info["track"],
                                artist=info["artist"] or None)
        if info["running"]:
            return outcome.done("spotify", wanted=action, playing=False,
                                note="Spotify is open; nothing is playing")
        return outcome.done("spotify", wanted=action, playing=False,
                            note="Spotify is not running")

    if action == "volume_set":
        raw = params.get("value", params.get("volume"))
        if raw is None:
            return outcome.failed(
                "spotify", "no volume level was given (0-100)",
                wanted=action)
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return outcome.failed(
                "spotify", f"'{raw}' is not a volume level (0-100)",
                wanted=action)
        now = _core.set_spotify_volume(level)
        if now is None:
            return outcome.failed(
                "spotify",
                "Spotify's own volume could not be set — it is not "
                "running, or per-app volume is unavailable here",
                wanted=action)
        return outcome.done("spotify", wanted=action, volume=now,
                            verified=True)

    return outcome.ask(
        "spotify",
        f"'{action}' is not a Spotify action — open, play_pause, next, "
        f"previous, now_playing or volume_set. Which was meant?",
        wanted=action)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "spotify",
    "description": (
        "Controls Spotify and reports what is playing. Use for anything "
        "about Spotify or the music that is on: 'open Spotify', 'pause "
        "the music', 'next track', 'what's playing', 'set Spotify's "
        "volume to 40'. Actions: open (launch the app), play_pause / "
        "play / pause (toggle), next, previous, now_playing (reports "
        "track and artist from the app window), volume_set (Spotify's "
        "OWN volume 0-100 in 'value' — never the system volume). "
        "now_playing saying nothing is playing is a real answer, not a "
        "failure. For the SYSTEM volume use computer_settings instead."
    ),
    "brief": (
        "Spotify: open it, play/pause, skip tracks, report what's "
        "playing, set Spotify's own volume. Put the verb in 'action' "
        "and a 0-100 level in 'value' for volume_set."
    ),
    "tier": "core",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "open | play_pause | play | pause | next | previous | now_playing | volume_set",
            },
            "value": {
                "type": "INTEGER",
                "description": "Volume level 0-100, for volume_set only",
            },
        },
        "required": ["action"],
    },
    "handler": spotify,
}
