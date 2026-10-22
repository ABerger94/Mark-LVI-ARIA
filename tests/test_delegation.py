"""Phase H1 — task delegation (core/delegation.py + the three tools).

What is being pinned shut:

  * a delegated task runs its model loop on a worker: tool calls
    dispatch through the bound seam, the final text lands in the
    record as the result, and task_status reads exactly that;
  * the gate rule: a dispatch result carrying
    [CONFIRMATION_PENDING] stops the task as BLOCKED — the gated
    act is never executed by the task and never claimed done;
  * the concurrency bound: four in flight (two running, two
    queued) is the cap; the fifth delegation is refused, in words;
  * cancellation: a queued task is cancelled in the record and
    never runs; a running task stops between model turns;
  * a task that cannot finish inside its turn cap fails honestly
    instead of wandering;
  * records left in flight by a dead process are closed out as
    interrupted when the module is next bound;
  * unbound, delegation refuses — it does not queue work nobody
    can run.
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


@pytest.fixture()
def bound(tmp_path, monkeypatch):
    """Delegation bound to scripted seams, on a tmp store.

    The fake model is a per-test script: a list of (text, calls)
    replies consumed in order, or a callable(messages) -> reply.
    """
    store = tmp_path / "delegated_tasks.json"
    monkeypatch.setattr(dlg, "STORE_PATH", store)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    dlg.reset_for_tests()
    seen = {"dispatches": [], "prompts": []}

    def make(script):
        replies = script if callable(script) else list(script)

        def call_model(messages, decls):
            seen["prompts"].append(messages[0]["text"]
                                    if messages else "")
            if callable(replies):
                return replies(messages, decls)
            return replies.pop(0) if replies else ("(no script)",
                                                    [])

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
    seen = bound["make"]([
        ("", [{"name": "ping", "args": {"x": 1}}]),
        ("All done: the answer is 42.", []),
    ])
    task_id, refusal = dlg.delegate("check the thing")
    assert refusal == "" and task_id == 1
    task = _wait_status(task_id, {"done"})
    assert task["result"] == "All done: the answer is 42."
    assert seen["dispatches"] == [("ping", {"x": 1})]
    assert task["started"] and task["finished"]


def test_task_status_action_reads_the_record(bound):
    bound["make"]([("Finished quietly.", [])])
    out = dt_action.delegate_task({"prompt": "do a thing"})
    assert out.startswith("status=done") and "task=1" in out
    _wait_status(1, {"done"})
    out = ts_action.task_status({"task_id": 1})
    assert "status=done" in out and "Finished quietly." in out
    out = ts_action.task_status({"task_id": 99})
    assert out.startswith("status=failed")


def test_model_failure_fails_the_task_honestly(bound):
    def boom(messages, decls):
        raise RuntimeError("provider down")
    bound["make"](boom)
    task_id, _ = dlg.delegate("anything")
    task = _wait_status(task_id, {"failed"})
    assert "provider down" in task["result"]


# ── the gate rule ───────────────────────────────────────────────────────

def test_gated_call_blocks_the_task_and_executes_nothing(bound):
    seen = bound["make"]([
        ("", [{"name": "gated_thing", "args": {}}]),
        ("this reply must never be consumed", []),
    ])
    task_id, _ = dlg.delegate("do the gated thing")
    task = _wait_status(task_id, {"blocked"})
    assert "confirmation gate" in task["result"]
    assert "gated_thing" in task["result"]
    # the model got exactly one turn — the loop stopped at the gate
    assert len(seen["prompts"]) == 1


# ── concurrency + cancellation ──────────────────────────────────────────

def _blocking_model(release):
    def call_model(messages, decls):
        release.wait(timeout=10)
        return ("released", [])
    return call_model


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
    assert "queued job" not in seen["prompts"]   # still never ran
    assert dlg.get_task(queued)["status"] == "cancelled"


def test_cancel_running_task_stops_between_turns(bound):
    gate = threading.Event()
    calls = {"n": 0}

    def call_model(messages, decls):
        calls["n"] += 1
        if calls["n"] == 1:
            return ("", [{"name": "ping", "args": {}}])
        gate.wait(timeout=10)      # second turn waits to be released
        return ("", [{"name": "ping", "args": {}}])

    bound["make"](call_model)
    task_id, _ = dlg.delegate("long job")
    _wait_status(task_id, {"running"})
    ok, message = dlg.cancel(task_id)
    assert ok and "between steps" in message
    gate.set()                     # the in-flight turn now returns
    task = _wait_status(task_id, {"cancelled"})
    assert "between steps" in task["result"]


def test_cancel_terminal_or_unknown_is_reported(bound):
    bound["make"]([("done already", [])])
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
    monkeypatch.setattr(dlg, "MAX_TURNS", 3)
    bound["make"](lambda messages, decls: ("", [{"name": "ping",
                                           "args": {}}]))
    task_id, _ = dlg.delegate("wander")
    task = _wait_status(task_id, {"failed"})
    assert "tool turns" in task["result"]


def test_stale_records_close_out_at_bind(bound):
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
    bound["make"]([("ok", [])])    # the bind closes the stale record
    stale = dlg.get_task(1)
    assert stale["status"] == "failed"
    assert "Interrupted" in stale["result"]
    assert dlg.get_task(2)["result"] == "kept"   # history untouched


def test_unbound_delegation_refuses(tmp_path, monkeypatch):
    monkeypatch.setattr(dlg, "STORE_PATH", tmp_path / "d.json")
    dlg.reset_for_tests()
    task_id, refusal = dlg.delegate("nobody home")
    assert task_id is None and "not available" in refusal
    out = dt_action.delegate_task({"prompt": "nobody home"})
    assert out.startswith("status=failed")


def test_empty_prompt_refuses(bound):
    bound["make"]([])
    task_id, refusal = dlg.delegate("   ")
    assert task_id is None and "empty" in refusal
