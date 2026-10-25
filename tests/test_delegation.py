"""Task delegation (core/delegation.py + the tools), Phase 2 engine.

What is being pinned shut:

  * a delegated task is planned first (a validated structured
    plan), runs its steps through the bound dispatch seam,
    checkpoints as it goes, and may only call itself DONE after
    a separate verifier pass — the final step's text lands in
    the record as the verified result;
  * the gate rule: a dispatch result carrying
    [CONFIRMATION_PENDING] stops the task as BLOCKED — the gated
    act is never executed by the task and never claimed done;
  * the concurrency bound: four in flight (two running, two
    queued) is the cap; the fifth delegation is refused, in words;
  * cancellation: a queued task is cancelled in the record and
    never runs; a running task stops between model turns;
  * a task that cannot finish inside its turn budget fails
    honestly instead of wandering;
  * a model failure is retried, then parks the task as
    blocked_model (F3) — it is NOT a terminal failure any more;
  * a record left in flight by a dead process is resumed from
    its checkpoint at bind (F1) — no longer closed out failed;
  * unbound — or the engine switched off — delegation refuses:
    it does not queue work nobody can run.
"""
import json
import threading
import time

import pytest

from actions import delegate_task as dt_action
from actions import task_cancel as tc_action
from actions import task_status as ts_action
from core import delegation as dlg
from core import incidents


# ── a phase-aware fake model ────────────────────────────────────────────

def phase_of(messages):
    first = messages[0]["text"] if messages else ""
    if first.startswith("Plan this background task"):
        return "plan"
    if first.startswith("You are the verifier"):
        return "verify"
    return "step"


def make_model(plan=None, verdict="pass", gaps=None, step_fn=None,
               plan_fn=None):
    """A scripted model that speaks the engine's three phases.

    plan: list of {title, done_when} (default one step).
    step_fn(messages, n): reply for the n-th step-phase call.
    plan_fn(messages, n): reply for the n-th planning call.
    """
    calls = {"plan": 0, "verify": 0, "step": 0}

    def model(messages, decls):
        phase = phase_of(messages)
        calls[phase] += 1
        if phase == "plan":
            if plan_fn is not None:
                return plan_fn(messages, calls["plan"])
            steps = plan or [{"title": "Only step",
                              "done_when": "it is done"}]
            return (json.dumps({"steps": steps}), [])
        if phase == "verify":
            return (json.dumps({"verdict": verdict,
                                 "gaps": gaps or []}), [])
        if step_fn is not None:
            return step_fn(messages, calls["step"])
        return ("Step complete.", [])

    model.calls = calls
    return model


@pytest.fixture()
def bound(tmp_path, monkeypatch):
    """Delegation bound to scripted seams, on a tmp store."""
    store = tmp_path / "delegated_tasks.json"
    monkeypatch.setattr(dlg, "STORE_PATH", store)
    monkeypatch.setattr(dlg, "_retry_delay_s", 0)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    dlg.reset_for_tests()
    seen = {"dispatches": [], "first_texts": []}

    def make(model):
        def call_model(messages, decls):
            seen["first_texts"].append(messages[0]["text"]
                                        if messages else "")
            return model(messages, decls)

        def dispatch(name, args):
            seen["dispatches"].append((name, args))
            if name == "gated_thing":
                return ("[CONFIRMATION_PENDING] I have put a "
                        "confirmation on screen for: gated_thing.")
            return f"result-of-{name}"

        dlg.bind(call_model=call_model, dispatch=dispatch,
                 tool_declarations=lambda: [{"name": "ping"}])
        return seen

    yield {"store": store, "seen": seen, "make": make}
    dlg.reset_for_tests()


def _wait_status(task_id, want, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        task = dlg.get_task(task_id)
        if task and task["status"] in want:
            return task
        time.sleep(0.02)
    raise AssertionError(
        f"task {task_id} never reached {want}: {dlg.get_task(task_id)}")


# ── the happy path ──────────────────────────────────────────────────────

def test_task_runs_tools_and_records_the_result(bound):
    plan = [{"title": "Gather", "done_when": "evidence collected"},
            {"title": "Write up", "done_when": "draft complete"}]

    def step_fn(messages, n):
        first = messages[0]["text"]
        if "Plan step 1 of" in first and n == 1:
            return ("", [{"name": "ping", "args": {"x": 1}}])
        if "Plan step 1 of" in first:
            return ("Gathered the evidence.", [])
        return ("All done: the answer is 42.", [])

    seen = bound["make"](make_model(plan=plan, step_fn=step_fn))
    task_id, refusal = dlg.delegate("check the thing")
    assert refusal == "" and task_id == 1
    task = _wait_status(task_id, {"done"})
    assert task["result"] == "All done: the answer is 42."
    assert task["verification"]["status"] == "passed"
    assert [s["status"] for s in task["steps_state"]] == ["done",
                                                          "done"]
    assert seen["dispatches"] == [("ping", {"x": 1})]
    assert task["started"] and task["finished"]


def test_task_status_action_reads_the_record(bound):
    bound["make"](make_model(
        step_fn=lambda messages, n: ("Finished quietly.", [])))
    out = dt_action.delegate_task({"prompt": "do a thing"})
    assert out.startswith("status=done") and "task=1" in out
    _wait_status(1, {"done"})
    out = ts_action.task_status({"task_id": 1})
    assert "status=done" in out and "Finished quietly." in out
    out = ts_action.task_status({"task_id": 99})
    assert out.startswith("status=failed")


def test_model_failure_parks_blocked_model_after_retries(bound,
                                                         monkeypatch):
    monkeypatch.setattr(dlg, "MODEL_RETRIES", 2)
    attempts = {"n": 0}

    def boom(messages, decls):
        attempts["n"] += 1
        raise RuntimeError("provider down")

    bound["make"](boom)
    task_id, _ = dlg.delegate("anything")
    task = _wait_status(task_id, {"blocked_model"})
    assert attempts["n"] == 3          # first try + 2 retries
    assert "provider down" in task["blocked_reason"]
    assert task["finished"] is None    # parked, not finished


# ── the gate rule ───────────────────────────────────────────────────────

def test_gated_call_blocks_the_task_and_executes_nothing(bound):
    model = make_model(step_fn=lambda messages, n:
                       ("", [{"name": "gated_thing", "args": {}}]))
    seen = bound["make"](model)
    task_id, _ = dlg.delegate("do the gated thing")
    task = _wait_status(task_id, {"blocked"})
    assert "confirmation gate" in task["result"]
    assert "gated_thing" in task["result"]
    # plan + exactly one step turn — the loop stopped at the gate
    assert model.calls == {"plan": 1, "verify": 0, "step": 1}
    assert seen["dispatches"] == [("gated_thing", {})]


# ── concurrency + cancellation ──────────────────────────────────────────

def _blocking_model(release):
    inner = make_model()

    def model(messages, decls):
        release.wait(timeout=10)
        return inner(messages, decls)
    return model


def test_concurrency_bound_refuses_the_fifth(bound):
    release = threading.Event()
    bound["make"](_blocking_model(release))
    ids = []
    for i in range(dlg.MAX_ACTIVE):
        task_id, refusal = dlg.delegate(f"job {i}")
        assert refusal == ""
        ids.append(task_id)
    task_id, refusal = dlg.delegate("one too many")
    assert task_id is None and "already in flight" in refusal
    release.set()
    for tid in ids:
        _wait_status(tid, {"done"})


def test_cancel_queued_task_never_runs(bound):
    release = threading.Event()
    seen = bound["make"](_blocking_model(release))
    first, _ = dlg.delegate("first")
    second, _ = dlg.delegate("second")
    queued, _ = dlg.delegate("queued job")
    _wait_status(first, {"running"})
    _wait_status(second, {"running"})
    ok, message = dlg.cancel(queued)
    assert ok and "before it started" in message
    assert dlg.get_task(queued)["status"] == "cancelled"
    release.set()
    _wait_status(first, {"done"})
    _wait_status(second, {"done"})
    time.sleep(0.3)   # a worker slot freed — the cancelled task
    assert not any("queued job" in t for t in seen["first_texts"])
    assert dlg.get_task(queued)["status"] == "cancelled"


def test_cancel_running_task_stops_between_turns(bound):
    gate = threading.Event()
    state = {"step": 0}

    def step_fn(messages, n):
        state["step"] += 1
        if state["step"] == 1:
            return ("", [{"name": "ping", "args": {}}])
        gate.wait(timeout=10)      # second turn waits to be released
        return ("", [{"name": "ping", "args": {}}])

    bound["make"](make_model(step_fn=step_fn))
    task_id, _ = dlg.delegate("long job")
    _wait_status(task_id, {"running"})
    ok, message = dlg.cancel(task_id)
    assert ok and "between steps" in message
    gate.set()                     # the in-flight turn now returns
    task = _wait_status(task_id, {"cancelled"})
    assert "between steps" in task["result"]


def test_cancel_terminal_or_unknown_is_reported(bound):
    bound["make"](make_model())
    task_id, _ = dlg.delegate("quick")
    _wait_status(task_id, {"done"})
    ok, message = dlg.cancel(task_id)
    assert not ok and "already done" in message
    ok, message = dlg.cancel(4242)
    assert not ok and "no background task" in message
    out = tc_action.task_cancel({"task_id": task_id})
    assert out.startswith("status=failed")


# ── caps, staleness, refusal ────────────────────────────────────────────

def test_turn_cap_stops_a_wandering_task(bound, monkeypatch):
    monkeypatch.setattr(dlg, "MAX_TURNS", 4)
    bound["make"](make_model(
        step_fn=lambda messages, n: ("", [{"name": "ping",
                                            "args": {}}])))
    task_id, _ = dlg.delegate("wander")
    task = _wait_status(task_id, {"failed"})
    assert "model turns" in task["result"]


def test_stale_records_resume_at_bind(bound):
    bound["store"].write_text(json.dumps({
        "next_id": 3,
        "tasks": [
            {"id": 1, "prompt": "old running", "status": "running",
             "result": "", "created": 1.0, "started": 2.0,
             "finished": None},
            {"id": 2, "prompt": "old done", "status": "done",
             "result": "kept", "created": 1.0, "started": 2.0,
             "finished": 3.0},
        ]}), encoding="utf-8")
    dlg.reset_for_tests()          # fresh process, nothing bound yet
    bound["make"](make_model())    # the bind resumes the stale task
    task = _wait_status(1, {"done"})
    assert task["prompt"] == "old running"
    assert dlg.get_task(2)["result"] == "kept"   # history untouched


def test_unbound_delegation_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(dlg, "STORE_PATH", tmp_path / "d.json")
    dlg.reset_for_tests()
    task_id, refusal = dlg.delegate("nobody home")
    assert task_id is None and "not available" in refusal
    out = dt_action.delegate_task({"prompt": "nobody home"})
    assert out.startswith("status=failed")


def test_engine_switched_off_refuses(bound, monkeypatch):
    bound["make"](make_model())
    monkeypatch.setattr(dlg, "_settings",
                        lambda: {"task_engine_enabled": False})
    task_id, refusal = dlg.delegate("while disabled")
    assert task_id is None and "switched off" in refusal


def test_empty_prompt_refuses(bound):
    bound["make"](make_model())
    task_id, refusal = dlg.delegate("   ")
    assert task_id is None and "empty" in refusal
