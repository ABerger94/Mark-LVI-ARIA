"""
core/delegation.py — background tasks, run without the conversation.

WHAT IT IS
    delegate_task hands a prompt to a worker: the prompt runs
    through the app's model WITH the app's tools — the same
    dispatch a spoken call uses — on worker threads, while the
    conversation goes on without it. The record lives in
    memory/delegated_tasks.json (atomic writes, the stores' own
    idiom): {id, prompt, status, result, created, started,
    finished}. task_status reads the record back; a status is
    never guessed, summarised or reconstructed from anything else.

THE ENGINE SEAM
    The model loop is this module's; the two capabilities it
    cannot own are bound by main.py, in the routines/scheduler
    pattern:

      * call_model(messages, tool_decls) -> (text, calls) — one
        model turn over the plain message protocol below.
        Production runs it over core/gemini's one-shot ladder
        (whose rungs include a configured provider, so the seam is
        the app's engine in both modes).
      * dispatch(name, args) -> str — one tool execution, through
        JarvisLive._execute_tool, exactly as a routine step runs:
        same registry, same gates, same outcome text.
      * tool_declarations() -> the declarations the delegated
        model may call (main's set, minus the exclusions below).

    Unbound — no session machinery yet — delegation refuses
    honestly rather than queueing work nobody can run.

    The message protocol is deliberately plain data, so the loop
    is testable without a vendor SDK in the room:
      {"role": "user",  "text": ...}
      {"role": "model", "text": ..., "calls": [{name, args}]}
      {"role": "tool",  "results": [{name, result}]}

THE GATE RULE
    Delegation is not a way around the confirmation gate. A
    delegated task's calls pass through the same dispatch as live
    ones; when a call parks behind the HUD banner, its result
    comes back [CONFIRMATION_PENDING] and the task STOPS there:
    status "blocked", the parked action named in the result. The
    banner is live — if Alek confirms, the parked act runs the way
    it would for a live call — but the task cannot see that act's
    outcome and does not pretend to; a fresh task can pick up from
    there. core/confirm's trusted scope is routines' sanction and
    is entered only by core/routines: delegation never enters it,
    so nothing a delegated task does executes ungated.

EXCLUSIONS (decided in main.py's binding, stated here so the rule
has one home): the delegated model is not offered delegate_task
itself (no recursion), shutdown_jarvis (a background task does
not end the app), run_routine (a trusted routine's pre-answered
gate is the user's answer for a replay THEY set going, not for a
task the model started), or the self-edit tools (their restart
belongs to the conversation's lifecycle, not a worker's).

CONCURRENCY + CANCELLATION
    Two workers, at most MAX_ACTIVE (4) tasks in flight (running +
    queued): delegate_task beyond that is refused outright, by a
    count read from the store. A silent unbounded backlog is how
    background systems lie; a refusal is a fact the user can act
    on. Queued tasks start in order as workers free.

    Cancellation is cooperative, and says so: a queued task is
    cancelled in the record and never runs; a running task is
    flagged and stops BETWEEN model turns — a model call already
    in flight finishes, because there is no honest way to un-send
    it, and the record says "cancelled" only once it has stopped.

THE STORE'S HONESTY RULES
    * The newest MAX_RECORDS (50) tasks are kept; a task in flight
      is never pruned out from under its worker.
    * A task still marked queued/running when the module is bound
      in a new process was interrupted by the app's death: it is
      marked failed ("interrupted — the app restarted") at bind
      time. A status claiming work is happening when nothing is,
      is a lie the store would otherwise keep telling forever.
    * A task gets at most MAX_TURNS (12) model turns. One that
      cannot finish in twelve reports exactly that, rather than
      wandering a worker thread for the rest of the day.

COMPLETION
    Reaching a terminal state (done / failed / blocked /
    cancelled) announces once through the scheduler's proactive
    speech seam — speak_alert, so the awake gate and the
    proactive_alerts flag apply, as they do to every other thing
    she says unbidden. Muted, asleep or unbound, the announcement
    simply does not happen; the record remains the truth, and
    task_status always knows.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

MAX_WORKERS = 2
MAX_ACTIVE = 4        # running + queued, beyond which delegation refuses
MAX_RECORDS = 50
MAX_TURNS = 12

TERMINAL = ("done", "failed", "blocked", "cancelled")


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch this constant to a tmp path (the scheduler pattern).
STORE_PATH = _base_dir() / "memory" / "delegated_tasks.json"

_lock = threading.RLock()
_call_model = None        # (messages, tool_decls) -> (text, calls)
_dispatch = None          # (name, args) -> result text
_tool_decls = None        # () -> [declaration dicts]
_executor: ThreadPoolExecutor | None = None
_cancel_flags: dict[int, threading.Event] = {}
_bound_once = False


# ── The store ──────────────────────────────────────────────────────────────

def _empty() -> dict:
    return {"next_id": 1, "tasks": []}


def _load() -> dict:
    try:
        data = json.loads(STORE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("tasks"), list):
            out = _empty()
            out.update(data)
            return out
    except Exception:
        pass
    return _empty()


def _save(data: dict) -> None:
    STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STORE_PATH.with_name(STORE_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, STORE_PATH)


def _prune(data: dict) -> None:
    tasks = data["tasks"]
    if len(tasks) <= MAX_RECORDS:
        return
    keep = []
    excess = len(tasks) - MAX_RECORDS
    for t in tasks:                      # oldest first in the store
        if excess > 0 and t.get("status") in TERMINAL:
            excess -= 1
            continue
        keep.append(t)
    data["tasks"] = keep


def _update(task_id: int, **fields) -> dict | None:
    with _lock:
        data = _load()
        for t in data["tasks"]:
            if t.get("id") == task_id:
                t.update(fields)
                _prune(data)
                _save(data)
                return dict(t)
    return None


def get_task(task_id: int) -> dict | None:
    """The record, read back — task_status's whole epistemology."""
    with _lock:
        for t in _load()["tasks"]:
            if t.get("id") == int(task_id):
                return dict(t)
    return None


def list_tasks(limit: int = 8) -> list[dict]:
    with _lock:
        rows = [dict(t) for t in _load()["tasks"]]
    rows.sort(key=lambda t: t.get("id") or 0, reverse=True)
    return rows[:limit]


def _active_count() -> int:
    with _lock:
        return sum(1 for t in _load()["tasks"]
                   if t.get("status") in ("queued", "running"))


# ── Binding (main.py, once per process) ────────────────────────────────────

def bind(call_model=None, dispatch=None, tool_declarations=None) -> None:
    """Wire the engine seam. The first bind in a process also
    closes out the previous process's in-flight records (see the
    module docstring's honesty rules)."""
    global _call_model, _dispatch, _tool_decls, _bound_once
    _call_model = call_model
    _dispatch = dispatch
    _tool_decls = tool_declarations
    if not _bound_once:
        _bound_once = True
        _recover_stale()


def _recover_stale() -> None:
    with _lock:
        data = _load()
        changed = False
        for t in data["tasks"]:
            if t.get("status") in ("queued", "running"):
                t["status"] = "failed"
                t["result"] = ("Interrupted — the app restarted "
                                "before this task finished.")
                t["finished"] = time.time()
                changed = True
        if changed:
            _prune(data)
            _save(data)


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                       thread_name_prefix="delegate")
    return _executor


# ── Delegation ─────────────────────────────────────────────────────────────

def delegate(prompt: str) -> tuple[int | None, str]:
    """Queue a background task. (task_id, "") on success, or
    (None, the honest refusal) — unbound, empty prompt, or the
    in-flight bound reached."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "nothing to delegate — the prompt was empty"
    if _call_model is None or _dispatch is None:
        return None, ("background tasks are not available until "
                      "the session machinery is running")
    with _lock:
        if _active_count() >= MAX_ACTIVE:
            return None, (f"{MAX_ACTIVE} background tasks are "
                          f"already in flight — let one finish "
                          f"first")
        data = _load()
        task = {"id": data["next_id"], "prompt": prompt,
                "status": "queued", "result": "",
                "created": time.time(), "started": None,
                "finished": None}
        data["next_id"] += 1
        data["tasks"].append(task)
        _prune(data)
        _save(data)
        _cancel_flags[task["id"]] = threading.Event()
    _ensure_executor().submit(_run_task, task["id"])
    return task["id"], ""


def cancel(task_id: int) -> tuple[bool, str]:
    """Cancel a task. Queued: cancelled in the record, never runs.
    Running: flagged — the worker stops between model turns and
    writes the cancelled state itself. Terminal/unknown: said so."""
    task = get_task(task_id)
    if task is None:
        return False, f"no background task #{task_id}"
    status = task.get("status")
    if status in TERMINAL:
        return False, f"task #{task_id} is already {status}"
    flag = _cancel_flags.get(int(task_id))
    if flag is not None:
        flag.set()
    if status == "queued":
        _update(int(task_id), status="cancelled",
                result="Cancelled before it started.",
                finished=time.time())
        _announce(_update(int(task_id)))
        return True, f"task #{task_id} cancelled before it started"
    return True, (f"task #{task_id} will stop between steps — a "
                  f"call already in flight finishes first")


# ── The worker ─────────────────────────────────────────────────────────────

def _announce(task: dict | None) -> None:
    """One proactive line per terminal task, through the
    scheduler's gated seam. Never raises; an announcement that
    cannot happen costs nothing — the record is the truth."""
    if not task:
        return
    try:
        from core import scheduler as _scheduler
        first = (task.get("result") or "").splitlines()
        summary = first[0][:140] if first else ""
        line = (f"Background task #{task['id']} {task['status']}"
                + (f": {summary}" if summary else ""))
        _scheduler.speak_alert(line)
    except Exception:
        pass


def _finish(task_id: int, status: str, result: str) -> None:
    task = _update(task_id, status=status, result=result,
                   finished=time.time())
    _announce(task)


def _run_task(task_id: int) -> None:
    task = get_task(task_id)
    if task is None or task.get("status") != "queued":
        return   # cancelled while queued, or recovered as stale
    flag = _cancel_flags.get(task_id)
    if flag is not None and flag.is_set():
        _finish(task_id, "cancelled", "Cancelled before it started.")
        return
    _update(task_id, status="running", started=time.time())
    decls = []
    try:
        decls = list(_tool_decls()) if _tool_decls else []
    except Exception:
        decls = []
    messages = [{"role": "user", "text": task["prompt"]}]
    try:
        for _turn in range(MAX_TURNS):
            if flag is not None and flag.is_set():
                _finish(task_id, "cancelled",
                        "Cancelled while running — stopped between "
                        "steps, as promised.")
                return
            try:
                text, calls = _call_model(messages, decls)
            except Exception as e:
                _finish(task_id, "failed",
                        f"The model call failed: {e}")
                return
            calls = list(calls or [])
            if not calls:
                _finish(task_id, "done",
                        (text or "").strip()
                        or "(the task finished without an answer)")
                return
            messages.append({"role": "model", "text": text or "",
                             "calls": calls})
            results = []
            for call in calls:
                if flag is not None and flag.is_set():
                    _finish(task_id, "cancelled",
                            "Cancelled while running — stopped "
                            "between steps, as promised.")
                    return
                name = str(call.get("name") or "")
                args = call.get("args") or {}
                try:
                    result = _dispatch(name, args)
                except Exception as e:
                    result = f"Tool '{name}' failed: {e}"
                result = str(result)
                if "[CONFIRMATION_PENDING]" in result:
                    _finish(
                        task_id, "blocked",
                        f"Stopped at the confirmation gate: "
                        f"'{name}' is waiting for confirmation on "
                        f"the HUD. The task did not do it, and "
                        f"cannot see its outcome if it is confirmed "
                        f"— a fresh task can pick up from there.")
                    return
                results.append({"name": name, "result": result})
            messages.append({"role": "tool", "results": results})
        _finish(task_id, "failed",
                f"The task used its {MAX_TURNS} tool turns without "
                f"finishing, so it was stopped rather than left "
                f"wandering.")
    finally:
        _cancel_flags.pop(task_id, None)


# ── Rendering for the tools ────────────────────────────────────────────────

def _fmt_ts(epoch) -> str:
    if not epoch:
        return "—"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(float(epoch)))
    except Exception:
        return str(epoch)


def render_task(task: dict) -> str:
    lines = [f"Task #{task['id']} [{task['status']}] "
             f"created {_fmt_ts(task.get('created'))}"]
    lines.append(f"Prompt: {task.get('prompt', '')}")
    if task.get("started"):
        lines.append(f"Started: {_fmt_ts(task['started'])}")
    if task.get("finished"):
        lines.append(f"Finished: {_fmt_ts(task['finished'])}")
    if task.get("result"):
        lines.append(f"Result: {task['result']}")
    return "\n".join(lines)


def render_list(rows: list[dict]) -> str:
    if not rows:
        return "No background tasks yet."
    out = []
    for t in rows:
        prompt = (t.get("prompt") or "")[:60]
        out.append(f"#{t['id']} [{t['status']}] {prompt}")
    return "\n".join(out)


def reset_for_tests() -> None:
    """Clear the bindings and in-flight bookkeeping (the store file
    itself is the fixture's to point elsewhere)."""
    global _call_model, _dispatch, _tool_decls, _bound_once, _executor
    _call_model = _dispatch = _tool_decls = None
    _bound_once = False
    _cancel_flags.clear()
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None
