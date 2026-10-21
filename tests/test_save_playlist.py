"""Phase F3 — save_playlist: the voice write path to the playlist
shelf DJ reads.

The gap this closes: core/dj.py resolves named playlists from
memory's `playlist` category, but until now only the Phase D
importer could write it — memory_manager.remember() whitelisted six
categories and silently rerouted "playlist" to `notes`. Pinned:
  * link normalisation — URI, open.spotify.com URL (query strings,
    /intl-/ and legacy /user/ segments), bare 22-char id — and
    clean rejection of everything else;
  * the write lands in the `playlist` category in the importer's
    shape, verified by re-reading the store;
  * the integration itself: a just-saved name resolves through
    core/dj.py's own find_playlist/resolve;
  * overwrite semantics (replace + report; same link = already
    saved, no duplicate);
  * remember() with category="playlist" no longer reroutes, while
    a genuinely invalid category still lands in `notes`.
"""
import json

import pytest

from core import dj as dj_core
from memory import memory_manager
from actions import save_playlist as save_action


ID_A = "37i9dQZF1DWWQRwui0ExPn"          # Lofi Beats' real id shape
ID_B = "37i9dQZF1DX76Wlfdnj7AP"          # Beast Mode's
URI_A = f"spotify:playlist:{ID_A}"
URI_B = f"spotify:playlist:{ID_B}"


@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    path = tmp_path / "long_term.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(memory_manager, "MEMORY_PATH", path)

    def _read():
        return json.loads(path.read_text(encoding="utf-8"))

    return _read


# ── normalisation ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("link", [
    URI_A,
    f"  {URI_A}  ",
    f"https://open.spotify.com/playlist/{ID_A}",
    f"https://open.spotify.com/playlist/{ID_A}?si=abc123&utm_source=copy",
    f"https://open.spotify.com/intl-de/playlist/{ID_A}?si=x",
    f"https://open.spotify.com/user/alek/playlist/{ID_A}",
    f"https://open.spotify.com/playlist/{ID_A}/",
    ID_A,
])
def test_normalise_accepts_playlist_forms(link):
    assert save_action.normalise_playlist_uri(link) == URI_A


@pytest.mark.parametrize("link", [
    "",
    "   ",
    "my coding playlist",
    "abc123",                                        # too short for an id
    "spotify:playlist:",                             # no id
    "spotify:playlist:short",
    f"spotify:track:{ID_A}",                         # a track, not a playlist
    f"spotify:album:{ID_A}",
    f"https://open.spotify.com/track/{ID_A}",
    f"https://open.spotify.com/album/{ID_A}",
    f"https://example.com/playlist/{ID_A}",         # not Spotify
    "https://open.spotify.com/playlist/",           # no id
])
def test_normalise_rejects_non_playlists(link):
    assert save_action.normalise_playlist_uri(link) is None


# ── the action: write path ───────────────────────────────────────────────────

def test_save_lands_in_playlist_category_importer_shape(memory_file):
    result = save_action.save_playlist(
        {"name": "coding", "link": f"https://open.spotify.com/playlist/{ID_A}?si=z"})
    assert "status=done" in result
    store = memory_file()
    entry = store["playlist"]["coding"]
    assert isinstance(entry, dict) and entry["value"] == URI_A
    assert "coding" in result and "play my coding playlist" in result


def test_save_then_dj_resolves_the_name(memory_file):
    save_action.save_playlist({"name": "coding", "link": ID_A})
    memory = memory_manager.load_memory()
    uri, key = dj_core.find_playlist("coding", memory=memory)
    assert (uri, key) == (URI_A, "coding")
    resolved = dj_core.resolve("play my coding playlist", memory=memory)
    assert resolved["via"] == "saved" and resolved["uri"] == URI_A


def test_save_overwrite_replaces_and_reports(memory_file):
    save_action.save_playlist({"name": "coding", "link": ID_A})
    result = save_action.save_playlist({"name": "coding", "link": ID_B})
    assert "status=done" in result and "replaced=true" in result
    assert "Replaced the old link" in result and URI_A in result
    memory = memory_manager.load_memory()
    assert dj_core._playlist_rows(memory) == {"coding": URI_B}
    assert dj_core.playlist_count(memory) == 1


def test_save_same_link_twice_is_already_saved(memory_file):
    save_action.save_playlist({"name": "coding", "link": ID_A})
    result = save_action.save_playlist({"name": "coding", "link": URI_A})
    assert "status=done" in result and "already saved" in result
    assert dj_core.playlist_count(memory_manager.load_memory()) == 1


def test_save_bad_link_fails_and_writes_nothing(memory_file):
    result = save_action.save_playlist(
        {"name": "coding", "link": "https://example.com/whatever"})
    assert "status=failed" in result
    assert "open.spotify.com" in result        # says what it accepts
    assert "playlist" not in memory_file()     # and nothing was saved


def test_save_missing_name_or_link_fails(memory_file):
    assert "status=failed" in save_action.save_playlist({"link": ID_A})
    assert "status=failed" in save_action.save_playlist({"name": "coding"})
    assert "playlist" not in memory_file()


def test_tool_declaration_shape():
    tool = save_action.TOOL
    assert tool["name"] == "save_playlist"
    assert tool["handler"] is save_action.save_playlist
    assert tool["parameters"]["required"] == ["name", "link"]


# ── remember(): the raw path stops rerouting ────────────────────────────────

def test_remember_playlist_category_lands_where_dj_reads(memory_file):
    result = memory_manager.remember("coding", URI_A, category="playlist")
    assert result.startswith("Remembered: playlist/coding")
    memory = memory_manager.load_memory()
    assert dj_core._playlist_rows(memory) == {"coding": URI_A}


def test_remember_invalid_category_still_reroutes_to_notes(memory_file):
    result = memory_manager.remember("thing", "stuff", category="bogus")
    assert result.startswith("Remembered: notes/thing")
