"""
core/scheduler.py — the in-app scheduler (ARIA's, ported).

ARIA kept timers, one-shot reminders and recurring tasks in one
store and one loop, and her tool answers were the store's contents
read back. LVI already had a `reminder` action, but it writes
self-deleting scripts into the OS task scheduler — a different
animal: those survive the app closing, and the app cannot list or
cancel them. This module is the in-app half ARIA had: tasks the
assistant herself owns, lists and cancels, fired through her own
speech while she runs.

STORE — memory/scheduler.json (JSON, beside the other LVI stores;
ARIA used sqlite rows in her memory DB). Tasks:
    {id, kind, prompt, interval_s, next_run (epoch), created}
Kinds: once (speak the prompt), timer (speak "Timer done: …"),
interval / brief (run the prompt as a turn), screenwatch and
pricecheck (recurring passes owned by their own modules, created
through add_task — the same wiring ARIA had), and briefing (Phase
G4: a daily time-of-day task, below).

FIRING — tick() is the whole loop body, driven by main.py's
_run_scheduler task, which is created in the same session TaskGroup
as the proactive mode and ticks every couple of seconds — the
proactive engine's lifecycle, not a second unmanaged loop. tick()
takes its clock as an argument, so tests fire tasks with an
accelerated clock instead of sleeping.

DELIVERY DISCIPLINE — a task only counts as fired when the bound
hook says the line actually went out (main.py binds hooks that
report whether a session exists to speak into). An undelivered
one-shot stays due and retries on the next tick: a reminder set
before a reconnect is spoken late, never silently eaten. Recurring
tasks advance from the moment they fire, as ARIA's did.

PHASE G4 — THE PROACTIVE JOINS. Three connections, no new engines:
  * kind "briefing": a daily briefing at a set time of day. The
    record carries "time" ("07:30") and an "enabled" flag; when
    due and enabled it fires through the bound briefing hook —
    main.py composes the fixed-facts briefing (the startup
    greeting's own composition) and delivers it as a spoken turn.
    Undelivered (no session, or she is asleep) it stays due and
    retries, the store's ordinary discipline — which is also the
    whole catch-up story: a briefing whose time passed while she
    was off fires ONCE at the next awake session, then advances
    to the next day. Disabled, it advances silently.
  * Price crossings and screen-watch changes speak through
    speak_alert(), gated by config's proactive_alerts flag: off
    means the passes still run and the hits are logged, just not
    said aloud. A scheduled briefing is never gated by it.
  * A screen-watch change is logged here whether spoken or not,
    so a hit never exists only in a voice nobody heard.

TWO DIFFERENCES FROM ARIA, both deliberate:
  * Her timers were threading.Timer — in-memory, gone on restart.
    Here timers persist in the store like everything else (the
    migration brief requires persistence across restarts).
  * Her break-reminder toggle was a module global, also gone on
    restart. Here the toggle — and the last-nudge stamp — persist.
"""

from __future__ import annotations

import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional

_BRIEF_PROMPT_RE = re.compile(r"morning\s*brief", re.IGNORECASE)

BREAK_INTERVAL_S = 90 * 60
BREAK_TEXT = ("Break reminder: you've been going a while — stretch, "
              "look away from the screen, and get some water.")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch this constant to a tmp path (the routines pattern).
STORE_PATH = _base_dir() / "memory" / "scheduler.json"

_lock = threading.RLock()


def _store_path() -> Path:
    return STORE_PATH


def _empty() -> dict:
    return {"next_id": 1, "tasks": [], "break_reminders": False,
            "last_break_nudge": None}


def _load() -> dict:
    try:
        data = json.loads(_store_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            out = _empty()
            out.update(data)
            return out
    except Exception:
        pass
    return _empty()


def _save(data: dict) -> None:
    path = _store_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


# ── Hooks (bound once by main.py — the routines/confirm pattern) ─────────────

_say_hook: Optional[Callable[[str], bool]] = None
_run_hook: Optional[Callable[[str], bool]] = None
_briefing_hook: Optional[Callable[[], bool]] = None


def bind(say=None, run_prompt=None, briefing=None) -> None:
    """Wire the firing hooks to the live session. say(text) announces
    an exact line; run_prompt(text) injects a prompt as a turn;
    briefing() delivers the composed daily briefing. All return
    whether the delivery actually went out."""
    global _say_hook, _run_hook, _briefing_hook
    _say_hook = say
    _run_hook = run_prompt
    _briefing_hook = briefing


def speak(text: str) -> bool:
    """Announce a line from a background module (screen watches and
    price alerts speak through here, so there is one speech binding
    for everything that is not a tool call). False when nothing is
    bound or the session could not take it."""
    if _say_hook is None:
        return False
    try:
        result = _say_hook(text)
    except Exception:
        return False
    return True if result is None else bool(result)


def _run_prompt(text: str) -> bool:
    if _run_hook is None:
        return False
    try:
        result = _run_hook(text)
    except Exception:
        return False
    return True if result is None else bool(result)


def proactive_alerts_enabled() -> bool:
    """Config's proactive_alerts flag AND the presence layer, read
    at alert time (Phase G4; presence added in Presence v1). An
    unreadable config defaults to ON — the flag silences her on
    purpose, never by accident. Presence is fail-open the same
    way: only a positively-known absence silences a proactive
    alert, never a broken probe."""
    try:
        from memory.config_manager import get_proactive_alerts
        if not bool(get_proactive_alerts()):
            return False
    except Exception:
        pass
    try:
        from core import presence
        return bool(presence.is_present())
    except Exception:
        return True


def speak_alert(text: str) -> bool:
    """Speak a proactive watch alert (a price crossing, a screen
    change) — speak()'s flag-gated sibling. False when alerts are
    muted as well as when nothing could take the line, so the
    caller can log what was not said."""
    if not proactive_alerts_enabled():
        return False
    return speak(text)


def _call_briefing() -> bool:
    if _briefing_hook is None:
        return False
    try:
        result = _briefing_hook()
    except Exception:
        return False
    return True if result is None else bool(result)


# ── Store operations ─────────────────────────────────────────────────────────

def add_task(kind: str, prompt: str, delay_s: int = 0, interval_s: int = 0,
             now: Optional[float] = None) -> int:
    """Add a task; returns its id. next_run is now + delay for
    one-shots, now + interval for recurring kinds (ARIA's rule)."""
    now = time.time() if now is None else now
    nxt = now + (delay_s if delay_s else interval_s)
    with _lock:
        data = _load()
        task = {"id": data["next_id"], "kind": kind, "prompt": prompt,
                "interval_s": int(interval_s or 0), "next_run": nxt,
                "created": now}
        data["next_id"] += 1
        data["tasks"].append(task)
        _save(data)
        return task["id"]


def list_rows() -> list[dict]:
    """All tasks, soonest first (ARIA listed ORDER BY next_run)."""
    with _lock:
        rows = [dict(t) for t in _load()["tasks"]]
    rows.sort(key=lambda t: t.get("next_run") or 0)
    return rows


def cancel_task(task_id: int) -> bool:
    with _lock:
        data = _load()
        before = len(data["tasks"])
        data["tasks"] = [t for t in data["tasks"]
                         if t.get("id") != int(task_id)]
        if len(data["tasks"]) == before:
            return False
        _save(data)
        return True


def count() -> int:
    with _lock:
        return len(_load()["tasks"])


# ── Tool-facing helpers (ARIA's tool_* semantics) ────────────────────────────

def parse_duration(s: str) -> int:
    """ARIA's duration parser, verbatim: '20 minutes', '1h30m',
    '90s'; a bare number means minutes. 0 when nothing parses."""
    s = str(s or "").lower().strip()
    total = 0.0
    for num, unit in re.findall(
            r"(\d+(?:\.\d+)?)\s*(hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)(?![a-zA-Z])",
            s):
        n = float(num)
        u = unit[0]
        total += n * 3600 if u == "h" else n * 60 if u == "m" else n
    if total == 0 and s.replace(".", "", 1).isdigit():
        total = float(s) * 60
    return int(total)


def set_timer(duration_text: str, label: str = "timer",
              now: Optional[float] = None) -> tuple[Optional[int], int]:
    """(task id, seconds) or (None, 0) when the duration won't parse."""
    secs = parse_duration(duration_text)
    if secs <= 0:
        return None, 0
    tid = add_task("timer", label or "timer", delay_s=secs, now=now)
    return tid, secs


def set_recurring(interval_seconds: int, prompt: str,
                  now: Optional[float] = None) -> tuple[int, str]:
    """(task id, kind). A prompt asking for a morning brief becomes
    kind 'brief', as ARIA classified it; the firing is the same —
    the prompt runs as a turn and the briefing tool answers it."""
    kind = "brief" if _BRIEF_PROMPT_RE.search(prompt or "") else "interval"
    interval = max(60, int(interval_seconds or 0))
    tid = add_task(kind, prompt, interval_s=interval, now=now)
    return tid, kind


# ── The daily briefing task (Phase G4) ───────────────────────────────────────

_DAILY_TIME_RE = re.compile(
    r"^\s*(\d{1,2})(?::([0-5]\d))?\s*(am|pm)?\s*$", re.IGNORECASE)


def parse_daily_time(text) -> Optional[tuple[int, int]]:
    """'7:30', '07:30', '7:30 AM', '7 PM', '19:30' → (hour, minute)
    in 24-hour form, or None. With a meridiem the hour is 1–12 and
    a missing minute part means on the hour; without one the hour
    is read as 24-hour time."""
    m = _DAILY_TIME_RE.match(str(text or ""))
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    meridiem = (m.group(3) or "").lower()
    if meridiem:
        if not 1 <= hour <= 12:
            return None
        if meridiem == "pm" and hour != 12:
            hour += 12
        elif meridiem == "am" and hour == 12:
            hour = 0
    elif hour > 23:
        return None
    return hour, minute


def _fmt_daily_time(hour: int, minute: int) -> str:
    suffix = "PM" if hour >= 12 else "AM"
    return f"{hour % 12 or 12}:{minute:02d} {suffix}"


def _next_daily(now: float, hour: int, minute: int) -> float:
    """The next local occurrence of hour:minute strictly after
    `now` — wall-clock, so a 7:30 briefing stays a 7:30 briefing
    across a daylight-saving shift."""
    cand = datetime.fromtimestamp(now).replace(
        hour=hour, minute=minute, second=0, microsecond=0)
    if cand.timestamp() <= now:
        cand += timedelta(days=1)
    return cand.timestamp()


def _briefing_prompt(task: dict) -> str:
    parsed = parse_daily_time(task.get("time") or "")
    label = (_fmt_daily_time(*parsed) if parsed
             else (task.get("time") or "?"))
    base = f"Daily briefing at {label}"
    return base if task.get("enabled", True) else base + " (disabled)"


def get_briefing_task() -> Optional[dict]:
    """The daily-briefing task record, or None. There is at most
    one: setting it again moves the time rather than stacking."""
    with _lock:
        for t in _load()["tasks"]:
            if t.get("kind") == "briefing":
                return dict(t)
    return None


def set_daily_briefing(time_text: str, enabled: bool = True,
                       now: Optional[float] = None
                       ) -> tuple[Optional[int], Optional[str]]:
    """Create or move the daily briefing. Returns (task id,
    normalised "HH:MM"), or (None, None) when the time won't
    parse. next_run is the next occurrence of the time of day."""
    parsed = parse_daily_time(time_text)
    if parsed is None:
        return None, None
    hour, minute = parsed
    now = time.time() if now is None else now
    time_str = f"{hour:02d}:{minute:02d}"
    with _lock:
        data = _load()
        task = next((t for t in data["tasks"]
                     if t.get("kind") == "briefing"), None)
        if task is None:
            task = {"id": data["next_id"], "kind": "briefing",
                    "prompt": "", "interval_s": 86400, "created": now}
            data["next_id"] += 1
            data["tasks"].append(task)
        task["time"] = time_str
        task["enabled"] = bool(enabled)
        task["next_run"] = _next_daily(now, hour, minute)
        task["prompt"] = _briefing_prompt(task)
        _save(data)
        return task["id"], time_str


def set_briefing_enabled(enabled: bool,
                          now: Optional[float] = None) -> bool:
    """Flip the briefing task's enabled flag (and re-arm its next
    run from now, so a re-enabled briefing never fires for a time
    that passed while it was off). False when no briefing exists."""
    now = time.time() if now is None else now
    with _lock:
        data = _load()
        task = next((t for t in data["tasks"]
                     if t.get("kind") == "briefing"), None)
        if task is None:
            return False
        task["enabled"] = bool(enabled)
        parsed = parse_daily_time(task.get("time") or "")
        if parsed is not None:
            task["next_run"] = _next_daily(now, *parsed)
        task["prompt"] = _briefing_prompt(task)
        _save(data)
        return True


def _fmt_next(epoch: float) -> str:
    try:
        return datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(epoch)


_OS_NOTE = ("(Reminders set with the reminder tool live in the OS task "
            "scheduler, not here — those are separate and are not "
            "listed.)")


def render_list(rows: list[dict]) -> str:
    if not rows:
        return "No scheduled tasks.\n" + _OS_NOTE
    lines = [f"#{t['id']} [{t['kind']}] next: {_fmt_next(t['next_run'])}"
             f" | {t['prompt']}" for t in rows]
    lines.append(_OS_NOTE)
    return "\n".join(lines)


def break_reminders_enabled() -> bool:
    with _lock:
        return bool(_load().get("break_reminders"))


def set_break_reminders(enabled: bool, now: Optional[float] = None) -> bool:
    """Persist the toggle. Turning it ON stamps the nudge clock, so
    the first nudge comes a full interval later, not immediately."""
    now = time.time() if now is None else now
    with _lock:
        data = _load()
        data["break_reminders"] = bool(enabled)
        if enabled and not data.get("last_break_nudge"):
            data["last_break_nudge"] = now
        _save(data)
        return bool(enabled)


# ── The loop body ────────────────────────────────────────────────────────────

def tick(now: Optional[float] = None) -> list[str]:
    """Fire everything due at `now`. Returns log lines.

    Delivery-gated, per the module docstring: a one-shot whose line
    could not be spoken stays in the store and retries next tick.
    screenwatch/pricecheck tasks advance BEFORE their pass runs (the
    pass is the work; a failing pass must not spin at tick rate), and
    the pass itself is guarded so a broken watch can never wedge the
    loop for everything else."""
    now = time.time() if now is None else now
    logs: list[str] = []
    with _lock:
        data = _load()
        tasks = data["tasks"]
        due = sorted((t for t in tasks if (t.get("next_run") or 0) <= now),
                     key=lambda t: t.get("next_run") or 0)
        changed = False
        for task in due:
            kind = task.get("kind")
            tid = task.get("id")
            prompt = task.get("prompt") or ""
            if kind in ("once", "timer"):
                line = prompt if kind == "once" else f"Timer done: {prompt}."
                if speak(line):
                    tasks.remove(task)
                    changed = True
                    logs.append(f"{kind} #{tid} fired")
                else:
                    logs.append(f"{kind} #{tid} due but undelivered — "
                                f"will retry")
            elif kind in ("interval", "brief"):
                if _run_prompt(f"[Scheduled task] {prompt}"):
                    task["next_run"] = now + (task.get("interval_s") or 3600)
                    changed = True
                    logs.append(f"{kind} #{tid} fired")
                else:
                    logs.append(f"{kind} #{tid} due but undelivered — "
                                f"will retry")
            elif kind == "screenwatch":
                task["next_run"] = now + (task.get("interval_s") or 300)
                changed = True
                try:
                    from core import screenwatch
                    result = screenwatch.check_watch(prompt)
                    if "change detected" in (result or ""):
                        logs.append(f"screenwatch #{tid}: {result}")
                except Exception as e:
                    logs.append(f"screenwatch #{tid} check failed: {e}")
            elif kind == "pricecheck":
                task["next_run"] = now + (task.get("interval_s") or 3600)
                changed = True
                try:
                    from core import pricecheck
                    hits: list = []
                    _summary, alerts = pricecheck.check_watches(
                        hits=hits)
                    if proactive_alerts_enabled():
                        for hit in hits:
                            line = pricecheck.spoken_alert(hit)
                            if not speak(line):
                                logs.append(
                                    f"pricecheck #{tid}: alert "
                                    f"undelivered: {hit['label']}")
                            # The phone hears about a crossing too
                            # (core/notify.py): the same sentence she
                            # speaks, under its own title. Guarded
                            # like every pass in this loop — a broken
                            # notify can never wedge the tick. (While
                            # she sleeps the speak above queues the
                            # line and main's hook pushes it as a
                            # scheduler item; notify()'s same-body
                            # window folds the two into one event.)
                            try:
                                from core import notify as _notify
                                _notify.notify("Price watch", line,
                                               source="price_watch")
                            except Exception:
                                pass
                    else:
                        for alert in alerts:
                            logs.append(
                                f"pricecheck #{tid}: alert not spoken "
                                f"(proactive alerts off): {alert}")
                except Exception as e:
                    logs.append(f"pricecheck #{tid} failed: {e}")
            elif kind == "briefing":
                parsed = parse_daily_time(task.get("time") or "")
                if parsed is None:
                    task["next_run"] = now + 3600
                    changed = True
                    logs.append(f"briefing #{tid}: unusable time "
                                f"{task.get('time')!r} — rescheduled +1h")
                elif not task.get("enabled", True):
                    task["next_run"] = _next_daily(now, *parsed)
                    changed = True
                    logs.append(f"briefing #{tid} disabled — skipped")
                elif _call_briefing():
                    task["next_run"] = _next_daily(now, *parsed)
                    changed = True
                    logs.append(f"briefing #{tid} fired")
                else:
                    logs.append(f"briefing #{tid} due but undelivered "
                                f"— will retry")
            else:
                # An unknown kind must not re-fire every tick forever:
                # push it an hour out and say so once per firing.
                task["next_run"] = now + 3600
                changed = True
                logs.append(f"task #{tid}: unknown kind '{kind}' — "
                            f"rescheduled +1h")

        if data.get("break_reminders"):
            last = data.get("last_break_nudge")
            if last is None:
                data["last_break_nudge"] = now
                changed = True
            elif now - float(last) >= BREAK_INTERVAL_S:
                if speak(BREAK_TEXT):
                    data["last_break_nudge"] = now
                    changed = True
                    logs.append("break reminder fired")

        if changed:
            data["tasks"] = tasks
            _save(data)
    return logs
