"""
core/screenwatch.py — watch the screen for an answer changing
(ARIA's Module 7, ported).

A watch is a question about the screen — "has the build finished?",
"what's the order status?" — polled on an interval. Each poll
captures the screen and asks the question of the vision side-call;
when the answer changes meaningfully (ARIA's difflib rule, ratio
below 0.85), the watch speaks up. State lives at
memory/screen_watches.json, ARIA's shape: question, interval, the
scheduler task id, last answer, last checked.

Wiring is ARIA's, translated: creating a watch creates a recurring
in-app scheduler task of kind "screenwatch" whose prompt is the
watch name; the scheduler tick advances the task and calls
check_watch() here. The alert goes out through core/scheduler's
bound speech binding — the one every background module shares —
via speak_alert() (Phase G4), so the proactive_alerts flag can
mute the speech while the change is still recorded and logged.

The vision ask is one injectable function (ask_screen) so tests
drive checks with stubbed answers and no display.
"""

from __future__ import annotations

import difflib
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Optional

MIN_INTERVAL_S = 60
DIFF_THRESHOLD = 0.85
_NAME_RE = re.compile(r"[^a-z0-9_-]")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch this constant to a tmp path (the routines pattern).
STORE_PATH = _base_dir() / "memory" / "screen_watches.json"


def _store_path() -> Path:
    return STORE_PATH


def sanitize_name(name) -> str:
    """ARIA's rule, verbatim: lowercase, alnum + _/- only (anything
    else is REMOVED, not replaced), max 40 chars."""
    return _NAME_RE.sub("", str(name or "").lower().strip())[:40]


def _load() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save(watches: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(watches, indent=2), encoding="utf-8")


def answers_differ(old: str, new: str,
                   threshold: float = DIFF_THRESHOLD) -> bool:
    """Pure comparator, ARIA's verbatim: True if the screen answer
    changed meaningfully (SequenceMatcher ratio < 0.85)."""
    old, new = (old or "").strip(), (new or "").strip()
    if old == new:
        return False
    if not old or not new:
        return True
    ratio = difflib.SequenceMatcher(None, old, new).ratio()
    return ratio < threshold


def ask_screen(question: str) -> str:
    """Capture the screen and ask `question` of it — the vision
    side-call (core/gemini's ladder), over the same capture main.py's
    screen_process uses. Raises on any failure; check_watch turns
    that into a failed check with the state untouched."""
    from google.genai import types as gtypes

    from actions.screen_processor import _capture_screen
    from core import gemini

    img_bytes, mime = _capture_screen()
    answer = gemini.text(
        [gtypes.Part.from_bytes(data=img_bytes, mime_type=mime),
         f"Answer this question about the screen, briefly and "
         f"factually: {question}"],
        tier=gemini.FAST, timeout_ms=30_000)
    if not answer:
        raise RuntimeError("the vision call returned no answer")
    return answer


def check_watch(name: str,
                ask: Optional[Callable[[str], str]] = None) -> str:
    """Run one check for a watch. On a meaningful change, speaks the
    alert through the scheduler's bound hook. A failed check (no
    capture, no answer) changes NO state — a broken poll must not
    become the baseline the next poll is compared against."""
    name = sanitize_name(name)
    if not name:
        return "[Invalid watch name.]"
    watches = _load()
    watch = watches.get(name)
    if not watch:
        return f"[No screen watch named '{name}'.]"
    ask = ask or ask_screen
    question = watch.get("question", "")
    try:
        answer = str(ask(question) or "")
    except Exception as e:
        return f"[Screen check failed: {e}]"
    if not answer.strip():
        return "[Screen check failed: the vision call returned an empty answer]"

    last = watch.get("last_answer")
    watch["last_answer"] = answer
    watch["last_checked"] = time.strftime("%Y-%m-%d %H:%M:%S")
    _save(watches)

    if last is not None and answers_differ(last, answer):
        from core import scheduler
        line = (f"Screen watch '{name}': the answer to '{question}' "
                f"changed. Before: {str(last)[:150]} "
                f"Now: {answer[:150]}")
        spoken = scheduler.speak_alert(line)
        if not spoken and not scheduler.proactive_alerts_enabled():
            return (f"Screen watch '{name}': change detected "
                    f"(not spoken — proactive alerts are off).")
        return f"Screen watch '{name}': change detected."
    return f"Screen watch '{name}': no change."


def watch_screen(name: str, question: str,
                 interval_s: int = 300) -> tuple[bool, str, Optional[dict]]:
    """Create a watch + its scheduler task. (ok, message, watch)."""
    name = sanitize_name(name)
    if not name:
        return False, ("Invalid watch name — use lowercase letters, "
                       "numbers, _ or -."), None
    if not str(question or "").strip():
        return False, ("A question is required — what should I look "
                       "for on the screen?"), None
    try:
        interval = int(interval_s)
    except (TypeError, ValueError):
        interval = 300
    if interval < MIN_INTERVAL_S:
        return False, (f"Minimum watch interval is {MIN_INTERVAL_S}s "
                       f"— try a longer interval."), None
    watches = _load()
    if name in watches:
        return False, (f"Already watching '{name}'. Use unwatch_screen "
                       f"first to replace it."), None
    from core import scheduler
    task_id = scheduler.add_task("screenwatch", name, interval_s=interval)
    watch = {
        "question": str(question).strip(),
        "interval_s": interval,
        "sched_task_id": task_id,
        "last_answer": None,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "last_checked": None,
    }
    watches[name] = watch
    _save(watches)
    return True, (f"Watching for '{question}' every {interval}s "
                  f"(watch '{name}', check task #{task_id}). I'll "
                  f"speak up when it changes."), watch


def unwatch_screen(name: str) -> tuple[bool, str]:
    """Stop a watch and cancel its scheduler task."""
    name = sanitize_name(name)
    watches = _load()
    watch = watches.pop(name, None)
    if not watch:
        return False, f"No screen watch named '{name}'."
    task_id = watch.get("sched_task_id")
    if task_id:
        try:
            from core import scheduler
            scheduler.cancel_task(int(task_id))
        except Exception:
            pass
    _save(watches)
    return True, f"Stopped watching '{name}'."


def list_watches() -> dict:
    return _load()


def render_list(watches: dict) -> str:
    """ARIA's list format."""
    if not watches:
        return "[No active screen watches.]"
    lines = []
    for name, w in watches.items():
        last = w.get("last_checked") or "never checked"
        lines.append(f"'{name}' — every {w.get('interval_s', '?')}s, "
                     f"last checked {last}\n  Q: {w.get('question', '')}")
    return "\n".join(lines)
