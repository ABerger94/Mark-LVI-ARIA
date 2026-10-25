"""
core/mediacontrol.py — the arithmetic of "skip to halfway".

Every media-control surface in this app (the HUD video player, and
Windows Media Player driven through UI Automation) reduces to the
same handful of questions:

  * what does "1:12:00" mean in seconds, and what does 2850 seconds
    look like written back?
  * given a position and a duration, where is 50%? Where is "ten
    minutes from here"? And never past the end or before the start.
  * given a slider that lives in SOME unit (seconds, percent,
    milliseconds — the player decides), what fraction along is it,
    and what value puts it at a given moment of a known duration?
  * a player shows "47:30 / 1:35:00" somewhere in its UI: which is
    the position and which is the duration? (The duration is the
    largest timecode; the position is the other one. A player that
    shows only one timecode has told us nothing usable as a pair.)

All of that is pure — no Qt, no Windows, no imports beyond the
standard library — so it is testable without a machine that can
play anything, and both backends share exactly one implementation
of the maths instead of drifting apart.

THE HUD PLAYER'S HANDS live here too (hud_state / hud_seek /
hud_set_playing / hud_set_volume): they drive the duck-typed player
facade the actions already receive (position and duration in
milliseconds, a seek method, transport and volume methods) and they
import nothing, so the facade stays the only Qt-shaped thing in the
chain. A seek through these hands is followed by a short re-read
poll: the facade's seek is a queued Qt signal, so the position the
caller reads the instant after asking is the OLD position, and a
report composed from it would be a lie about where the movie went.
The poll waits until the read position is close to the target (or
the reads stop changing), bounded so a player that never lands
cannot hang the tool.
"""

from __future__ import annotations

import math
import re
import time

# ── Timecodes ────────────────────────────────────────────────────────────────

_TIMECODE_TOKEN = re.compile(
    r"\d{1,3}:\d{2}(?::\d{2})?(?:\.\d+)?|\d+(?:\.\d+)?")


def parse_timecode(value) -> float | None:
    """Seconds for a timecode, or None when it is not one.

    Accepts a number (already seconds) or a string in one of the
    shapes a person or a player writes:

        "90"        bare seconds
        "90.5"      bare seconds, fractional
        "12:34"     minutes:seconds
        "1:12:00"   hours:minutes:seconds

    In the colon shapes every component after the first is a real
    clock component and must be under 60 ("12:99" is garbage, not
    13:39), only the last component may be fractional, and a sign
    anywhere rejects the whole thing: "-30" as a seek TARGET is a
    caller bug (relative seeks carry their sign in seek_by's delta,
    which is a number, never a timecode).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        secs = float(value)
        return secs if math.isfinite(secs) and secs >= 0 else None
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or "-" in text or "+" in text:
        return None
    parts = text.split(":")
    if len(parts) > 3:
        return None
    if len(parts) == 1:
        try:
            secs = float(parts[0])
        except ValueError:
            return None
        return secs if math.isfinite(secs) and secs >= 0 else None
    total = 0.0
    for i, part in enumerate(parts):
        if not part or part != part.strip():
            return None
        try:
            num = float(part) if i == len(parts) - 1 else int(part)
        except ValueError:
            return None
        if num < 0 or (i > 0 and num >= 60):
            return None
        total = total * 60 + num
    return total


def format_timecode(seconds) -> str:
    """A position as a person reads it: "47:30" under an hour,
    "1:35:00" over it. Rounds to the nearest second and never goes
    negative — a position before the start is the start."""
    try:
        total = int(round(float(seconds)))
    except (TypeError, ValueError):
        return "0:00"
    total = max(0, total)
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


# ── Seek targets ─────────────────────────────────────────────────────────────

def clamp_target(target_s, duration_s) -> float:
    """A seek target held inside the media: never before 0, never
    past the duration when the duration is known."""
    target = max(0.0, float(target_s))
    if duration_s is not None and float(duration_s) > 0:
        target = min(target, float(duration_s))
    return target


def percent_to_seconds(percent, duration_s) -> float | None:
    """The moment at `percent` through the media, or None when the
    duration is unknown — a percentage of an unknown length is not
    a moment, and inventing one is how a tool ends up claiming it
    skipped to halfway through a file it never measured."""
    if duration_s is None or float(duration_s) <= 0:
        return None
    pct = max(0.0, min(100.0, float(percent)))
    return float(duration_s) * pct / 100.0


def resolve_seek_target(action: str, *, position_s=None, duration_s=None,
                        percent=None, timecode=None, seconds=None) -> float:
    """The absolute target (seconds) for one seek action.

    Raises ValueError with a speakable reason when the target cannot
    be computed — the callers turn that into an honest failure
    BEFORE anything moves, so a malformed request never becomes a
    random jump.
    """
    action = (action or "").strip().lower()
    if action == "seek_percent":
        if percent is None or isinstance(percent, bool):
            raise ValueError("no percentage was given")
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            raise ValueError(f"'{percent}' is not a percentage") from None
        if not math.isfinite(pct) or pct < 0 or pct > 100:
            raise ValueError(
                f"{percent} is not a percentage between 0 and 100")
        target = percent_to_seconds(pct, duration_s)
        if target is None:
            raise ValueError("the length of what is playing is not "
                             "known yet, so a percentage has nothing "
                             "to land on")
        return target
    if action == "seek_to":
        if timecode is None or (isinstance(timecode, str)
                                and not timecode.strip()):
            raise ValueError("no time was given to skip to")
        target = parse_timecode(timecode)
        if target is None:
            raise ValueError(f"'{timecode}' is not a time I can use — "
                             "try minutes and seconds, like 47:30")
        return clamp_target(target, duration_s)
    if action == "seek_by":
        if seconds is None or isinstance(seconds, bool):
            raise ValueError("no amount was given to skip by")
        try:
            delta = float(seconds)
        except (TypeError, ValueError):
            raise ValueError(f"'{seconds}' is not a number of "
                             "seconds") from None
        if not math.isfinite(delta):
            raise ValueError(f"'{seconds}' is not a number of seconds")
        if position_s is None:
            raise ValueError("the current position is not known, so "
                             "a relative skip has nothing to start from")
        return clamp_target(float(position_s) + delta, duration_s)
    raise ValueError(f"'{action}' is not a seek action")


# ── Slider math ──────────────────────────────────────────────────────────────

def slider_fraction(minimum, maximum, value) -> float | None:
    """Where along its range a slider sits, 0..1 — or None when the
    range is empty or unreadable. Unit-free on purpose: a player may
    range its seek slider in seconds, percent or milliseconds, and
    the fraction is the only reading that means the same in all
    three."""
    try:
        lo, hi, val = float(minimum), float(maximum), float(value)
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    return max(0.0, min(1.0, (val - lo) / (hi - lo)))


def slider_value_for_fraction(minimum, maximum, fraction) -> float | None:
    """The slider value at a fraction along its range (the inverse
    of slider_fraction), or None for an empty range."""
    try:
        lo, hi, frac = float(minimum), float(maximum), float(fraction)
    except (TypeError, ValueError):
        return None
    if hi <= lo:
        return None
    return lo + (hi - lo) * max(0.0, min(1.0, frac))


def slider_value_for_seconds(minimum, maximum, duration_s,
                             target_s) -> float | None:
    """The slider value that lands on `target_s` of a media whose
    duration is known: the target as a fraction of the duration,
    mapped onto the slider's range — correct whatever units the
    slider uses."""
    if duration_s is None or float(duration_s) <= 0 or target_s is None:
        return None
    return slider_value_for_fraction(
        minimum, maximum, float(target_s) / float(duration_s))


def seconds_from_slider(minimum, maximum, value, duration_s) -> float | None:
    """A slider reading as seconds into the media, through a known
    duration. None when the duration is unknown: a bare slider value
    is in the player's private units and calling it seconds would
    be a guess."""
    frac = slider_fraction(minimum, maximum, value)
    if frac is None or duration_s is None or float(duration_s) <= 0:
        return None
    return frac * float(duration_s)


# ── Timecode labels ──────────────────────────────────────────────────────────

def parse_label_timecodes(texts) -> tuple[float, float] | None:
    """(position_s, duration_s) from a player's text labels, or None.

    Players present the pair in several shapes — two Text elements
    ("47:30", "1:35:00"), one combined string ("47:30 / 1:35:00"),
    sometimes with extra numbers nearby (a volume level, a track
    count). The rule that survives all of them: the DURATION is the
    largest timecode present, and the POSITION is the first other
    timecode — labels are conventionally written position-first, so
    the first candidate wins ties of shape. Fewer than two timecodes
    is not a pair and returns None rather than half an answer.
    """
    if not texts:
        return None
    found: list[float] = []
    for text in texts:
        if not text:
            continue
        for token in _TIMECODE_TOKEN.findall(str(text)):
            secs = parse_timecode(token)
            if secs is not None:
                found.append(secs)
    if len(found) < 2:
        return None
    duration = max(found)
    position = next((s for s in found if s < duration), None)
    if position is None:
        position = duration          # every label agrees: at the end
    return position, duration


def position_phrase(position_s, duration_s) -> str:
    """A position as it is spoken: "47:30 of 1:35:00". With no
    duration known, just the position; with no position, nothing —
    the caller reports the absence instead."""
    if position_s is None:
        return ""
    pos = format_timecode(position_s)
    if duration_s is None or float(duration_s) <= 0:
        return pos
    return f"{pos} of {format_timecode(duration_s)}"


def percent_of(position_s, duration_s) -> int | None:
    """Whole percent through the media, or None when either side is
    unknown."""
    if position_s is None or duration_s is None or float(duration_s) <= 0:
        return None
    return int(round(100.0 * float(position_s) / float(duration_s)))


# ── The HUD player's hands (duck-typed facade, no Qt in here) ────────────────

def hud_state(player) -> dict:
    """The HUD video's state from the facade's cached readings.
    Positions arrive in milliseconds (Qt's unit) and leave in
    seconds (everyone else's). A missing facade method reads as
    unknown, never as zero."""
    def _ms(name):
        fn = getattr(player, name, None)
        if fn is None:
            return None
        try:
            value = fn()
        except Exception:
            return None
        return None if value is None else float(value)

    pos_ms, dur_ms = _ms("video_position_ms"), _ms("video_duration_ms")
    playing = None
    fn = getattr(player, "video_playback_playing", None)
    if fn is not None:
        try:
            playing = bool(fn())
        except Exception:
            playing = None
    volume = None
    fn = getattr(player, "video_volume", None)
    if fn is not None:
        try:
            volume = fn()
        except Exception:
            volume = None
    return {"position_s": None if pos_ms is None else pos_ms / 1000.0,
            "duration_s": None if dur_ms is None else dur_ms / 1000.0,
            "playing": playing,
            "volume": None if volume is None else int(volume)}


def hud_seek(player, target_s, *, sleep=time.sleep,
             timeout_s: float = 1.5) -> dict:
    """Seek the HUD video and RE-READ where it landed.

    The facade's seek is a queued Qt signal: the seek is only real
    once the player's position cache moves. So this polls the cache
    until it is within two seconds of the target (players land
    where keyframes let them, not necessarily on the exact frame)
    or the reads stop changing, bounded by `timeout_s`. The state
    returned is the last read — the report is composed from it,
    never from the requested target.
    """
    player.seek_video(int(round(float(target_s) * 1000)))
    deadline = time.monotonic() + timeout_s
    last_pos, stable = None, 0
    state = hud_state(player)
    while time.monotonic() < deadline:
        state = hud_state(player)
        pos = state["position_s"]
        if pos is not None and abs(pos - float(target_s)) <= 2.0:
            break
        if pos is not None and pos == last_pos:
            stable += 1
            if stable >= 3:
                break                    # settled somewhere else: report it
        else:
            stable = 0
        last_pos = pos
        sleep(0.05)
    return state


def hud_set_playing(player, playing: bool) -> dict:
    """Play or pause the HUD video through the facade, then re-read."""
    fn = getattr(player, "video_set_playing", None)
    if fn is not None:
        fn(bool(playing))
    elif playing:
        raise ValueError("the HUD player cannot be told to play")
    else:
        raise ValueError("the HUD player cannot be told to pause")
    return hud_state(player)


def hud_toggle(player) -> dict:
    """Toggle the HUD video's transport, then re-read."""
    fn = getattr(player, "video_toggle_playback", None)
    if fn is None:
        raise ValueError("the HUD player cannot be toggled")
    fn()
    return hud_state(player)


def hud_set_volume(player, level: int) -> dict:
    """Set the HUD video's volume through the facade, then re-read."""
    fn = getattr(player, "set_video_volume", None)
    if fn is None:
        raise ValueError("the HUD player's volume cannot be set")
    fn(max(0, min(100, int(level))))
    return hud_state(player)
