"""
actions/dj.py — DJ mode (ARIA's, ported).

Resolution lives in core/dj.py (saved playlists from memory's
`playlist` category → curated moods → labelled search). The one
rule that matters most: with NO saved playlists on this machine —
the memory import has not run here yet — DJ says exactly that and
offers the moods. It never invents a playlist the user does not
have.
"""

from __future__ import annotations

from core import dj as dj_core
from core import outcome


def dj(parameters: dict = None, response=None, player=None,
       session_memory=None) -> str:
    params = parameters or {}
    request = (params.get("request") or "").strip()
    if not request:
        return outcome.failed(
            "dj", "no request was given — what should I play?")
    resolved = dj_core.resolve(request)
    if resolved["via"] == "none":
        return outcome.ask(
            "dj",
            "I have no playlists saved on this machine yet — the "
            "saved playlists from the old setup have not been "
            "imported here, so there is nothing of yours for me to "
            "look the request up in, and I won't invent one. I can "
            "still play a mood: chill (Lofi Beats), focus (Deep "
            "Focus) or energy (Beast Mode) — or say 'liked songs' "
            "for your Liked Songs.",
            request=request)
    opened, shuffle_sent = dj_core.play(resolved["uri"],
                                        resolved["shuffle"])
    label = resolved["label"]
    if not opened:
        return outcome.failed(
            "dj", f"Spotify would not open {label}", via=resolved["via"])
    extra = ""
    if resolved["via"] == "search":
        extra = " (no saved playlist matched, so this is a Spotify search)"
    if resolved["shuffle"] and not shuffle_sent:
        return outcome.unverified(
            "dj",
            "playback started, but the shuffle key could not be sent "
            "(Spotify could not be focused), so shuffle is probably "
            "off",
            label=label, via=resolved["via"]) + f"\nNow playing: {label}.{extra}"
    return outcome.done("dj", label=label, via=resolved["via"],
                        shuffle=resolved["shuffle"]) + \
        f"\nSpotify DJ: playing {label}.{extra}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "dj",
    "description": (
        "DJ mode: play music from a loose request. Resolves in "
        "order — 'liked songs' to the Liked Songs collection, a name "
        "to a playlist saved in memory, a mood word (chill, focus, "
        "energy, workout...) to a curated Spotify playlist, and "
        "otherwise a Spotify search, said as a search. 'shuffle' in "
        "the request turns shuffle on. If no playlists are saved on "
        "this machine yet, it says so and offers the moods rather "
        "than guessing."
    ),
    "brief": "Play a saved playlist, mood or liked songs ('play my coding playlist').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "request": {
                "type": "STRING",
                "description": "What to play, e.g. 'shuffle my coding playlist', 'something chill', 'my liked songs'",
            },
        },
        "required": ["request"],
    },
    "handler": dj,
}
