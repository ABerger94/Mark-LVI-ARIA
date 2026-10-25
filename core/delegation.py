"""
core/delegation.py — background tasks, run without the conversation.

WHAT IT IS
    delegate_task hands a prompt to a worker: the prompt runs
    through the app's model WITH the app's tools — the same
    dispatch a spoken call uses — on worker threads, while the
    conversation goes on without it. The record lives in
    memory/delegated_tasks.json (atomic writes, the stores' own
    idiom). task_status reads the record back; a status is
    never guessed, summarised or reconstructed from anything else.

PHASE 2 — THE LONG-HORIZON ENGINE (2026-10-24, Alek's picks F1–F4)
    Phase H1's worker was a sprint: one prompt blob, twelve model
    turns, the first model failure terminal, an app restart fatal,
    and "done" on the model's own say-so. This engine replaces the
    sprint with a persisted job, in four moves:

      * PLAN FIRST. The first worker turn produces a structured
        plan — at most MAX_PLAN_STEPS steps, each {title,
        done_when} — validated strictly (validate_plan: schema,
        per-field caps, whole-plan cap; one violation rejects the
        whole plan, consolidation's changeset rule). One re-ask on
        a bad plan, then the task fails honestly. The plan is part
        of the record and renders in task_status and the Fleet tab.
      * STEP BY STEP, CHECKPOINTED. Steps run one at a time, each
        with its own bounded turn budget (STEP_TURNS). After every
        model turn and every step completion the checkpoint is
        persisted atomically: step states with bounded summaries,
        the CURRENT step's message history (bounded — see
        CHECKPOINT BOUNDS), the in-flight call marker, the draft
        result so far. Completed steps are never replayed, on any
        path — resume, restart, or repair.
      * BLOCKED IS A PLACE, NOT A FUNERAL. A task that cannot
        proceed parks with its checkpoint and a reason:
        blocked_model (the model ladder is down — F3),
        blocked_input (the worker says it needs Alek — a step
        reply starting "NEED_INPUT:"), blocked_unknown (the app
        died mid-tool-call; the call's outcome is unknowable and
        replaying it could double-act — F1), and the original
        blocked (the confirmation gate; unchanged, see THE GATE
        RULE — and NOT resumable, because the gate's outcome
        belongs to the banner, not the task). task_resume returns
        a parked task to its checkpoint; a blocked_unknown resume
        skips the unknown call, records the skip, and continues
        from the next model turn of that step.
      * VERIFY AT THE END (F2). When every step completes, a
        separate verifier turn judges the draft against the
        original prompt, the plan's done_when tests, and the
        per-step summaries — the evidence, not the worker's prose
        alone. Its verdict is validated ({verdict: pass|fail,
        gaps: [...]}). Pass = done. Fail = the gaps become a
        repair step and the task goes back to work, at most
        MAX_REPAIR_LOOPS times; after that — or if the verifier
        cannot be reached or its verdict cannot be read — the task
        ends done_unverified with the gaps/reason named. A result
        is NEVER upgraded to done without a passing verdict.

    F3 — MODEL FAILURE. A model call that raises (the one-shot
    ladder's "no model answered" among them) is retried up to
    MODEL_RETRIES times per turn. Exhausted retries during
    planning or execution park the task as blocked_model, not
    failed. On the next process bind, blocked_model tasks
    requeue from their checkpoints automatically — the ladder
    gets its next chance without Alek lifting a finger. (The
    verifier is the one exception, above: a dead verifier makes
    the result unverified, not the task immortal.)

    F1 — RESTART. Records found queued/running at bind are no
    longer closed out as failed. Between steps (no in-flight
    call recorded) a task requeues and resumes from its last
    checkpoint. Mid-call it parks blocked_unknown, above.

    A blocked_input honesty note: resuming re-runs the current
    step from its checkpoint with the context the worker already
    had. An answer Alek gave in conversation is NOT injected —
    the worker's world is its prompt and its checkpoint, the
    standing rule below — so task_resume for blocked_input is
    for when the need was satisfied out of band, and the record
    never pretends the answer was carried in.

CHECKPOINT BOUNDS (the record is a job file, not a transcript)
    * Each step keeps a summary of at most SUMMARY_CAP chars;
      the draft result at most DRAFT_CAP.
    * The current step's message history is persisted so a
      mid-step restart loses no tool result, but every text and
      tool result in it is truncated to HISTORY_TEXT_CAP chars
      (with a marker naming the cut), the history keeps at most
      HISTORY_MAX_MSGS entries (the step's opening instruction
      plus the newest exchanges), and a completed step's history
      is dropped — only its summary survives. A task record
      therefore stays tens of kilobytes however long the job.
    * Turn budgets: STEP_TURNS per step, MAX_TURNS model turns
      per task in total (planning, steps, verifier, repairs —
      retries count; a ladder being retried is still being paid
      for in turns). A step that exhausts its own budget fails
      the task honestly, naming the step: a step that cannot say
      it is done inside its budget has no honest continuation,
      and parking it would invite the same loop on every resume.

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
    honestly rather than queueing work nobody can run. The same
    honesty applies to the off-switch: task_engine_enabled=false
    in the config makes delegate_task refuse with the reason —
    a disabled engine that silently queued work would be lying
    about two things at once.

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
itself (no recursion), task_resume or task_cancel (a worker does not run the lifecycle it lives inside), shutdown_jarvis and restart_program (a
background task neither ends nor relaunches the app), run_routine
(a trusted routine's pre-answered
gate is the user's answer for a replay THEY set going, not for a
task the model started), or the self-edit tools (their restart
belongs to the conversation's lifecycle, not a worker's), or
run_shell_command (a shell is arbitrary code: every command
needs a live human at the banner, in the moment — a background
task is neither).

CONCURRENCY + CANCELLATION
    Two workers, at most MAX_ACTIVE (4) tasks in flight (running +
    queued): delegate_task beyond that is refused outright, by a
    count read from the store. Blocked tasks are not in flight —
    they hold no worker — and do not count; a resumed task counts
    again the moment it requeues. A silent unbounded backlog is
    how background systems lie; a refusal is a fact the user can
    act on. Queued tasks start in order as workers free.

    Cancellation is cooperative, and says so: a queued task is
    cancelled in the record and never runs; a running task is
    flagged and stops BETWEEN model turns — a model call already
    in flight finishes, because there is no honest way to un-send
    it, and the record says "cancelled" only once it has stopped.
    A blocked task can be cancelled outright: nothing is running
    to protect.

THE STORE'S HONESTY RULES
    * The newest MAX_RECORDS (50) tasks are kept; a task in flight
      or blocked is never pruned out from under its worker.
    * A task's status is only ever what a worker, a recovery
      pass, or Alek (cancel/resume) wrote. task_status and the
      Fleet tab read the same record, so her words and the
      screen cannot diverge.

COMPLETION
    Reaching a terminal state (done / done_unverified / failed /
    cancelled) — or parking as blocked — announces once through
    the scheduler's proactive speech seam — speak_alert, so the
    awake gate and the proactive_alerts flag apply, as they do to
    every other thing she says unbidden. Muted, asleep or unbound,
    the announcement simply does not happen; the record remains
    the truth, and task_status always knows.
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
MAX_TURNS = 48        # total model turns per task (plan+steps+verify)
STEP_TURNS = 6        # model turns per step
MAX_PLAN_STEPS = 8
MAX_REPAIR_LOOPS = 2
MODEL_RETRIES = 3     # retries after the first failed model attempt

TITLE_CAP = 120
DONE_WHEN_CAP = 240
PLAN_JSON_CAP = 8000
SUMMARY_CAP = 1500
DRAFT_CAP = 8000
HISTORY_TEXT_CAP = 2000
HISTORY_MAX_MSGS = 40
GAP_CAP = 240
MAX_GAPS = 8

TERMINAL = ("done", "done_unverified", "failed", "cancelled")
BLOCKED_STATUSES = ("blocked", "blocked_model", "blocked_input",
                    "blocked_unknown")
RESUMABLE = ("blocked_model", "blocked_input", "blocked_unknown")

_retry_delay_s = 0.5  # base pause between model retries; tests set 0


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


def _checkpoint(task_id: int, **fields) -> dict | None:
    """A state write that also stamps when the checkpoint landed."""
    fields["checkpoint_at"] = time.time()
    return _update(task_id, **fields)


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


def _settings() -> dict:
    """The engine's config (memory/config_manager.py), read lazily
    so a broken config can never take delegation's import down
    with it. A read failure means the defaults — enabled."""
    try:
        from memory.config_manager import get_task_engine_settings
        settings = get_task_engine_settings()
        return settings if isinstance(settings, dict) else {}
    except Exception:
        return {}


def _engine_enabled() -> bool:
    return bool(_settings().get("task_engine_enabled", True))


# ── Plan + verdict validation (consolidation's changeset rule) ─────────────

def _json_from_text(text: str):
    """The JSON object in a model reply: fences stripped, the
    outermost braces taken. None when there is no object to read."""
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start:end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def validate_plan(text: str):
    """(plan, "") on success, (None, the violation) on failure.

    Strict, like a consolidation changeset: the reply must be one
    JSON object {"steps": [...]} and nothing else is negotiated —
    1..MAX_PLAN_STEPS steps, each exactly {title, done_when},
    both non-empty strings inside their caps, the whole plan
    inside PLAN_JSON_CAP. One violation rejects the whole plan:
    a half-valid plan executed anyway is how a worker ends up
    improvising the missing half.
    """
    if len(text or "") > PLAN_JSON_CAP * 2:
        return None, "the plan reply was far too large"
    data = _json_from_text(text)
    if data is None:
        return None, "the reply was not a JSON plan object"
    if set(data) != {"steps"}:
        return None, "the plan must have exactly one key: 'steps'"
    steps = data["steps"]
    if not isinstance(steps, list) or not steps:
        return None, "'steps' must be a non-empty list"
    if len(steps) > MAX_PLAN_STEPS:
        return None, f"too many steps ({len(steps)} > {MAX_PLAN_STEPS})"
    plan = []
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or set(step) != {"title",
                                                        "done_when"}:
            return None, (f"step {i + 1} must have exactly 'title' "
                          f"and 'done_when'")
        title = step["title"]
        done_when = step["done_when"]
        if not isinstance(title, str) or not title.strip():
            return None, f"step {i + 1} has no title"
        if not isinstance(done_when, str) or not done_when.strip():
            return None, f"step {i + 1} has no done_when test"
        if len(title) > TITLE_CAP:
            return None, f"step {i + 1} title exceeds {TITLE_CAP} chars"
        if len(done_when) > DONE_WHEN_CAP:
            return None, (f"step {i + 1} done_when exceeds "
                          f"{DONE_WHEN_CAP} chars")
        plan.append({"title": title.strip(),
                     "done_when": done_when.strip()})
    if len(json.dumps(plan)) > PLAN_JSON_CAP:
        return None, "the plan is too large overall"
    return plan, ""


def validate_verdict(text: str):
    """(verdict, gaps, "") or (None, [], the violation).

    The verifier's reply must be one JSON object
    {"verdict": "pass"|"fail", "gaps": [str, ...]} — gaps capped
    in count and length, because the gaps become a repair step
    and an unbounded gap list is an unbounded repair job. A fail
    with no named gap gains a generic one: a repair step needs
    something to aim at, and the record should say the verifier
    named nothing rather than invent specifics for it.
    """
    data = _json_from_text(text)
    if data is None:
        return None, [], "the verdict reply was not a JSON object"
    if set(data) - {"verdict", "gaps"}:
        return None, [], "the verdict carried unexpected keys"
    verdict = data.get("verdict")
    if verdict not in ("pass", "fail"):
        return None, [], "the verdict was neither 'pass' nor 'fail'"
    gaps = data.get("gaps", [])
    if not isinstance(gaps, list) or \
            any(not isinstance(g, str) for g in gaps):
        return None, [], "'gaps' must be a list of strings"
    gaps = [g.strip()[:GAP_CAP] for g in gaps if g.strip()][:MAX_GAPS]
    if verdict == "fail" and not gaps:
        gaps = ["the verifier failed the result without naming a gap"]
    return verdict, gaps, ""


# ── Checkpoint history bounds ──────────────────────────────────────────────

def _truncate(text, cap: int = HISTORY_TEXT_CAP) -> str:
    text = str(text or "")
    if len(text) <= cap:
        return text
    return text[:cap] + f" …[truncated {len(text) - cap} chars]"


def _trim_history(messages: list) -> list:
    """The persisted form of a step's message history: every text
    and tool result truncated to HISTORY_TEXT_CAP, and at most
    HISTORY_MAX_MSGS entries — the opening instruction plus the
    newest exchanges (a dropped middle is older tool output the
    step summaries and later turns have already absorbed)."""
    out = []
    for m in messages or []:
        role = m.get("role")
        if role == "tool":
            out.append({"role": "tool", "results": [
                {"name": r.get("name", ""),
                 "result": _truncate(r.get("result", ""))}
                for r in m.get("results") or []]})
        elif role == "model":
            out.append({"role": "model",
                        "text": _truncate(m.get("text", "")),
                        "calls": list(m.get("calls") or [])})
        else:
            out.append({"role": "user",
                        "text": _truncate(m.get("text", ""))})
    if len(out) > HISTORY_MAX_MSGS:
        out = out[:1] + out[-(HISTORY_MAX_MSGS - 1):]
    return out


# ── Binding (main.py, once per process) ────────────────────────────────────

def bind(call_model=None, dispatch=None, tool_declarations=None) -> None:
    """Wire the engine seam. The first bind in a process also runs
    the recovery pass over the previous process's records (see the
    header's F1/F3 rules)."""
    global _call_model, _dispatch, _tool_decls, _bound_once
    _call_model = call_model
    _dispatch = dispatch
    _tool_decls = tool_declarations
    if not _bound_once:
        _bound_once = True
        _recover_stale()


def _queue_for_run(task_id: int) -> None:
    """Put a task on the executor with a fresh cancel flag."""
    _cancel_flags[int(task_id)] = threading.Event()
    _ensure_executor().submit(_run_task, int(task_id))


def _recover_stale() -> None:
    """The F1/F3 recovery pass, once per process at first bind.

    queued/running with no in-flight call -> requeue and resume
    from the checkpoint. queued/running WITH an in-flight call ->
    blocked_unknown (the call's outcome is unknowable; it is
    never replayed). blocked_model -> requeue (F3's auto-retry on
    next launch). Everything else is a record, left exactly as it
    was written.
    """
    to_run = []
    with _lock:
        data = _load()
        changed = False
        for t in data["tasks"]:
            status = t.get("status")
            if status == "blocked_model":
                t["status"] = "queued"
                t["blocked_reason"] = ""
                changed = True
                to_run.append(t["id"])
            elif status in ("queued", "running"):
                call = t.get("inflight_call")
                if call:
                    t["status"] = "blocked_unknown"
                    t["blocked_reason"] = (
                        f"the app restarted while the tool call "
                        f"'{call}' was in flight — its outcome is "
                        f"unknown and it will not be replayed. "
                        f"Resume with task_resume to continue from "
                        f"the next step of the checkpoint.")
                    t["finished"] = None
                    changed = True
                else:
                    if status != "queued":
                        t["status"] = "queued"
                        changed = True
                    to_run.append(t["id"])
        if changed:
            _prune(data)
            _save(data)
    for task_id in to_run:
        _queue_for_run(task_id)


def _ensure_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(max_workers=MAX_WORKERS,
                                       thread_name_prefix="delegate")
    return _executor


# ── Delegation ─────────────────────────────────────────────────────────────

def _new_record(prompt: str) -> dict:
    return {"id": 0, "prompt": prompt, "status": "queued",
            "result": "", "created": time.time(), "started": None,
            "finished": None,
            # Phase 2 engine state (the checkpoint).
            "phase": "plan", "plan": None, "steps_state": [],
            "current_step": 0, "checkpoint_at": None,
            "inflight_call": None, "blocked_reason": "",
            "draft_result": "", "verification": None,
            "repair_loops": 0, "turns_used": 0, "skipped_calls": []}


def delegate(prompt: str) -> tuple[int | None, str]:
    """Queue a background task. (task_id, "") on success, or
    (None, the honest refusal) — the engine switched off, unbound,
    an empty prompt, or the in-flight bound reached."""
    prompt = (prompt or "").strip()
    if not prompt:
        return None, "nothing to delegate — the prompt was empty"
    if not _engine_enabled():
        return None, ("the task engine is switched off "
                      "(task_engine_enabled is false) — background "
                      "tasks are disabled")
    if _call_model is None or _dispatch is None:
        return None, ("background tasks are not available until "
                      "the session machinery is running")
    with _lock:
        if _active_count() >= MAX_ACTIVE:
            return None, (f"{MAX_ACTIVE} background tasks are "
                          f"already in flight — let one finish "
                          f"first")
        data = _load()
        task = _new_record(prompt)
        task["id"] = data["next_id"]
        data["next_id"] += 1
        data["tasks"].append(task)
        _prune(data)
        _save(data)
        task_id = task["id"]
    _queue_for_run(task_id)
    return task_id, ""


def resume(task_id: int) -> tuple[bool, str]:
    """Return a blocked task to its checkpoint (task_resume).

    Resumable: blocked_model, blocked_input, blocked_unknown.
    A confirmation-gate block is NOT resumable — the gate's
    outcome belongs to the banner, and the original rule stands:
    a fresh task picks up from there. A blocked_unknown resume
    skips the in-flight call whose outcome is unknowable (the
    record names it in skipped_calls, and the step's pending
    model turn that asked for it is dropped so the worker
    re-decides the step from its completed results) — the call
    itself is never replayed.
    """
    task = get_task(task_id)
    if task is None:
        return False, f"no background task #{task_id}"
    status = task.get("status")
    if status in TERMINAL:
        return False, f"task #{task_id} is already {status}"
    if status in ("queued", "running"):
        return False, f"task #{task_id} is already {status}"
    if status == "blocked":
        return False, ("task #{} is blocked at the confirmation "
                       "gate — its outcome belongs to the banner "
                       "and cannot be resumed; start a fresh task "
                       "to pick up from there").format(task_id)
    if status not in RESUMABLE:
        return False, f"task #{task_id} is {status} and cannot resume"
    if _call_model is None or _dispatch is None:
        return False, ("background tasks are not available until "
                       "the session machinery is running")
    with _lock:
        if _active_count() >= MAX_ACTIVE:
            return False, (f"{MAX_ACTIVE} background tasks are "
                           f"already in flight — let one finish "
                           f"first")
    fields: dict = {"status": "queued", "blocked_reason": "",
                    "finished": None}
    if status == "blocked_unknown":
        skipped = task.get("inflight_call")
        if skipped:
            calls = list(task.get("skipped_calls") or [])
            calls.append(skipped)
            fields["skipped_calls"] = calls
        fields["inflight_call"] = None
        states = [dict(s) for s in task.get("steps_state") or []]
        idx = int(task.get("current_step") or 0)
        if 0 <= idx < len(states):
            history = list(states[idx].get("history") or [])
            while history and history[-1].get("role") == "model":
                history.pop()
            states[idx]["history"] = history
            fields["steps_state"] = states
    _checkpoint(int(task_id), **fields)
    _queue_for_run(int(task_id))
    where = _progress_text(task) or "its checkpoint"
    return True, (f"task #{task_id} resumed from {where}"
                  + ("; the unknown call "
                     f"'{task.get('inflight_call')}' was skipped, "
                     "not replayed"
                     if status == "blocked_unknown"
                     and task.get("inflight_call") else ""))


def cancel(task_id: int) -> tuple[bool, str]:
    """Cancel a task. Queued: cancelled in the record, never runs.
    Running: flagged — the worker stops between model turns and
    writes the cancelled state itself. Blocked: cancelled outright,
    nothing is running to protect. Terminal/unknown: said so."""
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
        _announce(get_task(int(task_id)))
        return True, f"task #{task_id} cancelled before it started"
    if status in BLOCKED_STATUSES:
        _update(int(task_id), status="cancelled",
                result=f"Cancelled while {status}.",
                finished=time.time())
        _announce(get_task(int(task_id)))
        return True, f"task #{task_id} cancelled while {status}"
    return True, (f"task #{task_id} will stop between steps — a "
                  f"call already in flight finishes first")


# ── The worker ─────────────────────────────────────────────────────────────

class _ModelDown(Exception):
    """Every retry of a model turn failed; carries the last error."""


class _TurnBudget(Exception):
    """The task's total model-turn budget is spent."""


class _Cancelled(Exception):
    """The cancel flag landed between turns."""


def _announce(task: dict | None) -> None:
    """One proactive line per terminal or parked task, through the
    scheduler's gated seam. Never raises; an announcement that
    cannot happen costs nothing — the record is the truth."""
    if not task:
        return
    try:
        from core import scheduler as _scheduler
        first = (task.get("result") or "").splitlines()
        summary = first[0][:140] if first else \
            (task.get("blocked_reason") or "")[:140]
        line = (f"Background task #{task['id']} {task['status']}"
                + (f": {summary}" if summary else ""))
        _scheduler.speak_alert(line)
    except Exception:
        pass


def _finish(task_id: int, status: str, result: str,
            **fields) -> None:
    fields.setdefault("phase", "done")
    fields.setdefault("inflight_call", None)
    task = _update(task_id, status=status, result=result,
                   finished=time.time(), **fields)
    _announce(task)


def _park(task_id: int, status: str, reason: str, **fields) -> None:
    """Block a task with its checkpoint intact, and say so once."""
    task = _checkpoint(task_id, status=status,
                       blocked_reason=reason, finished=None, **fields)
    _announce(task)


class _Engine:
    """One run of one task: the model-turn seam with F3 retries,
    turn accounting, and cancellation, shared by every phase."""

    def __init__(self, task_id: int, decls: list, flag):
        self.task_id = int(task_id)
        self.decls = decls
        self.flag = flag

    def check_cancel(self) -> None:
        if self.flag is not None and self.flag.is_set():
            raise _Cancelled()

    def call_model(self, messages):
        """One model turn, retried per F3. Raises _ModelDown when
        every attempt failed, _TurnBudget when the task's total
        turn budget is spent, _Cancelled on a cancel."""
        last: Exception | None = None
        for attempt in range(MODEL_RETRIES + 1):
            self.check_cancel()
            task = get_task(self.task_id)
            used = int((task or {}).get("turns_used") or 0) + 1
            if used > MAX_TURNS:
                raise _TurnBudget()
            _update(self.task_id, turns_used=used)
            try:
                text, calls = _call_model(messages, self.decls)
                return (text or ""), list(calls or [])
            except Exception as e:               # noqa: BLE001
                last = e
                if attempt < MODEL_RETRIES and _retry_delay_s > 0:
                    time.sleep(min(_retry_delay_s * (attempt + 1),
                                   2.0))
        raise _ModelDown(str(last) if last else "no model answered")


_PLAN_INSTRUCTION = (
    "Plan this background task. Reply with ONLY a JSON object, "
    "no prose, no code fences:\n"
    '{"steps": [{"title": "what this step does", '
    '"done_when": "the test that proves this step is done"}]}\n'
    "At most %d steps. Each step must be a concrete piece of "
    "work one worker can finish; the done_when is how a separate "
    "verifier will later judge it. The task:\n\n%s")

_VERIFY_INSTRUCTION = (
    "You are the verifier for a finished background task. Judge "
    "the draft result against the task and the plan's done_when "
    "tests, using the per-step evidence. Reply with ONLY a JSON "
    "object, no prose, no code fences:\n"
    '{"verdict": "pass", "gaps": []} or '
    '{"verdict": "fail", "gaps": ["each unmet requirement"]}\n\n'
    "TASK:\n%s\n\nPLAN AND EVIDENCE:\n%s\n\nDRAFT RESULT:\n%s")


def _phase_plan(engine: _Engine) -> str:
    """Plan first: one model turn (plus a single re-ask on a bad
    plan), strictly validated, persisted before any step runs."""
    task = get_task(engine.task_id)
    messages = [{"role": "user",
                 "text": _PLAN_INSTRUCTION
                         % (MAX_PLAN_STEPS, task["prompt"])}]
    complaint = ""
    for _attempt in range(2):
        try:
            text, _calls = engine.call_model(messages)
        except _ModelDown as e:
            _park(engine.task_id, "blocked_model",
                  f"the model could not be reached while planning "
                  f"({e}) — the task will retry on the next launch, "
                  f"or resume it with task_resume")
            return "stop"
        except _TurnBudget:
            _finish(engine.task_id, "failed",
                    f"The task used its {MAX_TURNS} model turns "
                    f"during planning, so it was stopped.")
            return "stop"
        plan, complaint = validate_plan(text)
        if plan:
            states = [{"status": "pending", "summary": "",
                       "turns": 0, "history": []} for _ in plan]
            _checkpoint(engine.task_id, plan=plan,
                        steps_state=states, current_step=0,
                        phase="execute")
            return "continue"
        messages = messages + [
            {"role": "model", "text": text or ""},
            {"role": "user",
             "text": f"That plan was invalid: {complaint}. Reply "
                     f"with ONLY the JSON plan, exactly in the "
                     f"shape asked for."}]
    _finish(engine.task_id, "failed",
            f"The model could not produce a valid plan "
            f"({complaint}), so the task was not started.")
    return "stop"


def _step_opening(task: dict, plan: list, states: list,
                  idx: int) -> dict:
    """A step's first message: the task, this step and its test,
    and the summaries of the steps already done — the worker's
    whole world, per the standing context rule."""
    prior = [f"Step {i + 1} ({plan[i]['title']}): "
             f"{states[i].get('summary', '')}"
             for i in range(min(idx, len(states)))
             if states[i].get("status") == "done"]
    step = plan[idx]
    text = (f"Background task: {task['prompt']}\n\n"
            f"Plan step {idx + 1} of {len(plan)}: {step['title']}\n"
            f"This step is done when: {step['done_when']}\n\n"
            + ("Earlier steps completed:\n" + "\n".join(prior)
               + "\n\n" if prior else "")
            + "Work this step with the tools you have. When the "
              "step is done, reply with a summary of what you "
              "found or did and NO tool calls. If you cannot "
              "proceed without the user, reply with one line "
              "starting 'NEED_INPUT:' saying what you need, and "
              "no tool calls.")
    return {"role": "user", "text": text}


def _phase_execute(engine: _Engine) -> str:
    """Run the plan's steps in order, checkpointing as we go.
    Steps already done are skipped — never replayed."""
    while True:
        task = get_task(engine.task_id)
        plan = task.get("plan") or []
        states = [dict(s) for s in task.get("steps_state") or []]
        while len(states) < len(plan):
            states.append({"status": "pending", "summary": "",
                           "turns": 0, "history": []})
        idx = int(task.get("current_step") or 0)
        while idx < len(plan) and \
                states[idx].get("status") == "done":
            idx += 1
        if idx >= len(plan):
            _checkpoint(engine.task_id, steps_state=states,
                        current_step=idx, phase="verify")
            return "continue"
        if _run_step(engine, task, plan, states, idx) == "stop":
            return "stop"
        # step done: loop and take the next one


def _run_step(engine: _Engine, task: dict, plan: list,
              states: list, idx: int) -> str:
    """One step, at most STEP_TURNS model turns. Returns "next"
    when the step completed, "stop" when the task parked or
    finished inside the step."""
    tid = engine.task_id
    state = states[idx]
    history = list(state.get("history") or []) or \
        [_step_opening(task, plan, states, idx)]
    turns = int(state.get("turns") or 0)
    state["status"] = "running"

    def persist(inflight=None):
        state["history"] = _trim_history(history)
        state["turns"] = turns
        fields = {"steps_state": states, "current_step": idx}
        if inflight is not None:
            fields["inflight_call"] = inflight
        _checkpoint(tid, **fields)

    persist()
    while turns < STEP_TURNS:
        engine.check_cancel()
        try:
            text, calls = engine.call_model(history)
        except _ModelDown as e:
            persist()
            _park(tid, "blocked_model",
                  f"the model could not be reached during step "
                  f"{idx + 1} ('{plan[idx]['title']}') ({e}) — "
                  f"the checkpoint is saved; the task will retry "
                  f"on the next launch, or resume it with "
                  f"task_resume")
            return "stop"
        except _TurnBudget:
            _finish(tid, "failed",
                    f"The task used its {MAX_TURNS} model turns "
                    f"without finishing, so it was stopped.")
            return "stop"
        turns += 1
        if not calls:
            answer = (text or "").strip()
            if answer.startswith("NEED_INPUT:") or \
                    answer == "NEED_INPUT":
                need = answer[len("NEED_INPUT:"):].strip() or \
                    "the worker needs the user"
                persist()
                _park(tid, "blocked_input",
                      f"step {idx + 1} ('{plan[idx]['title']}') "
                      f"needs the user: {need}")
                return "stop"
            state["status"] = "done"
            state["summary"] = (answer[:SUMMARY_CAP]
                                or "(the step finished without "
                                   "a summary)")
            state["history"] = []
            state["turns"] = turns
            _checkpoint(tid, steps_state=states,
                        current_step=idx + 1,
                        draft_result=answer[:DRAFT_CAP])
            return "next"
        history.append({"role": "model", "text": text or "",
                        "calls": calls})
        results = []
        for call in calls:
            engine.check_cancel()
            name = str(call.get("name") or "")
            args = call.get("args") or {}
            persist(inflight=name)      # F1: the call is in flight
            try:
                result = _dispatch(name, args)
            except Exception as e:        # noqa: BLE001
                result = f"Tool '{name}' failed: {e}"
            result = str(result)
            _update(tid, inflight_call=None)
            if "[CONFIRMATION_PENDING]" in result:
                history.append({"role": "tool", "results": [
                    {"name": name, "result": result}]})
                state["history"] = _trim_history(history)
                state["turns"] = turns
                gate_text = (
                    f"Stopped at the confirmation gate: '{name}' "
                    f"is waiting for confirmation on the HUD. "
                    f"The task did not do it, and cannot see its "
                    f"outcome if it is confirmed — a fresh task "
                    f"can pick up from there.")
                _park(tid, "blocked", gate_text,
                      steps_state=states, result=gate_text)
                return "stop"
            results.append({"name": name, "result": result})
        history.append({"role": "tool", "results": results})
        persist()
    _finish(tid, "failed",
            f"Step {idx + 1} ('{plan[idx]['title']}') used its "
            f"{STEP_TURNS} model turns without finishing, so the "
            f"task was stopped rather than left wandering.")
    return "stop"


def _phase_verify(engine: _Engine) -> str:
    """The verifier's turn (F2): evidence in, validated verdict
    out — pass finishes done, fail repairs or ends unverified."""
    tid = engine.task_id
    task = get_task(tid)
    plan = task.get("plan") or []
    states = task.get("steps_state") or []
    draft = (task.get("draft_result") or "").strip()
    if not draft:
        draft = "\n".join(
            str(s.get("summary") or "") for s in states
            if s.get("status") == "done").strip() \
            or "(the task finished without an answer)"
    evidence = "\n".join(
        f"Step {i + 1}: {step['title']} — done when: "
        f"{step['done_when']} — evidence: "
        f"{(states[i].get('summary') if i < len(states) else '') or '(none)'}"
        for i, step in enumerate(plan))
    messages = [{"role": "user",
                 "text": _VERIFY_INSTRUCTION
                         % (task["prompt"], evidence, draft)}]
    try:
        text, _calls = engine.call_model(messages)
    except _ModelDown as e:
        _finish(tid, "done_unverified",
                draft + f"\n\n(Unverified: the verifier could not "
                        f"be reached — the model did not answer "
                        f"({e}). This result has not been checked.)",
                verification={"status": "unavailable", "gaps": [],
                              "loops": int(task.get("repair_loops")
                                           or 0),
                              "at": time.time()},
                draft_result=draft)
        return "stop"
    except _TurnBudget:
        _finish(tid, "failed",
                f"The task used its {MAX_TURNS} model turns "
                f"without finishing, so it was stopped.")
        return "stop"
    verdict, gaps, complaint = validate_verdict(text)
    loops = int(task.get("repair_loops") or 0)
    if verdict is None:
        _finish(tid, "done_unverified",
                draft + f"\n\n(Unverified: the verifier's verdict "
                        f"could not be read — {complaint}. This "
                        f"result has not been checked.)",
                verification={"status": "invalid", "gaps": [],
                              "loops": loops, "at": time.time()},
                draft_result=draft)
        return "stop"
    if verdict == "pass":
        _finish(tid, "done", draft,
                verification={"status": "passed", "gaps": gaps,
                              "loops": loops, "at": time.time()},
                draft_result=draft)
        return "stop"
    # verdict == "fail"
    if loops >= MAX_REPAIR_LOOPS:
        _finish(tid, "done_unverified",
                draft + f"\n\n(Unverified: verification failed "
                        f"after {loops} repair loop(s). Gaps: "
                        + "; ".join(gaps) + ")",
                verification={"status": "failed", "gaps": gaps,
                              "loops": loops, "at": time.time()},
                draft_result=draft)
        return "stop"
    repair = {"title": "Repair: address verification gaps",
              "done_when": "; ".join(gaps)[:DONE_WHEN_CAP]}
    plan = plan + [repair]
    states = states + [{"status": "pending", "summary": "",
                        "turns": 0, "history": []}]
    _checkpoint(tid, plan=plan, steps_state=states,
                current_step=len(plan) - 1, phase="execute",
                repair_loops=loops + 1,
                verification={"status": "failed", "gaps": gaps,
                              "loops": loops + 1, "at": time.time()})
    return "continue"


def _run_task(task_id: int) -> None:
    task = get_task(task_id)
    if task is None or task.get("status") != "queued":
        return   # cancelled while queued, or already handled
    flag = _cancel_flags.get(task_id)
    if flag is not None and flag.is_set():
        _finish(task_id, "cancelled", "Cancelled before it started.")
        return
    _update(task_id, status="running",
            started=task.get("started") or time.time())
    decls = []
    try:
        decls = list(_tool_decls()) if _tool_decls else []
    except Exception:
        decls = []
    engine = _Engine(task_id, decls, flag)
    try:
        while True:
            engine.check_cancel()
            task = get_task(task_id)
            phase = (task or {}).get("phase") or "plan"
            if phase == "plan":
                outcome = _phase_plan(engine)
            elif phase == "execute":
                outcome = _phase_execute(engine)
            elif phase == "verify":
                outcome = _phase_verify(engine)
            else:
                return
            if outcome != "continue":
                return
    except _Cancelled:
        _finish(task_id, "cancelled",
                "Cancelled while running — stopped between "
                "steps, as promised.")
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


def _progress_text(task: dict) -> str:
    """'step 2 of 4: <title>' from the record — or 'all N steps
    complete', or "" for a legacy record with no plan."""
    plan = task.get("plan") or []
    if not plan:
        return ""
    states = task.get("steps_state") or []
    done = sum(1 for s in states if s.get("status") == "done")
    if done >= len(plan):
        return f"all {len(plan)} steps complete"
    idx = int(task.get("current_step") or 0)
    idx = min(max(idx, 0), len(plan) - 1)
    return f"step {idx + 1} of {len(plan)}: {plan[idx]['title']}"


def render_task(task: dict) -> str:
    lines = [f"Task #{task['id']} [{task['status']}] "
             f"created {_fmt_ts(task.get('created'))}"]
    lines.append(f"Prompt: {task.get('prompt', '')}")
    if task.get("started"):
        lines.append(f"Started: {_fmt_ts(task['started'])}")
    if task.get("finished"):
        lines.append(f"Finished: {_fmt_ts(task['finished'])}")
    plan = task.get("plan") or []
    if plan:
        lines.append(f"Plan ({_progress_text(task)}):")
        states = task.get("steps_state") or []
        for i, step in enumerate(plan):
            state = states[i] if i < len(states) else {}
            mark = state.get("status") or "pending"
            line = f"  {i + 1}. [{mark}] {step.get('title', '')}"
            summary = (state.get("summary") or "")[:160]
            if summary:
                line += f" — {summary}"
            lines.append(line)
    if task.get("checkpoint_at"):
        lines.append(f"Checkpoint: "
                     f"{_fmt_ts(task['checkpoint_at'])}")
    if task.get("status") in BLOCKED_STATUSES and \
            task.get("blocked_reason"):
        lines.append(f"Blocked: {task['blocked_reason']}")
    if task.get("skipped_calls"):
        lines.append("Skipped (outcome unknown, not replayed): "
                     + ", ".join(task["skipped_calls"]))
    verification = task.get("verification")
    if isinstance(verification, dict) and verification:
        vline = f"Verification: {verification.get('status', '')}"
        gaps = verification.get("gaps") or []
        if gaps:
            vline += " — gaps: " + "; ".join(gaps)
        lines.append(vline)
    if task.get("result"):
        lines.append(f"Result: {task['result']}")
    return "\n".join(lines)


def render_list(rows: list[dict]) -> str:
    if not rows:
        return "No background tasks yet."
    out = []
    for t in rows:
        prompt = (t.get("prompt") or "")[:60]
        line = f"#{t['id']} [{t['status']}] {prompt}"
        progress = _progress_text(t)
        if progress:
            line += f" ({progress})"
        if t.get("status") in BLOCKED_STATUSES and \
                t.get("blocked_reason"):
            line += f" — blocked: {t['blocked_reason'][:80]}"
        out.append(line)
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
