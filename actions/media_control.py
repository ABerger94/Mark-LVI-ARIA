"""
actions/media_control.py — one tool for "skip to halfway".

Until now the assistant could START a movie but not drive one:
the library plugin opens the file in Windows Media Player with
os.startfile and keeps no handle, and the HUD video player had
play/stop/mute and no seek at all. So "skip forward to halfway
through the movie" — something the user plainly expects, and the
old assistant could do — was answered with an apology.

This tool is the control surface for a movie ALREADY playing, in
either of the two players this app can actually drive:

  * the HUD video, when one is playing (the player facade's own
    position/duration/seek — exact, in milliseconds);
  * Windows Media Player, through core/wmp_control (UI Automation:
    the running player's real seek slider and time labels).

ROUTING IS IN THAT ORDER, and only ever to something that reports
itself playing/running. With neither, the tool fails in words —
"nothing controllable is playing" — rather than pressing a key at
whatever window happens to have focus and calling it done.

EVERY REPORT IS A RE-READ. A seek's answer is composed from the
position read AFTER the seek (core/mediacontrol's HUD hands poll
the cache until it settles; the WMP backend re-reads its slider),
so when a player lands on a keyframe fifty seconds short of the
target, the user hears where it actually landed — 46:40 — not the
47:30 that was requested. Play/pause and volume follow the same
rule.

The WMP backend is a module-level seam (`_wmp_backend`), replaced
by tests with a fake, the way the other seams in this codebase are
replaced; production uses core/wmp_control itself.
"""

from __future__ import annotations

from core import mediacontrol as _mc
from core import outcome

# The WMP backend seam: core.wmp_control's module functions by
# default. Imported lazily so a machine where the import itself is
# unhappy still gets the HUD half of this tool.
_wmp_backend = None


def _wmp():
    global _wmp_backend
    if _wmp_backend is None:
        from core import wmp_control
        _wmp_backend = wmp_control
    return _wmp_backend


_ACTIONS = ("position", "seek_percent", "seek_to", "seek_by",
            "play_pause", "play", "pause", "volume")

_SOURCE_HUD = "the HUD video"
_SOURCE_WMP = "Windows Media Player"


def _hud_active(player) -> bool:
    if player is None:
        return False
    fn = getattr(player, "video_is_playing", None)
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        return False


def _report(state: dict, source: str) -> str:
    """The spoken fragment for a state: a position phrase with its
    percent, e.g. "47:30 of 1:35:00 (50%)"."""
    phrase = _mc.position_phrase(state.get("position_s"),
                                  state.get("duration_s"))
    pct = _mc.percent_of(state.get("position_s"),
                         state.get("duration_s"))
    if phrase and pct is not None:
        phrase += f" ({pct}%)"
    return phrase


def _seek_sentence(verb: str, state: dict) -> str:
    """A seek's sentence from the RE-READ state, with the halfway
    call-out the user's own phrasing earns ("skip to halfway" →
    "Skipped to 47:30 of 1:35:00, about halfway")."""
    phrase = _mc.position_phrase(state.get("position_s"),
                                  state.get("duration_s"))
    pct = _mc.percent_of(state.get("position_s"),
                         state.get("duration_s"))
    if not phrase:
        return f"{verb}, but the new position could not be read back."
    if pct is not None and 48 <= pct <= 52:
        return f"{verb} to {phrase}, about halfway."
    if pct is not None:
        return f"{verb} to {phrase} ({pct}%)."
    return f"{verb} to {phrase}."


def _done(action: str, sentence: str, source: str, state: dict) -> str:
    return outcome.done("media_control", wanted=action, source=source,
                        position=state.get("position_s"),
                        duration=state.get("duration_s")) + "\n" + sentence


def _failed(action: str, reason: str) -> str:
    return outcome.failed("media_control", reason, wanted=action) \
        + "\n" + reason


# ── The two backends, behind one shape ───────────────────────────────────────

def _run_hud(action: str, params: dict, player) -> str:
    state = _mc.hud_state(player)
    if action == "position":
        phrase = _report(state, _SOURCE_HUD)
        if not phrase:
            return _failed(action, "The HUD video's position is not "
                                   "readable yet.")
        return _done(action, f"We're at {phrase} in {_SOURCE_HUD}.",
                     _SOURCE_HUD, state)
    if action in ("seek_percent", "seek_to", "seek_by"):
        try:
            target = _mc.resolve_seek_target(
                action, position_s=state.get("position_s"),
                duration_s=state.get("duration_s"),
                percent=params.get("percent"),
                timecode=params.get("timecode"),
                seconds=params.get("seconds"))
        except ValueError as e:
            return _failed(action, str(e))
        if not hasattr(player, "seek_video"):
            return _failed(action, "The HUD player cannot seek.")
        after = _mc.hud_seek(player, target)
        verb = "Skipped back" if action == "seek_by" \
            and (params.get("seconds") or 0) < 0 else "Skipped"
        return _done(action, _seek_sentence(verb, after),
                     _SOURCE_HUD, after)
    if action in ("play_pause", "play", "pause"):
        try:
            if action == "play_pause":
                after = _mc.hud_toggle(player)
            else:
                after = _mc.hud_set_playing(player, action == "play")
        except ValueError as e:
            return _failed(action, str(e))
        word = "Playing" if after.get("playing") else "Paused"
        return _done(action, f"{word} {_SOURCE_HUD}.", _SOURCE_HUD, after)
    if action == "volume":
        try:
            after = _mc.hud_set_volume(player, params.get("volume"))
        except (ValueError, TypeError) as e:
            return _failed(action, str(e) if isinstance(e, ValueError)
                           else "no volume level was given")
        if after.get("volume") is None:
            return _failed(action, "The HUD player's volume could "
                                   "not be read back.")
        return _done(action, f"Volume set to {after['volume']}.",
                     _SOURCE_HUD, after)
    return _failed(action, f"'{action}' is not a media control action")


def _run_wmp(action: str, params: dict) -> str | None:
    """The WMP half. Returns None when the player is not there at
    all (the caller then composes the nothing-playing failure); a
    string otherwise — including honest failures from a player that
    IS running but could not be driven."""
    backend = _wmp()
    state = backend.get_state()
    if not state.get("available"):
        return None
    if action == "position":
        phrase = _report(state, _SOURCE_WMP)
        if not phrase:
            return _failed(action, "Windows Media Player's position "
                                   "is not readable.")
        return _done(action, f"We're at {phrase} in {_SOURCE_WMP}.",
                     _SOURCE_WMP, state)
    if action in ("seek_percent", "seek_to", "seek_by"):
        if action == "seek_percent":
            after = backend.seek_percent(params.get("percent"))
        elif action == "seek_to":
            target = _mc.parse_timecode(params.get("timecode"))
            after = backend.seek_to(target)
        else:
            after = backend.seek_by(params.get("seconds"))
        if not after.get("ok", after.get("available")):
            return _failed(action, after.get("reason")
                           or "the seek did not happen")
        verb = "Skipped back" if action == "seek_by" \
            and (params.get("seconds") or 0) < 0 else "Skipped"
        return _done(action, _seek_sentence(verb, after),
                     _SOURCE_WMP, after)
    if action in ("play_pause", "play", "pause"):
        if action == "play_pause":
            after = backend.play_pause()
        else:
            after = backend.set_playing(action == "play")
        if not after.get("ok", after.get("available")):
            return _failed(action, after.get("reason")
                           or "the transport did not respond")
        word = "Playing" if after.get("playing") else "Paused"
        return _done(action, f"{word} {_SOURCE_WMP}.", _SOURCE_WMP, after)
    if action == "volume":
        after = backend.set_volume(params.get("volume"))
        if not after.get("ok", after.get("available")):
            return _failed(action, after.get("reason")
                           or "the volume did not move")
        if after.get("volume") is None:
            return outcome.unverified(
                "media_control",
                "the volume was set but could not be read back",
                wanted=action) + "\nVolume set, but not read back."
        return _done(action, f"Volume set to {after['volume']}.",
                     _SOURCE_WMP, after)
    return _failed(action, f"'{action}' is not a media control action")


def _validate(action: str, params: dict) -> str | None:
    """Input failures, in words, BEFORE anything is routed — a bad
    request must not move a player that happens to be running."""
    if action == "seek_percent":
        percent = params.get("percent")
        if percent is None or isinstance(percent, bool):
            return "no percentage was given"
        try:
            pct = float(percent)
        except (TypeError, ValueError):
            return f"'{percent}' is not a percentage"
        if pct < 0 or pct > 100:
            return (f"{percent} is not a percentage between 0 "
                    "and 100")
    if action == "seek_to":
        raw = params.get("timecode")
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            return "no time was given to skip to"
        if _mc.parse_timecode(raw) is None:
            return (f"'{raw}' is not a time I can use — try minutes "
                    "and seconds, like 47:30")
    if action == "seek_by":
        seconds = params.get("seconds")
        if seconds is None or isinstance(seconds, bool):
            return "no amount was given to skip by"
        try:
            float(seconds)
        except (TypeError, ValueError):
            return f"'{seconds}' is not a number of seconds"
    if action == "volume":
        volume = params.get("volume", params.get("value"))
        if volume is None:
            return "no volume level was given (0-100)"
        try:
            level = int(volume)
        except (TypeError, ValueError):
            return f"'{volume}' is not a volume level (0-100)"
        if level < 0 or level > 100:
            return f"{volume} is not a volume level between 0 and 100"
        params["volume"] = level
    return None


def media_control(parameters: dict = None, player=None,
                  session_memory=None) -> str:
    params = dict(parameters or {})
    action = (params.get("action") or "position").strip().lower()
    if action not in _ACTIONS:
        return outcome.ask(
            "media_control",
            f"'{action}' is not a media control action — position, "
            "seek_percent, seek_to, seek_by, play_pause, play, "
            "pause or volume. Which was meant?",
            wanted=action)
    problem = _validate(action, params)
    if problem:
        return _failed(action, problem)

    if _hud_active(player):
        return _run_hud(action, params, player)

    wmp_out = _run_wmp(action, params)
    if wmp_out is not None:
        return wmp_out

    state = _wmp().get_state()
    reason = state.get("reason") or "Windows Media Player is not running"
    text = ("Nothing controllable is playing — no video in the HUD, "
            f"and {reason[0].lower() + reason[1:]}.")
    return _failed(action, text)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "media_control",
    "description": (
        "Controls a movie or video that is ALREADY playing — in "
        "Windows Media Player, or in the HUD video player — and "
        "reports the position it read back afterwards. Use for "
        "'skip to halfway through the movie', 'go forward 10 "
        "minutes', 'go back 30 seconds', 'jump to 1:12:00', 'where "
        "are we in the movie', 'pause the movie', 'set the movie's "
        "volume to 40'. Actions: position (where are we), "
        "seek_percent (a percentage through the media — use 50 for "
        "halfway), seek_to (a 'timecode' like '47:30' or "
        "'1:12:00'), seek_by ('seconds', negative to go back), "
        "play_pause / play / pause, volume ('volume' 0-100). This "
        "does not START anything: to play a movie use "
        "media_player_search or video_player; come back here once "
        "it is on. If nothing controllable is playing the tool "
        "says so — believe it, and do not claim a skip happened."
    ),
    "brief": ("Control the playing movie: position, seek by "
              "percent/timecode/amount, play/pause, volume."),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": ("position | seek_percent | seek_to | "
                                "seek_by | play_pause | play | pause "
                                "| volume"),
                "enum": list(_ACTIONS),
            },
            "percent": {
                "type": "NUMBER",
                "description": "For seek_percent: 0-100 (50 = halfway)",
            },
            "timecode": {
                "type": "STRING",
                "description": ("For seek_to: where to land, like "
                                "'47:30' or '1:12:00'"),
            },
            "seconds": {
                "type": "NUMBER",
                "description": ("For seek_by: seconds to move, "
                                "negative to go back"),
            },
            "volume": {
                "type": "INTEGER",
                "description": "For volume: the level, 0-100",
            },
        },
        "required": ["action"],
    },
    "handler": media_control,
}
