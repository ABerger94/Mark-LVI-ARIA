"""Media control — the tool (actions/media_control.py) and the HUD
extension of actions/video_player.py.

Routing and honesty are what is pinned here, with fakes at both
backend seams (the `player` facade the actions already receive, and
media_control's injectable WMP backend):

  * the HUD wins when a HUD video is playing; Windows Media Player
    is next; with neither, the answer is an honest failure, never a
    claimed success;
  * a seek reports the position RE-READ after the seek — a player
    that lands somewhere other than the target is reported where it
    actually landed;
  * invalid inputs (percent off the scale, a garbage timecode, a
    seek_by with no amount) fail in words, before anything moves;
  * a WMP backend that reports itself unavailable surfaces its
    reason instead of a guess.
"""
import pytest

from actions import media_control as mc_action
from actions import video_player as vp_action
from core import mediacontrol as mc

DURATION_S = 5700.0          # 1:35:00 — the movie
HALFWAY_S = 2850.0           # 47:30


# ── fakes ────────────────────────────────────────────────────────────────────

class FakeHudPlayer:
    """The player facade's video surface, in memory. `land_offset`
    simulates a player that does not land exactly where it was told:
    the report must follow the re-read position, not the request."""

    def __init__(self, playing=True, position_s=0.0, land_offset=0.0):
        self._playing = playing
        self._pos_ms = int(position_s * 1000)
        self._dur_ms = int(DURATION_S * 1000)
        self._land_offset_ms = int(land_offset * 1000)
        self._transport_playing = True
        self._volume = 80
        self.seeks = []

    def video_is_playing(self):
        return self._playing

    def video_position_ms(self):
        return self._pos_ms

    def video_duration_ms(self):
        return self._dur_ms

    def seek_video(self, position_ms):
        self.seeks.append(position_ms)
        self._pos_ms = max(0, min(self._dur_ms,
                                  int(position_ms) + self._land_offset_ms))

    def video_playback_playing(self):
        return self._transport_playing

    def video_set_playing(self, playing):
        self._transport_playing = bool(playing)

    def video_toggle_playback(self):
        self._transport_playing = not self._transport_playing

    def video_volume(self):
        return self._volume

    def set_video_volume(self, level):
        self._volume = max(0, min(100, int(level)))


class FakeWmpBackend:
    """core/wmp_control's function shape, scripted."""

    def __init__(self, available=True, position_s=600.0):
        self._available = available
        self._pos = position_s
        self._playing = True
        self._volume = 40
        self.calls = []

    def _state(self, ok=True, reason=""):
        return {"ok": ok, "available": self._available,
                "running": self._available,
                "position_s": self._pos if self._available else None,
                "duration_s": DURATION_S if self._available else None,
                "fraction": (self._pos / DURATION_S
                             if self._available else None),
                "playing": self._playing if self._available else None,
                "volume": self._volume if self._available else None,
                "title": "Windows Media Player",
                "reason": reason or ("" if self._available
                                     else "Windows Media Player is "
                                          "not running")}

    def get_state(self):
        self.calls.append(("get_state",))
        return self._state()

    def seek_percent(self, percent):
        self.calls.append(("seek_percent", percent))
        self._pos = mc.percent_to_seconds(percent, DURATION_S)
        return self._state()

    def seek_to(self, seconds):
        self.calls.append(("seek_to", seconds))
        self._pos = mc.clamp_target(seconds, DURATION_S)
        return self._state()

    def seek_by(self, seconds):
        self.calls.append(("seek_by", seconds))
        self._pos = mc.clamp_target(self._pos + seconds, DURATION_S)
        return self._state()

    def play_pause(self):
        self.calls.append(("play_pause",))
        self._playing = not self._playing
        return self._state()

    def set_playing(self, playing):
        self.calls.append(("set_playing", playing))
        self._playing = bool(playing)
        return self._state()

    def set_volume(self, level):
        self.calls.append(("set_volume", level))
        self._volume = level
        return self._state()


@pytest.fixture()
def wmp(monkeypatch):
    backend = FakeWmpBackend()
    monkeypatch.setattr(mc_action, "_wmp_backend", backend)
    return backend


NO_PLAYER = None


# ── routing ──────────────────────────────────────────────────────────────────

def test_hud_wins_over_wmp(wmp):
    hud = FakeHudPlayer(position_s=600.0)
    out = mc_action.media_control({"action": "position"}, player=hud)
    assert "status=done" in out
    assert "10:00 of 1:35:00" in out
    assert wmp.calls == []            # WMP never consulted


def test_wmp_used_when_no_hud_video(wmp):
    hud = FakeHudPlayer(playing=False)
    out = mc_action.media_control({"action": "position"}, player=hud)
    assert "status=done" in out
    assert "10:00 of 1:35:00" in out
    assert "Windows Media Player" in out
    assert wmp.calls == [("get_state",)]


def test_nothing_playing_is_an_honest_failure(monkeypatch):
    monkeypatch.setattr(mc_action, "_wmp_backend",
                        FakeWmpBackend(available=False))
    out = mc_action.media_control({"action": "position"}, player=NO_PLAYER)
    assert "status=failed" in out
    assert "nothing controllable is playing" in out.lower()


def test_wmp_unavailable_reason_is_surfaced(monkeypatch):
    monkeypatch.setattr(mc_action, "_wmp_backend",
                        FakeWmpBackend(available=False))
    out = mc_action.media_control({"action": "seek_percent", "percent": 50},
                                  player=NO_PLAYER)
    assert "status=failed" in out
    assert "not running" in out


# ── seeks report the re-read position ────────────────────────────────────────

def test_hud_seek_to_halfway(wmp):
    hud = FakeHudPlayer(position_s=0.0)
    out = mc_action.media_control({"action": "seek_percent", "percent": 50},
                                  player=hud)
    assert "status=done" in out
    assert hud.seeks == [int(HALFWAY_S * 1000)]
    assert "47:30 of 1:35:00" in out
    assert "halfway" in out
    assert wmp.calls == []


def test_hud_seek_reports_where_it_actually_landed():
    # The player lands 50 seconds short of the target; the report
    # must say 46:40 (the re-read), not 47:30 (the request).
    hud = FakeHudPlayer(position_s=0.0, land_offset=-50.0)
    out = mc_action.media_control({"action": "seek_percent", "percent": 50},
                                  player=hud)
    assert "status=done" in out
    assert "46:40 of 1:35:00" in out
    assert "47:30" not in out


def test_wmp_seek_by_is_relative(wmp):
    out = mc_action.media_control({"action": "seek_by", "seconds": 600},
                                  player=NO_PLAYER)
    assert ("seek_by", 600) in wmp.calls
    assert "20:00 of 1:35:00" in out     # 10:00 + 10 minutes, re-read


def test_wmp_seek_to_timecode(wmp):
    out = mc_action.media_control({"action": "seek_to",
                                   "timecode": "1:12:00"}, player=NO_PLAYER)
    assert ("seek_to", 4320.0) in wmp.calls
    assert "1:12:00 of 1:35:00" in out


def test_hud_seek_by_goes_back():
    hud = FakeHudPlayer(position_s=1000.0)
    out = mc_action.media_control({"action": "seek_by", "seconds": -300},
                                  player=hud)
    assert "11:40 of 1:35:00" in out     # 700s, re-read


# ── validation ───────────────────────────────────────────────────────────────

def test_percent_off_the_scale_fails(wmp):
    hud = FakeHudPlayer()
    out = mc_action.media_control({"action": "seek_percent", "percent": 150},
                                  player=hud)
    assert "status=failed" in out
    assert hud.seeks == [] and wmp.calls == []


def test_garbage_timecode_fails(wmp):
    out = mc_action.media_control({"action": "seek_to",
                                   "timecode": "whenever"}, player=NO_PLAYER)
    assert "status=failed" in out
    assert wmp.calls == []


def test_seek_by_without_an_amount_fails(wmp):
    out = mc_action.media_control({"action": "seek_by"}, player=NO_PLAYER)
    assert "status=failed" in out
    assert wmp.calls == []


def test_volume_out_of_range_fails(wmp):
    out = mc_action.media_control({"action": "volume", "volume": 250},
                                  player=NO_PLAYER)
    assert "status=failed" in out
    assert wmp.calls == []


def test_unknown_action_asks():
    out = mc_action.media_control({"action": "explode"},
                                  player=FakeHudPlayer())
    assert "status=ask" in out


# ── transport + volume ───────────────────────────────────────────────────────

def test_hud_pause_and_play():
    hud = FakeHudPlayer()
    out = mc_action.media_control({"action": "pause"}, player=hud)
    assert "status=done" in out and "Paused" in out
    assert hud.video_playback_playing() is False
    out = mc_action.media_control({"action": "play"}, player=hud)
    assert "Playing" in out
    assert hud.video_playback_playing() is True


def test_hud_play_pause_toggles():
    hud = FakeHudPlayer()
    out = mc_action.media_control({"action": "play_pause"}, player=hud)
    assert hud.video_playback_playing() is False
    assert "Paused" in out


def test_wmp_volume_is_read_back(wmp):
    out = mc_action.media_control({"action": "volume", "volume": 65},
                                  player=NO_PLAYER)
    assert ("set_volume", 65) in wmp.calls
    assert "status=done" in out
    assert "65" in out


def test_hud_volume():
    hud = FakeHudPlayer()
    out = mc_action.media_control({"action": "volume", "volume": 30},
                                  player=hud)
    assert "status=done" in out
    assert hud.video_volume() == 30


# ── the video_player HUD extension ───────────────────────────────────────────

def test_video_player_seek_percent():
    hud = FakeHudPlayer(position_s=0.0)
    out = vp_action.video_player({"action": "seek_percent", "percent": 50},
                                 player=hud)
    assert "47:30 of 1:35:00" in out
    assert hud.seeks == [int(HALFWAY_S * 1000)]


def test_video_player_position():
    hud = FakeHudPlayer(position_s=600.0)
    out = vp_action.video_player({"action": "position"}, player=hud)
    assert "10:00 of 1:35:00" in out


def test_video_player_seek_without_a_video_is_honest():
    hud = FakeHudPlayer(playing=False)
    out = vp_action.video_player({"action": "seek_percent", "percent": 50},
                                 player=hud)
    assert "Nothing is playing" in out
    assert hud.seeks == []
