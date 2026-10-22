"""
core/confirm.py — a confirmation the model cannot forge.

THE PROBLEM WITH THE OLD GATE
    computer_settings guarded shutdown and restart like this:

        confirmed = str(params.get("confirmed", "")).lower()
        if confirmed not in ("yes", "true", "1", "confirm"):
            return "Please confirm by calling again with confirmed=yes."

    `confirmed` is a tool parameter, which means the *model* writes it. Nothing
    stops it from sending confirmed=yes on the first call, and nothing checks
    that a human was ever involved. It is a convention, not a gate — and its
    coverage was two actions, so deleting files and switching off the WiFi the
    assistant is talking over went through with no gate at all.

THE DESIGN HERE
    The confirmation token is issued by the *interface*, never by the model:

      1. An action calls `request(...)` with a callable that does the real work.
      2. This module hands the UI a banner with CONFIRM / CANCEL and returns
         IMMEDIATELY with a sentence for the model to say out loud.
      3. If — and only if — the user presses CONFIRM, the UI calls `resolve()`,
         which runs the stored callable off the Qt thread.

    Nothing blocks. The model keeps talking while the banner is up, so this
    costs no latency at all; in fact it is cheaper than the old gate, which
    burned two tool round trips (reject, then re-call) on every shutdown.

WHAT BELONGS HERE AND WHAT DOES NOT
    Only genuinely irreversible things. Anything that can be reversed should be
    done at once and pushed onto core/undo.py instead — undo is faster than a
    question, and an assistant that asks before every action is one nobody uses.

THE ONE EXCEPTION
    A routine the user has explicitly trusted replays inside `trusted_scope()`
    (bottom of this file): its destructive steps run without a banner, because
    trusting the routine was the human answering this gate's question in
    advance, for a step list they can inspect. Untrusted routines get no such
    passage — their destructive steps park here like any live call.
"""

from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

# A pending confirmation is abandoned after this long. Chosen to outlast a
# normal "hang on, let me look at the screen" pause without leaving a live
# shutdown button sitting on the HUD for the rest of the day.
TIMEOUT_SECONDS = 90.0


@dataclass
class _Pending:
    key:     str
    title:   str
    detail:  str
    run:     Callable[[], str]
    at:      float


_pending: Optional[_Pending] = None
_lock = threading.Lock()

# Set once at startup by main.py. Signature: (title, detail) -> None for show,
# and () -> None for hide. Both are marshalled onto the Qt thread by the UI.
_show_cb: Optional[Callable[[str, str], None]] = None
_hide_cb: Optional[Callable[[], None]] = None
_log_cb:  Optional[Callable[[str], None]] = None


def bind(show, hide, log=None) -> None:
    """Wire this module to the HUD. Called once from main.py at startup."""
    global _show_cb, _hide_cb, _log_cb
    _show_cb, _hide_cb, _log_cb = show, hide, log


def _log(msg: str) -> None:
    if _log_cb:
        try:
            _log_cb(msg)
        except Exception:
            pass


def request(key: str, title: str, detail: str, run: Callable[[], str]) -> str:
    """Park an irreversible action behind the on-screen gate.

    Returns the sentence the tool should hand back to the model — phrased as an
    instruction so the assistant asks the user out loud, in English, in her
    own words, rather than reading this string verbatim."""
    global _pending

    if in_trusted_scope():
        # A trusted routine is replaying (core/routines.py). The human's
        # approval for this class of work was given when they trusted the
        # routine, so the gate's question has already been answered: run
        # the work now, on the caller's thread, and hand back its REAL
        # result instead of a pending sentence. No interface is needed —
        # there is nothing left to ask — so this precedes the unbound
        # refusal below. See the trusted-scope section at the end of this
        # file for why a module-global counter is the right shape here.
        try:
            result = run() or "Done."
        except Exception as e:
            return (f"The action '{title}' failed inside a trusted "
                    f"routine replay: {e}. Nothing else was done by "
                    f"this call.")
        _log(f"SYS: Trusted replay — {title}. {result}")
        return result

    if _show_cb is None:
        # No interface bound (headless, or a very early call). Refuse rather
        # than silently performing something irreversible.
        return (f"I cannot confirm '{title}' right now because the interface is "
                f"not available, so I have not done it.")

    with _lock:
        _pending = _Pending(key=key, title=title, detail=detail,
                            run=run, at=time.monotonic())

    try:
        _show_cb(title, detail)
    except Exception as e:
        with _lock:
            _pending = None
        return f"Could not ask for confirmation: {e}. Nothing was done."

    _log(f"SYS: Awaiting confirmation — {title}")
    return (
        f"[CONFIRMATION_PENDING] I have put a confirmation on screen for: {title}. "
        f"Say ONE short sentence in English telling them you need "
        f"them to confirm it on the HUD before you do it. Do not claim it is done."
    )


def resolve(accepted: bool) -> None:
    """Called by the UI when the user presses CONFIRM or CANCEL.

    Runs the stored callable on a worker thread — this is invoked from the Qt
    thread, and shutting the machine down from inside a button handler would
    freeze the interface on its way out."""
    global _pending

    with _lock:
        p, _pending = _pending, None

    if _hide_cb:
        try:
            _hide_cb()
        except Exception:
            pass

    if p is None:
        return

    if time.monotonic() - p.at > TIMEOUT_SECONDS:
        _log(f"SYS: Confirmation expired — {p.title}")
        return

    if not accepted:
        _log(f"SYS: Cancelled — {p.title}")
        return

    def _worker():
        try:
            result = p.run() or "Done."
            _log(f"SYS: Confirmed — {p.title}. {result}")
        except Exception as e:
            _log(f"ERR: {p.title} failed — {e}")

    threading.Thread(target=_worker, daemon=True,
                     name=f"confirm-{p.key}").start()


def pending_title() -> str:
    """'' when nothing is waiting. Lets an action avoid stacking two banners."""
    with _lock:
        if _pending is None:
            return ""
        if time.monotonic() - _pending.at > TIMEOUT_SECONDS:
            return ""
        return _pending.title


# ─────────────────────────────────────────────────────────────────────────────
#  Trusted scope — the one sanctioned way past the gate
# ─────────────────────────────────────────────────────────────────────────────
# A routine the user has explicitly trusted (core/routines.py) replays its
# steps without asking about each one: the trust WAS the confirmation, given
# once, for a named, inspectable list of steps the user can read back with
# describe_routine before trusting. While a trusted replay runs, it enters
# this scope around each step, and request() above honours it by running the
# parked callable immediately.
#
# WHY A MODULE-GLOBAL COUNTER AND NOT A THREAD-LOCAL OR A PARAMETER
#   The gate is consulted deep inside action handlers, on executor threads
#   the replay does not own: a replay dispatches a step, the step hops
#   threads twice (event loop, then worker pool) before an action calls
#   request(). A thread-local set by the replay never arrives there, and
#   threading a "preauthorized" parameter through every action signature —
#   ARIA's design — is exactly the model-visible surface this module exists
#   to remove. A plain counter is the shape that survives the hops.
#
# WHY THAT IS SAFE ENOUGH HERE
#   Replays are serialised by core/routines (one at a time, under a lock),
#   the scope is entered per step and left the moment the step returns, and
#   a replay occupies the conversation turn — the model is not issuing
#   unrelated gated calls concurrently. The window in which any gated call
#   would skip its question is the execution of a step in a routine the
#   user already trusted.
_trusted_lock  = threading.Lock()
_trusted_depth = 0


@contextlib.contextmanager
def trusted_scope():
    """Mark the enclosed dispatch as already-approved human intent.

    Only core/routines.py enters this, only around steps of a routine whose
    stored `trusted` flag is set. Nesting is counted, so a scope inside a
    scope behaves."""
    global _trusted_depth
    with _trusted_lock:
        _trusted_depth += 1
    try:
        yield
    finally:
        with _trusted_lock:
            _trusted_depth -= 1


def in_trusted_scope() -> bool:
    with _trusted_lock:
        return _trusted_depth > 0
