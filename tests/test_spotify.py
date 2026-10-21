"""Phase C1 — Spotify (blueprint P2).

The port of ARIA's Spotify surface: window-title telemetry in the SYS
rail, transport keys, launch, and Spotify's own volume. Everything OS
is stubbed at the core/spotify.py seam — the parser carries the whole
title grammar, so it is pinned case by case; the action is dispatched
against a stubbed core; the panel is fed state dicts directly.

What is being pinned shut:
  * the title grammar ARIA shipped (idle app titles, "Artist - Track"
    split on the FIRST dash, single-name titles still count as music);
  * a playing title beats an idle one when several windows exist;
  * volume_set touches the per-app path only, and reports the level
    read back — and a missing session is a failure, not a guess;
  * the panel shows track + artist while playing and the quiet
    'Not playing' otherwise — never an error state.
"""
import pytest

import core.spotify as core_spotify
from actions import spotify as spotify_action


# ── the title grammar ────────────────────────────────────────────────────────

@pytest.mark.parametrize("title", [None, "", "   "])
def test_blank_titles_are_offline(title):
    info = core_spotify.parse_spotify_title(title)
    assert info["state"] == "offline"
    assert info["running"] is False and info["playing"] is False


@pytest.mark.parametrize("title", ["Spotify", "Spotify Free", "Spotify Premium"])
def test_app_name_titles_are_idle(title):
    info = core_spotify.parse_spotify_title(title)
    assert info["state"] == "idle"
    assert info["running"] is True and info["playing"] is False
    assert info["title"] == title


def test_artist_dash_track_is_playing():
    info = core_spotify.parse_spotify_title("Daft Punk - One More Time")
    assert info["playing"] is True
    assert info["artist"] == "Daft Punk"
    assert info["track"] == "One More Time"
    assert info["title"] == "Daft Punk - One More Time"


def test_split_is_on_the_first_dash_only():
    info = core_spotify.parse_spotify_title("Artist - Track - With Dashes")
    assert info["artist"] == "Artist"
    assert info["track"] == "Track - With Dashes"


def test_surrounding_whitespace_is_trimmed():
    info = core_spotify.parse_spotify_title("  Artist - Track  ")
    assert info["artist"] == "Artist" and info["track"] == "Track"


def test_single_name_title_counts_as_a_track():
    """Ads and some podcasts present a bare name with no dash. ARIA's
    grammar treated any other title as playing with the title as the
    track; the alternative — claiming silence over music the user can
    hear — is the worse lie."""
    info = core_spotify.parse_spotify_title("Advertisement")
    assert info["playing"] is True
    assert info["track"] == "Advertisement" and info["artist"] == ""


def test_degenerate_dash_title_does_not_crash():
    """'Artist - ' loses its trailing separator to the strip and lands
    in the single-name branch — degenerate, but a state, not a crash."""
    info = core_spotify.parse_spotify_title("Artist - ")
    assert info["playing"] is True and info["track"] == "Artist -"


# ── choosing among windows ───────────────────────────────────────────────────

def test_playing_title_beats_idle_title():
    info = core_spotify.now_playing(["Spotify", "Daft Punk - One More Time"])
    assert info["playing"] is True and info["track"] == "One More Time"


def test_only_idle_titles_means_idle():
    info = core_spotify.now_playing(["Spotify Premium"])
    assert info["state"] == "idle" and info["playing"] is False


def test_no_titles_means_offline():
    info = core_spotify.now_playing([])
    assert info["state"] == "offline"


# ── the action, against a stubbed OS layer ───────────────────────────────────

class _StubCore:
    """Stands in for core.spotify inside the action module."""

    def __init__(self):
        self.keys = []
        self.launched = False
        self.info = {"state": "playing", "running": True, "playing": True,
                     "title": "Daft Punk - One More Time",
                     "artist": "Daft Punk", "track": "One More Time"}
        self.volume_after = 40

    def media_key(self, key):
        self.keys.append(key)
        return True

    def launch_spotify(self):
        self.launched = True
        return True

    def now_playing(self):
        return self.info

    def set_spotify_volume(self, level):
        return self.volume_after


@pytest.fixture
def stub(monkeypatch):
    s = _StubCore()
    monkeypatch.setattr(spotify_action, "_core", s)
    return s


def test_action_transport_goes_through_media_keys(stub):
    out = spotify_action.spotify({"action": "next"})
    assert out.startswith("status=done")
    assert stub.keys == ["next"]
    out = spotify_action.spotify({"action": "play_pause"})
    assert out.startswith("status=done")
    assert stub.keys[-1] == "play_pause"


def test_action_open_reports_unverified_launch(stub):
    out = spotify_action.spotify({"action": "open"})
    assert stub.launched is True
    assert out.startswith("status=unverified")


def test_action_now_playing_reports_track_and_artist(stub):
    out = spotify_action.spotify({"action": "now_playing"})
    assert out.startswith("status=done")
    assert "One More Time" in out and "Daft Punk" in out


def test_action_now_playing_quiet_when_offline(stub):
    stub.info = {"state": "offline", "running": False, "playing": False,
                 "title": "", "artist": "", "track": ""}
    out = spotify_action.spotify({"action": "now_playing"})
    assert out.startswith("status=done") and "not running" in out


def test_action_volume_set_reports_the_readback(stub):
    out = spotify_action.spotify({"action": "volume_set", "value": 40})
    assert out.startswith("status=done") and "volume=40" in out


def test_action_volume_set_without_a_session_fails(stub):
    stub.volume_after = None
    out = spotify_action.spotify({"action": "volume_set", "value": 40})
    assert out.startswith("status=failed")


def test_action_volume_set_without_a_level_fails(stub):
    out = spotify_action.spotify({"action": "volume_set"})
    assert out.startswith("status=failed")


def test_action_unknown_action_asks(stub):
    out = spotify_action.spotify({"action": "dj"})
    assert out.startswith("status=ask")


def test_real_core_is_quiet_off_windows():
    """On this Linux test box the OS layer itself must be the quiet
    state: no titles, no keys, no session — and no exceptions."""
    if core_spotify._OS == "Windows":
        pytest.skip("the OS layer is live on Windows")
    assert core_spotify.window_titles() == []
    assert core_spotify.now_playing([])["state"] == "offline"
    assert core_spotify.media_key("play_pause") is False
    assert core_spotify.spotify_volume() is None
    assert core_spotify.set_spotify_volume(50) is None


# ── the panel ────────────────────────────────────────────────────────────────

def test_spotify_panel_states():
    from PyQt6.QtWidgets import QApplication

    from ui import SpotifyPanel

    app = QApplication.instance() or QApplication([])
    panel = SpotifyPanel()
    assert panel._track_lbl.text() == "Not playing"
    assert panel._artist_lbl.text() == ""

    panel.show_state({"playing": True, "track": "One More Time",
                      "artist": "Daft Punk", "title": "Daft Punk - One More Time"})
    assert panel._track_lbl.text() == "One More Time"
    assert panel._artist_lbl.text() == "Daft Punk"

    panel.show_state({"playing": False, "track": "", "artist": "", "title": "Spotify"})
    assert panel._track_lbl.text() == "Not playing"
    assert panel._artist_lbl.text() == ""

    panel.show_state(None)   # a telemetry failure is the quiet state too
    assert panel._track_lbl.text() == "Not playing"
    app.closeAllWindows()
