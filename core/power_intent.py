"""
core/power_intent.py — the user's direct command, remembered for two minutes.

THE BUG THIS EXISTS FOR
    computer_settings can restart or shut down the computer, behind an
    on-screen confirmation. The gate was not the problem — the CALLER was.
    When the code watcher announced "My code changed — restarting now",
    the model heard a conversation full of the word "restart", with a tool
    whose vocabulary is stuffed with restart keywords, and called the
    restart action. Every self-restart put a "Restart this computer?"
    banner on the user's screen for him to cancel.

    A confirmation asks "may this happen?" — but nothing ever asked
    "did the USER actually request this?" Those are different questions,
    and only the second one separates a command from a coincidence of
    vocabulary.

THE SHAPE
    This module is a one-shot latch, armed only by the user's own words:

        note_user_text(text)   called at the seams where USER-originated
                               text enters a turn (typed input, the speech
                               transcript, phone-dashboard commands). If
                               the text is a direct power command, the
                               matching action is armed.
        consume(action)        called by computer_settings BEFORE it asks
                               for confirmation. True exactly once per
                               arm; the attempt spends the arm whether
                               the user then confirms or cancels.
        armed(action)          read-only peek, for tests and diagnostics.

    An arm lives 120 seconds: long enough for the turn it was spoken in,
    far too short to leak into a later, unrelated conversation.

WHAT COUNTS AS A DIRECT COMMAND
    Whole phrases whose object is the computer itself — "restart my
    computer", "shut the computer down", "power off". Deliberately NOT
    matched: "restart yourself", "restart the app", "the restart worked",
    "restarting now to load the new version", "restart the download".
    ARIA restarting HERSELF is the normal case this module exists to
    protect; her own announcements, session summaries, briefings and
    tool results never pass through note_user_text, so they can never
    arm anything.

    Never raises: a latch that can crash the input path would be worse
    than the bug it fixes.
"""
from __future__ import annotations

import re
import threading
import time

TTL_SECONDS = 120.0

_ACTIONS = ("restart", "shutdown")

_lock = threading.Lock()
_armed_until: dict[str, float] = {}


def _now() -> float:
    return time.monotonic()


# ── Detection ────────────────────────────────────────────────────────────────
#
# The object of the verb must be the machine itself: computer, pc or
# machine (with an optional my/the). "power off" is the one bare phrase —
# it has no other plausible object in a command to an assistant.

_OBJ = r"(?:my\s+|the\s+)?(?:computer|pc|machine)"

_RESTART_PATTERNS = (
    re.compile(rf"\b(?:restart|reboot)\s+{_OBJ}\b"),
)

_SHUTDOWN_PATTERNS = (
    re.compile(rf"\bshut\s+{_OBJ}\s+down\b"),
    re.compile(rf"\bturn\s+{_OBJ}\s+off\b"),
    re.compile(rf"\b(?:shut\s*down|shutdown)\s+{_OBJ}\b"),
    re.compile(rf"\bturn\s+off\s+{_OBJ}\b"),
    re.compile(rf"\bpower\s+off\s+{_OBJ}\b"),
    re.compile(r"\bpower\s+off\b"),
)


def detect_power_intent(text) -> str | None:
    """'restart' | 'shutdown' | None — whole-phrase match on user text."""
    try:
        if not text:
            return None
        low = " ".join(str(text).lower().split())
        for pattern in _SHUTDOWN_PATTERNS:
            if pattern.search(low):
                return "shutdown"
        for pattern in _RESTART_PATTERNS:
            if pattern.search(low):
                return "restart"
        return None
    except Exception:
        return None


# ── The latch ────────────────────────────────────────────────────────────────

def arm(action: str, ttl_s: float = TTL_SECONDS, now: float | None = None) -> None:
    """Arm one power action for ttl_s seconds. One-shot: see consume()."""
    try:
        if action not in _ACTIONS:
            return
        at = _now() if now is None else now
        with _lock:
            _armed_until[action] = at + ttl_s
    except Exception:
        pass


def armed(action: str, now: float | None = None) -> bool:
    """True while an unspent, unexpired arm exists for action."""
    try:
        if action not in _ACTIONS:
            return False
        at = _now() if now is None else now
        with _lock:
            until = _armed_until.get(action)
            if until is None:
                return False
            if at >= until:
                _armed_until.pop(action, None)
                return False
            return True
    except Exception:
        return False


def consume(action: str, now: float | None = None) -> bool:
    """Spend the arm for action. True exactly once per arm.

    The spend happens on the ATTEMPT, before any confirmation is shown:
    if the user cancels the confirmation, the arm is still gone and a
    fresh spoken/typed command is what re-arms it. An arm that survived
    a cancelled attempt could be picked up minutes later by a call the
    user never made — which is the bug this module exists to prevent.
    """
    try:
        if action not in _ACTIONS:
            return False
        at = _now() if now is None else now
        with _lock:
            until = _armed_until.pop(action, None)
            return until is not None and at < until
    except Exception:
        return False


def note_user_text(text) -> str | None:
    """The arming seam: feed USER-originated text here, and only here.

    Returns the action armed, or None. Only text the user typed, said,
    or sent from their phone may be passed — never announcements,
    summaries, briefings or tool results.
    """
    action = detect_power_intent(text)
    if action:
        arm(action)
    return action


def _reset() -> None:
    """Test support: drop every arm."""
    try:
        with _lock:
            _armed_until.clear()
    except Exception:
        pass
