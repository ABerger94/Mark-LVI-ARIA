"""Phase F-REST group 7 — DJ mode (ARIA's spotify DJ, ported).

Playback is stubbed at core/spotify's seams; the memory store is a
real long_term.json in a tmp dir (the same file the Phase D import
writes), so the playlist-category reads are the production path.
Pinned shut:
  * ARIA's resolution order: liked → saved (her word rule) → mood
    → labelled search;
  * THE EMPTY-STORE RULE: with no `playlist` category at all, DJ
    says so plainly, names the mood fallback, and opens NOTHING —
    it never invents a playlist;
  * shuffle that could not be sent is reported unverified, not
    claimed.
"""
import json
import types

import pytest

from core import dj as dj_core
from core import spotify as spotify_core
from memory import memory_manager
from actions import dj as dj_action


SAVED = {
    "playlist": {
        "coding playlist": {"value": "spotify:playlist:code123"},
        "Doja Cat Mix": {"value": "spotify:playlist:doja456"},
    }
}


@pytest.fixture
def transport(monkeypatch):
    calls = {"opened": [], "keys": [], "shuffle_taps": 0, "focus": 0}
    monkeypatch.setattr(spotify_core, "open_uri",
                        lambda uri: calls["opened"].append(uri) or True)
    monkeypatch.setattr(spotify_core, "media_key",
                        lambda key: calls["keys"].append(key) or True)
    monkeypatch.setattr(spotify_core, "focus_spotify",
                        lambda: calls.__setitem__("focus",
                                                  calls["focus"] + 1) or True)
    monkeypatch.setattr(spotify_core, "tap_shuffle_key",
                        lambda: calls.__setitem__(
                            "shuffle_taps", calls["shuffle_taps"] + 1) or True)
    monkeypatch.setattr(dj_core, "time",
                        types.SimpleNamespace(sleep=lambda s: None))
    return calls


@pytest.fixture
def memory_file(tmp_path, monkeypatch):
    def _write(store):
        path = tmp_path / "long_term.json"
        path.write_text(json.dumps(store), encoding="utf-8")
        monkeypatch.setattr(memory_manager, "MEMORY_PATH", path)

    return _write


# ── resolution ───────────────────────────────────────────────────────────────

def test_resolve_liked_songs():
    r = dj_core.resolve("play my liked songs", memory=SAVED)
    assert r["via"] == "liked"
    assert r["uri"] == "spotify:collection:tracks"


def test_resolve_saved_playlist_by_word_match():
    r = dj_core.resolve("play my coding playlist", memory=SAVED)
    assert r["via"] == "saved"
    assert r["uri"] == "spotify:playlist:code123"
    assert r["label"] == "coding playlist"


def test_resolve_saved_playlist_partial_words():
    r = dj_core.resolve("play doja mix", memory=SAVED)
    assert r["via"] == "saved" and r["uri"] == "spotify:playlist:doja456"


def test_resolve_keeps_arias_strict_word_rule():
    # ARIA's matcher is all-words-or-nothing and 'the' is not a
    # filler word in her strip list — 'play the doja mix' misses the
    # saved playlist in her code too and falls through to search.
    # The port keeps the rule rather than quietly improving it.
    r = dj_core.resolve("play the doja mix", memory=SAVED)
    assert r["via"] == "search"


def test_resolve_mood_fallback():
    r = dj_core.resolve("something chill", memory=SAVED)
    assert r["via"] == "mood" and r["label"] == "Lofi Beats"


def test_resolve_empty_store_is_none_not_search():
    r = dj_core.resolve("play jazz fusion essentials", memory={})
    assert r["via"] == "none" and r["uri"] is None


def test_resolve_store_miss_is_labelled_search():
    r = dj_core.resolve("play jazz fusion essentials", memory=SAVED)
    assert r["via"] == "search"
    assert r["uri"].startswith("spotify:search:")
    assert "jazz" in r["uri"]


def test_resolve_shuffle_flag():
    r = dj_core.resolve("shuffle my coding playlist", memory=SAVED)
    assert r["shuffle"] is True and r["via"] == "saved"


def test_curated_moods_are_arias():
    assert dj_core.CURATED_MOODS["focus"][1] == "Deep Focus"
    assert dj_core.CURATED_MOODS["workout"][1] == "Beast Mode"


# ── the action ───────────────────────────────────────────────────────────────

def test_dj_plays_saved(memory_file, transport):
    memory_file(SAVED)
    result = dj_action.dj({"request": "play my coding playlist"})
    assert "status=done" in result and "via=saved" in result
    assert transport["opened"] == ["spotify:playlist:code123"]
    assert transport["keys"] == ["play_pause"]


def test_dj_empty_store_says_so_and_plays_nothing(memory_file, transport):
    memory_file({})   # no playlist category — the import has not run here
    result = dj_action.dj({"request": "play jazz fusion essentials"})
    assert "status=done" not in result
    assert "no playlists saved" in result
    assert "chill" in result            # the mood fallback is offered
    assert transport["opened"] == []   # and nothing was invented/played


def test_dj_search_is_labelled(memory_file, transport):
    memory_file(SAVED)
    result = dj_action.dj({"request": "play jazz fusion essentials"})
    assert "via=search" in result
    assert "Spotify search" in result


def test_dj_open_failure_is_honest(memory_file, transport, monkeypatch):
    memory_file(SAVED)
    monkeypatch.setattr(spotify_core, "open_uri", lambda uri: False)
    result = dj_action.dj({"request": "play my coding playlist"})
    assert "status=failed" in result


def test_dj_shuffle_unsent_is_unverified(memory_file, transport, monkeypatch):
    memory_file(SAVED)
    monkeypatch.setattr(spotify_core, "tap_shuffle_key", lambda: False)
    result = dj_action.dj({"request": "shuffle my coding playlist"})
    assert "status=unverified" in result
    assert "shuffle" in result
    assert transport["opened"] == ["spotify:playlist:code123"]


def test_dj_shuffle_sent_is_done(memory_file, transport):
    memory_file(SAVED)
    result = dj_action.dj({"request": "shuffle my coding playlist"})
    assert "status=done" in result and "shuffle=true" in result
    assert transport["shuffle_taps"] == 1
