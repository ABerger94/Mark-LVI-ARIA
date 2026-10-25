"""Media control — the pure logic (core/mediacontrol.py).

Everything here is arithmetic and parsing, so it is pinned without a
window, a player or a Qt install in sight:

  * the timecode grammar ("1:12:00", "12:34", "90") and its rejects;
  * format_timecode as parse_timecode's mirror;
  * seek targets: percent / absolute / relative, always clamped to
    the real duration, and honest failures when a target cannot be
    computed (no duration, garbage percent, missing delta);
  * slider math: fraction <-> value, in whatever units a slider
    happens to use, and seconds through a known duration;
  * timecode labels read off a player's UI ("47:30 / 1:35:00"): the
    duration is the largest timecode, the position is the other one.
"""
import pytest

from core import mediacontrol as mc


# ── timecodes ────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("text,seconds", [
    ("90", 90.0),
    ("0", 0.0),
    ("90.5", 90.5),
    ("12:34", 12 * 60 + 34.0),
    ("0:47:30", 47 * 60 + 30.0),
    ("1:12:00", 3600 + 12 * 60.0),
    ("2:00:00", 7200.0),
    (" 47:30 ", 47 * 60 + 30.0),
])
def test_parse_timecode_accepts(text, seconds):
    assert mc.parse_timecode(text) == seconds


def test_parse_timecode_accepts_numbers():
    assert mc.parse_timecode(90) == 90.0
    assert mc.parse_timecode(47.5) == 47.5


@pytest.mark.parametrize("bad", [
    None, "", "   ", "abc", "halfway", "1:2:3:4", "12:99", "1:60:00",
    "-30", "-1:00", "12:", ":30", "1::30", "12:34:56:78", "1.5:30",
    float("nan"), float("inf"), -5,
])
def test_parse_timecode_rejects_garbage(bad):
    assert mc.parse_timecode(bad) is None


@pytest.mark.parametrize("seconds,text", [
    (0, "0:00"),
    (30, "0:30"),
    (47 * 60 + 30, "47:30"),
    (3600 + 12 * 60, "1:12:00"),
    (5700, "1:35:00"),
    (59.6, "1:00"),          # rounds to the nearest second
])
def test_format_timecode(seconds, text):
    assert mc.format_timecode(seconds) == text


def test_format_timecode_clamps_negative():
    assert mc.format_timecode(-10) == "0:00"


def test_timecode_round_trip():
    for secs in (0, 5, 754, 2850, 5700, 7384):
        assert mc.parse_timecode(mc.format_timecode(secs)) == float(secs)


# ── seek targets ─────────────────────────────────────────────────────────────

def test_percent_to_seconds():
    assert mc.percent_to_seconds(50, 5700) == 2850.0
    assert mc.percent_to_seconds(0, 5700) == 0.0
    assert mc.percent_to_seconds(100, 5700) == 5700.0
    assert mc.percent_to_seconds(50, 0) is None
    assert mc.percent_to_seconds(50, None) is None


def test_clamp_target():
    assert mc.clamp_target(99999, 5700) == 5700.0
    assert mc.clamp_target(-5, 5700) == 0.0
    assert mc.clamp_target(100, 5700) == 100.0
    assert mc.clamp_target(-5, None) == 0.0
    assert mc.clamp_target(100, None) == 100.0


def test_resolve_seek_percent():
    target = mc.resolve_seek_target("seek_percent", position_s=0,
                                    duration_s=5700, percent=50)
    assert target == 2850.0


def test_resolve_seek_percent_needs_a_real_percent():
    with pytest.raises(ValueError):
        mc.resolve_seek_target("seek_percent", position_s=0,
                               duration_s=5700, percent=150)
    with pytest.raises(ValueError):
        mc.resolve_seek_target("seek_percent", position_s=0,
                               duration_s=5700, percent=None)
    with pytest.raises(ValueError):
        mc.resolve_seek_target("seek_percent", position_s=0,
                               duration_s=None, percent=50)


def test_resolve_seek_to_timecode_and_seconds():
    assert mc.resolve_seek_target("seek_to", position_s=0, duration_s=5700,
                                  timecode="1:12:00") == 4320.0
    assert mc.resolve_seek_target("seek_to", position_s=0, duration_s=5700,
                                  timecode="90") == 90.0
    # Past the end clamps to the end, never beyond it.
    assert mc.resolve_seek_target("seek_to", position_s=0, duration_s=5700,
                                  timecode="9:59:59") == 5700.0
    with pytest.raises(ValueError):
        mc.resolve_seek_target("seek_to", position_s=0, duration_s=5700,
                               timecode="whenever")


def test_resolve_seek_by_is_relative_and_clamped():
    assert mc.resolve_seek_target("seek_by", position_s=1000,
                                  duration_s=5700, seconds=600) == 1600.0
    assert mc.resolve_seek_target("seek_by", position_s=1000,
                                  duration_s=5700, seconds=-2000) == 0.0
    assert mc.resolve_seek_target("seek_by", position_s=5600,
                                  duration_s=5700, seconds=600) == 5700.0
    with pytest.raises(ValueError):
        mc.resolve_seek_target("seek_by", position_s=1000,
                               duration_s=5700, seconds=None)
    with pytest.raises(ValueError):
        mc.resolve_seek_target("seek_by", position_s=None,
                               duration_s=5700, seconds=60)


# ── slider math ──────────────────────────────────────────────────────────────

def test_slider_fraction():
    assert mc.slider_fraction(0, 100, 50) == 0.5
    assert mc.slider_fraction(0, 5700, 2850) == 0.5
    assert mc.slider_fraction(10, 110, 60) == 0.5
    assert mc.slider_fraction(0, 100, 250) == 1.0     # clamped
    assert mc.slider_fraction(0, 100, -5) == 0.0      # clamped
    assert mc.slider_fraction(5, 5, 5) is None        # no range
    assert mc.slider_fraction(0, None, 5) is None


def test_slider_value_for_fraction():
    assert mc.slider_value_for_fraction(0, 100, 0.5) == 50.0
    assert mc.slider_value_for_fraction(0, 5700, 0.5) == 2850.0
    assert mc.slider_value_for_fraction(10, 110, 0.5) == 60.0
    assert mc.slider_value_for_fraction(0, 100, 2.0) == 100.0


def test_slider_seconds_round_trip_through_duration():
    # A percent slider (0-100) still carries exact seconds when the
    # duration is known from the player's labels.
    value = mc.slider_value_for_seconds(0, 100, 5700, 2850)
    assert value == 50.0
    assert mc.seconds_from_slider(0, 100, value, 5700) == 2850.0
    assert mc.seconds_from_slider(0, 100, 50, None) is None
    assert mc.slider_value_for_seconds(0, 100, None, 60) is None


# ── timecode labels ──────────────────────────────────────────────────────────

def test_label_pair_position_and_duration():
    pos, dur = mc.parse_label_timecodes(["47:30", "1:35:00"])
    assert (pos, dur) == (2850.0, 5700.0)


def test_label_pair_in_one_string():
    pos, dur = mc.parse_label_timecodes(["47:30 / 1:35:00"])
    assert (pos, dur) == (2850.0, 5700.0)


def test_label_duration_is_the_largest():
    # Labels arriving in either order still resolve the same way.
    pos, dur = mc.parse_label_timecodes(["1:35:00", "47:30"])
    assert dur == 5700.0 and pos == 2850.0


def test_label_at_the_end():
    pos, dur = mc.parse_label_timecodes(["1:35:00", "1:35:00"])
    assert (pos, dur) == (5700.0, 5700.0)


def test_labels_without_a_pair_say_nothing():
    assert mc.parse_label_timecodes(["47:30"]) is None
    assert mc.parse_label_timecodes(["No time here"]) is None
    assert mc.parse_label_timecodes([]) is None
    assert mc.parse_label_timecodes(None) is None


def test_position_phrase():
    phrase = mc.position_phrase(2850, 5700)
    assert "47:30 of 1:35:00" in phrase
    assert mc.position_phrase(2850, None) == "47:30"
    assert mc.position_phrase(None, 5700) == ""
