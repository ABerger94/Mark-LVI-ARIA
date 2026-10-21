"""
actions/save_playlist.py — save a Spotify playlist into memory,
under a name DJ can play back.

Why this tool exists: DJ (core/dj.py) resolves named playlists from
memory's top-level `playlist` category — {name: {"value": uri}} —
but until now only the Phase D importer could write that category.
memory_manager.remember()'s category whitelist did not include
`playlist`, so "ARIA, remember this playlist" silently landed in
`notes`, where DJ never looks. This action is the front door: it
validates the link, normalises every accepted form to a
`spotify:playlist:<id>` URI, and writes the category directly via
update_memory — the importer's shape — then re-reads the store and
only reports done when the entry is actually there.

Accepted link forms:
  * a Spotify URI:            spotify:playlist:<id>
  * an open.spotify.com URL:  .../playlist/<id>, with query strings
    (?si=...), /intl-<lang>/ segments and legacy /user/<name>/
    segments all tolerated
  * a bare playlist id (Spotify ids are 22 base62 characters)
Anything else is refused, saying what IS accepted — a track link,
an album link or a sentence is not a playlist reference.
"""

from __future__ import annotations

import re

from core import outcome

_ID = r"[A-Za-z0-9]{22}"
_URI_RE = re.compile(rf"^spotify:playlist:({_ID})$")
_URL_RE = re.compile(rf"open\.spotify\.com/(?:[^\s/]+/)*?playlist/({_ID})"
                     rf"(?:[/?#\s]|$)")
_BARE_RE = re.compile(rf"^({_ID})$")

_ACCEPTED = ("a Spotify playlist link — a spotify:playlist: URI, an "
             "open.spotify.com playlist URL, or the bare playlist id")


def normalise_playlist_uri(link: str) -> str | None:
    """The canonical `spotify:playlist:<id>` for an accepted link
    form, or None when the input is not recognisably a playlist
    reference."""
    text = (link or "").strip()
    if not text:
        return None
    for pattern in (_URI_RE, _BARE_RE):
        m = pattern.match(text)
        if m:
            return f"spotify:playlist:{m.group(1)}"
    m = _URL_RE.search(text)
    if m:
        return f"spotify:playlist:{m.group(1)}"
    return None


def save_playlist(parameters: dict = None, response=None, player=None,
                  session_memory=None) -> str:
    from core import dj as dj_core
    from memory import memory_manager

    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed(
            "save_playlist",
            "no name was given — what should DJ call this playlist?")
    uri = normalise_playlist_uri(params.get("link") or "")
    if uri is None:
        return outcome.failed(
            "save_playlist",
            f"that link is not recognisably a playlist — I need "
            f"{_ACCEPTED}. Nothing was saved.",
            name=name)

    before = dj_core._playlist_rows(memory_manager.load_memory())
    previous = before.get(name)
    memory_manager.update_memory({"playlist": {name: {"value": uri}}})

    # Verified, not assumed: re-read the store and look at it.
    after = dj_core._playlist_rows(memory_manager.load_memory())
    if after.get(name) != uri:
        return outcome.failed(
            "save_playlist",
            f"the save did not land — '{name}' is not in the "
            f"playlist shelf after writing, so DJ would not find "
            f"it. Nothing was saved under that name.",
            name=name, uri=uri)

    if previous == uri:
        return outcome.done("save_playlist", name=name, uri=uri,
                            replaced=False) + \
            f"\n'{name}' was already saved with that link — DJ " \
            f"will find it as your '{name}' playlist."
    if previous is not None:
        return outcome.done("save_playlist", name=name, uri=uri,
                            replaced=True) + \
            f"\nReplaced the old link for '{name}' (was {previous}) " \
            f"— DJ will now play {uri} when you ask for your " \
            f"'{name}' playlist."
    return outcome.done("save_playlist", name=name, uri=uri,
                        replaced=False) + \
        f"\nSaved '{name}' to your playlist shelf — DJ will find " \
        f"it when you say 'play my {name} playlist'."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "save_playlist",
    "description": (
        "Save a Spotify playlist into memory under a name, so DJ "
        "mode can play it later ('play my coding playlist'). Give a "
        "name and a playlist link: a spotify:playlist: URI, an "
        "open.spotify.com playlist URL, or the bare playlist id — "
        "all are stored as the canonical URI. Saving an existing "
        "name replaces its link and says so. Prefer this over a "
        "general memory save for playlists: it validates the link "
        "and stores the canonical URI in the playlist category DJ "
        "reads by name."
    ),
    "brief": "Save a Spotify playlist under a name DJ can play ('save this as my coding playlist').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The name DJ will know it by, e.g. 'coding' or 'Doja Cat mix'",
            },
            "link": {
                "type": "STRING",
                "description": "A spotify:playlist: URI, an open.spotify.com playlist URL, or the bare 22-character playlist id",
            },
        },
        "required": ["name", "link"],
    },
    "handler": save_playlist,
}
