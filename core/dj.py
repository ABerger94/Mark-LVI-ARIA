"""
core/dj.py — DJ mode (ARIA's spotify.tool_dj, ported).

What ARIA's DJ did, kept exactly: take a loose request ("play my
coding playlist", "shuffle some chill music", "play my liked
songs") and resolve it to something Spotify can open, in her order:
  1. "liked songs"  → the Liked Songs collection URI;
  2. a SAVED playlist — the user's own playlists from the memory
     `playlist` category, matched by ARIA's word rule (every word
     of the cleaned request appears in the saved key, or the saved
     key's words all appear in the request);
  3. a curated MOOD fallback — the Spotify editorial playlists
     below, hers verbatim;
  4. Spotify search for whatever name is left.

Phase D's importer lands ARIA's saved playlists in long_term.json
under the `playlist` category, which is where find_playlist reads.

THE EMPTY-STORE RULE (the migration brief's, and the reason step 4
is conditional): when the playlist category is EMPTY — the import
has not run on this machine yet — there is nothing of the user's to
search-resolve against, and DJ must say so plainly and offer the
mood fallback instead of inventing a playlist or firing a search
and dressing it up as one. Only a non-empty store that simply
misses the name falls through to search, labelled as a search.

Playback itself is core/spotify's (open_uri, focus-gated shuffle
chord, the media play key) — the same transport the spotify action
uses.
"""

from __future__ import annotations

import re
import time
import urllib.parse
from typing import Optional

# ARIA's curated mood fallbacks, verbatim.
CURATED_MOODS = {
    "chill": ("spotify:playlist:37i9dQZF1DWWQRwui0ExPn", "Lofi Beats"),
    "lofi": ("spotify:playlist:37i9dQZF1DWWQRwui0ExPn", "Lofi Beats"),
    "focus": ("spotify:playlist:37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"),
    "ambient": ("spotify:playlist:37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"),
    "study": ("spotify:playlist:37i9dQZF1DWZeKCadgRdKQ", "Deep Focus"),
    "energy": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
    "workout": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
    "gym": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP", "Beast Mode"),
    "electronic workout": ("spotify:playlist:37i9dQZF1DX76Wlfdnj7AP",
                           "Beast Mode"),
}

LIKED_URI = "spotify:collection:tracks"

# Filler words stripped from a request before playlist matching —
# ARIA's pattern, verbatim.
_STRIP_RE = re.compile(
    r"\b(play|shuffle|shuffled|some|something|music|me|my|playlist|"
    r"playlists|on|spotify)\b")


def _playlist_rows(memory: Optional[dict] = None) -> dict:
    """The memory store's `playlist` category as {key: uri}.
    Entries are {key: {"value": uri}} (the importer's shape); plain
    string values are tolerated."""
    if memory is None:
        from memory import memory_manager
        memory = memory_manager.load_memory()
    rows = (memory or {}).get("playlist") or {}
    out = {}
    if isinstance(rows, dict):
        for key, entry in rows.items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            if isinstance(value, str) and value.strip():
                out[str(key)] = value.strip()
    return out


def playlist_count(memory: Optional[dict] = None) -> int:
    return len(_playlist_rows(memory))


def find_playlist(name: str, memory: Optional[dict] = None
                  ) -> tuple[Optional[str], Optional[str]]:
    """ARIA's matcher: (uri, saved key) or (None, None)."""
    rows = _playlist_rows(memory)
    if not rows or not name:
        return None, None
    words = [w for w in re.sub(r"[^a-z0-9\s]", "", name.lower()).split()
             if w not in ("my", "playlist", "playlists")]
    if not words:
        return None, None
    joined = " ".join(words)
    for key, value in rows.items():
        key_lower = key.lower()
        if all(w in key_lower for w in words):
            return value, key
        if all(w in joined for w in key_lower.split()):
            return value, key
    return None, None


def resolve(request: str, memory: Optional[dict] = None) -> dict:
    """Resolve a DJ request. Returns:
        {uri, label, via, shuffle, clean_name}
    via is one of: liked, saved, mood, search, none. `none` is the
    empty-store case — uri is None and the caller must say so."""
    q = (request or "").lower()
    shuffle = "shuffle" in q
    if "liked songs" in q:
        return {"uri": LIKED_URI, "label": "Liked Songs", "via": "liked",
                "shuffle": shuffle, "clean_name": "liked songs"}
    name = " ".join(_STRIP_RE.sub(" ", q).split())
    uri, saved_key = find_playlist(name, memory)
    if uri:
        return {"uri": uri, "label": saved_key, "via": "saved",
                "shuffle": shuffle, "clean_name": name}
    for mood, (mood_uri, mood_label) in CURATED_MOODS.items():
        if mood in q:
            return {"uri": mood_uri, "label": mood_label, "via": "mood",
                    "shuffle": shuffle, "clean_name": name}
    if playlist_count(memory) == 0:
        return {"uri": None, "label": None, "via": "none",
                "shuffle": shuffle, "clean_name": name}
    if name:
        return {"uri": "spotify:search:" + urllib.parse.quote(name),
                "label": name, "via": "search", "shuffle": shuffle,
                "clean_name": name}
    return {"uri": None, "label": None, "via": "none",
            "shuffle": shuffle, "clean_name": name}


def play(uri: str, shuffle: bool = False) -> tuple[bool, bool]:
    """Open the URI in Spotify and start playback. Returns
    (opened, shuffle_sent). ARIA's sequence: open, give the app a
    moment to load it, (shuffle: focus + Ctrl+S first), then the
    play key. The sleeps are hers — the URI load is not instant and
    a play key into a still-loading app is a no-op."""
    from core import spotify as spotify_core

    if not spotify_core.open_uri(uri):
        return False, False
    time.sleep(1.5)
    shuffle_sent = False
    if shuffle:
        spotify_core.focus_spotify()
        time.sleep(0.4)
        shuffle_sent = spotify_core.tap_shuffle_key()
    spotify_core.media_key("play_pause")
    return True, shuffle_sent
