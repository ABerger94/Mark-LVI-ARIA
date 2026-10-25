"""Phase 2 — the long-horizon task engine (core/delegation.py).

The pins, by approved fork:

  * plan validation is strict and total (bad shape, too many
    steps, oversize fields — one violation rejects the plan; one
    re-ask, then the task fails honestly);
  * checkpoints land after every step; a completed step is
    never replayed — not on resume, not on restart, not in a
    repair loop;
  * F1 restart: between steps a task auto-resumes from its
    checkpoint at bind; mid-tool-call it parks blocked_unknown
    naming the call, and resuming it skips that call for good;
  * F3 model failure: retries, then blocked_model (not failed),
    and blocked_model requeues by itself on the next bind;
  * NEED_INPUT parks a task as blocked_input;
  * F2 verification: pass = done; fail = a repair step and back
    to work (max 2 loops), then done_unverified with the gaps
    named; a dead or unreadable verifier = done_unverified,
    never done;
  * task_resume's paths and refusals, MAX_ACTIVE across resume,
    legacy-record rendering, and the checkpoint history bounds.
"""
import json
import threading
import time

import pytest

from actions import task_resume as tr_action
from core import delegation as dlg
from core import incidents
from tests.test_delegation import make_model, phase_of, _wait_status


@pytest.fixture()
def bound(tmp_path, monkeypatch):
    store = tmp_path / "delegated_tasks.json"
    monkeypatch.setattr(dlg, "STORE_PATH", store)
    monkeypatch.setattr(dlg, "_retry_delay_s", 0)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    dlg.reset_for_tests()
    seen = {"dispatches": []}

    def make(model):
        def dispatch(name, args):
            seen["dispatches"].append((name, args))
            return f"result-of-{name}"

        dlg.bind(call_model=model, dispatch=dispatch,
                 tool_declarations=lambda: [{"name": "ping"}])
        return seen

    yield {"store": store, "seen": seen, "make": make}
    dlg.reset_for_tests()


def _write_store(store, tasks, next_id=99):
    store.write_text(json.dumps({"next_id": next_id,
                                 "tasks": tasks}),
                     encoding="utf-8")


def _record(**fields):
    base = {"id": 1, "prompt": "legacy job", "status": "running",
            "result": "", "created": 1.0, "started": 2.0,
            "finished": None, "phase": "execute",
            "plan": [{"title": "First", "done_when": "one done"},
                     {"title": "Second", "done_when": "two done"}],
            "steps_state": [
                {"status": "done", "summary": "first evidence",
                 "turns": 2, "history": []},
                {"status": "pending", "summary": "", "turns": 0,
                 "history": []}],
            "current_step": 1, "checkpoint_at": 3.0,
            "inflight_call": None, "blocked_reason": "",
            "draft_result": "", "verification": None,
            "repair_loops": 0, "turns_used": 3,
            "skipped_calls": []}
    base.update(fields)
    return base


# ── plan + verdict validation ───────────────────────────────────────────

def test_validate_plan_accepts_a_good_plan():
    plan, err = dlg.validate_plan(json.dumps({"steps": [
        {"title": "Look", "done_when": "it is seen"},
        {"title": "Tell", "done_when": "it is said"}]}))
    assert err == "" and [s["title"] for s in plan] == ["Look",
                                                        "Tell"]


@pytest.mark.parametrize("bad", [
    "not json at all",
    json.dumps({"steps": []}),
    json.dumps({"plan": [{"title": "x", "done_when": "y"}]}),
    json.dumps({"steps": [{"title": "x"}]}),
    json.dumps({"steps": [{"title": "x", "done_when": "y",
                            "extra": 1}]}),
    json.dumps({"steps": [{"title": "x" * 121, "done_when": "y"}]}),
    json.dumps({"steps": [{"title": f"s{i}", "done_when": "y"}
                           for i in range(9)]}),
])
def test_validate_plan_rejects_violations(bad):
    plan, err = dlg.validate_plan(bad)
    assert plan is None and err


def test_validate_verdict_shapes():
    verdict, gaps, err = dlg.validate_verdict(
        '{"verdict": "fail", "gaps": ["missing sources"]}')
    assert (verdict, gaps, err) == ("fail", ["missing sources"],
                                    "")
    verdict, gaps, err = dlg.validate_verdict('{"verdict": "pass"}')
    assert (verdict, gaps, err) == ("pass", [], "")
    verdict, gaps, err = dlg.validate_verdict('{"verdict": "meh"}')
    assert verdict is None and err
    verdict, gaps, err = dlg.validate_verdict("prose, no json")
    assert verdict is None and err
    verdict, gaps, err = dlg.validate_verdict(
        '{"verdict": "fail", "gaps": []}')
    assert verdict == "fail" and gaps  # a nameless gap is named so


def test_bad_plan_reasks_once_then_fails(bound):
    model = make_model(plan_fn=lambda messages, n: ("I would "
                                                    "start by…",
                                                    []))
    bound["make"](model)
    task_id, _ = dlg.delegate("unplannable")
    task = _wait_status(task_id, {"failed"})
    assert "valid plan" in task["result"]
    assert model.calls["plan"] == 2      # the ask + the one re-ask


def test_bad_plan_then_good_plan_on_reask(bound):
    replies = iter(["nope", json.dumps({"steps": [
        {"title": "Only", "done_when": "done"}]})])
    model = make_model(plan_fn=lambda messages, n: (next(replies),
                                                    []))
    bound["make"](model)
    task_id, _ = dlg.delegate("replannable")
    task = _wait_status(task_id, {"done"})
    assert task["plan"][0]["title"] == "Only"


# ── checkpoints + no replay ─────────────────────────────────────────────

def test_checkpoint_persists_after_each_step(bound):
    gate = threading.Event()
    plan = [{"title": "One", "done_when": "first done"},
            {"title": "Two", "done_when": "second done"}]

    def step_fn(messages, n):
        if "Plan step 2 of" in messages[0]["text"]:
            gate.wait(timeout=10)
            return ("Second summary.", [])
        return ("First summary.", [])

    bound["make"](make_model(plan=plan, step_fn=step_fn))
    task_id, _ = dlg.delegate("two steps")
    deadline = time.time() + 10
    while time.time() < deadline:
        task = dlg.get_task(task_id)
        states = task["steps_state"]
        if states and states[0]["status"] == "done":
            break
        time.sleep(0.02)
    task = dlg.get_task(task_id)
    assert task["steps_state"][0]["summary"] == "First summary."
    assert task["current_step"] == 1
    assert task["checkpoint_at"]
    assert task["draft_result"] == "First summary."
    gate.set()
    _wait_status(task_id, {"done"})


def test_resume_never_replays_completed_steps(bound, monkeypatch):
    monkeypatch.setattr(dlg, "MODEL_RETRIES", 0)
    state = {"model_down": True, "step1_calls": 0, "pings": 0}
    plan = [{"title": "Ping step", "done_when": "pinged"},
            {"title": "Fragile step", "done_when": "survived"}]
    inner = make_model(plan=plan)

    def model(messages, decls):
        if phase_of(messages) == "step":
            if "Plan step 1 of 2: Ping step" in messages[0]["text"]:
                state["step1_calls"] += 1
                if state["step1_calls"] == 1:
                    return ("", [{"name": "ping", "args": {}}])
                return ("Pinged once.", [])
            if "Plan step 2 of 2: Fragile step" in \
                    messages[0]["text"] and state["model_down"]:
                raise RuntimeError("ladder down")
        return inner(messages, decls)

    def dispatch(name, args):
        state["pings"] += 1
        return "pong"

    monkeypatch.setattr(dlg, "_retry_delay_s", 0)
    dlg.bind(call_model=model, dispatch=dispatch,
             tool_declarations=lambda: [{"name": "ping"}])
    task_id, _ = dlg.delegate("two fragile steps")
    task = _wait_status(task_id, {"blocked_model"})
    assert task["steps_state"][0]["status"] == "done"
    state["model_down"] = False
    ok, message = dlg.resume(task_id)
    assert ok and "resumed" in message
    task = _wait_status(task_id, {"done"})
    assert state["pings"] == 1             # the ping never replayed
    assert task["steps_state"][0]["summary"] == "Pinged once."


# ── F1: restart between steps / mid-call ────────────────────────────────

def test_restart_between_steps_auto_resumes(bound):
    _write_store(bound["store"], [_record()])
    dlg.reset_for_tests()
    model = make_model()
    bound["make"](model)                  # bind = the new process
    task = _wait_status(1, {"done"})
    assert task["steps_state"][0]["summary"] == "first evidence"
    assert model.calls["step"] == 1       # only step 2 ever ran
    assert task["result"] == "Step complete."


def test_restart_mid_call_parks_blocked_unknown(bound):
    _write_store(bound["store"], [_record(inflight_call="ping")])
    dlg.reset_for_tests()
    seen = bound["make"](make_model())
    task = dlg.get_task(1)
    assert task["status"] == "blocked_unknown"
    assert "ping" in task["blocked_reason"]
    ok, message = dlg.resume(1)
    assert ok and "skipped" in message and "ping" in message
    task = _wait_status(1, {"done"})
    assert task["skipped_calls"] == ["ping"]
    assert seen["dispatches"] == []       # the unknown call: no replay


# ── F3: retries, blocked_model, auto-requeue on bind ────────────────────

def test_model_failure_retries_then_succeeds(bound, monkeypatch):
    monkeypatch.setattr(dlg, "MODEL_RETRIES", 3)
    attempts = {"plan": 0}
    inner = make_model()

    def plan_fn(messages, n):
        attempts["plan"] += 1
        if attempts["plan"] < 3:
            raise RuntimeError("wobble")
        return inner(messages, [])

    bound["make"](make_model(plan_fn=plan_fn))
    task_id, _ = dlg.delegate("wobbly start")
    _wait_status(task_id, {"done"})
    assert attempts["plan"] == 3


def test_blocked_model_requeues_on_next_bind(bound, monkeypatch):
    monkeypatch.setattr(dlg, "MODEL_RETRIES", 0)

    def boom(messages, decls):
        raise RuntimeError("ladder down")

    dlg.bind(call_model=boom, dispatch=lambda n, a: "",
             tool_declarations=lambda: [])
    task_id, _ = dlg.delegate("wait for the ladder")
    _wait_status(task_id, {"blocked_model"})
    dlg.reset_for_tests()                  # the next launch
    bound["make"](make_model())
    task = _wait_status(task_id, {"done"})
    assert task["result"] == "Step complete."


def test_need_input_parks_blocked_input(bound):
    model = make_model(step_fn=lambda messages, n:
                       ("NEED_INPUT: what is the budget?", []))
    bound["make"](model)
    task_id, _ = dlg.delegate("needs a human answer")
    task = _wait_status(task_id, {"blocked_input"})
    assert "budget" in task["blocked_reason"]
    ok, _ = dlg.resume(task_id)           # need satisfied out of band
    task = _wait_status(task_id, {"blocked_input", "done"})
    assert task["status"] in ("blocked_input", "done")


# ── F2: verification ────────────────────────────────────────────────────

def test_verifier_fail_repairs_then_passes(bound):
    verdicts = iter(["fail", "pass"])
    plan = [{"title": "Draft", "done_when": "draft exists"}]
    inner = make_model(plan=plan)

    def model(messages, decls):
        if phase_of(messages) == "verify":
            v = next(verdicts)
            gaps = ["missing sources"] if v == "fail" else []
            return (json.dumps({"verdict": v, "gaps": gaps}), [])
        return inner(messages, decls)

    dlg.bind(call_model=model, dispatch=lambda n, a: "ok",
             tool_declarations=lambda: [])
    task_id, _ = dlg.delegate("verify me")
    task = _wait_status(task_id, {"done"})
    assert task["repair_loops"] == 1
    assert len(task["plan"]) == 2         # the repair step was added
    assert task["plan"][1]["title"].startswith("Repair")
    assert task["verification"]["status"] == "passed"


def test_verifier_fail_forever_ends_done_unverified(bound):
    inner = make_model()

    def model(messages, decls):
        if phase_of(messages) == "verify":
            return (json.dumps({"verdict": "fail",
                                 "gaps": ["still wrong"]}), [])
        return inner(messages, decls)

    dlg.bind(call_model=model, dispatch=lambda n, a: "ok",
             tool_declarations=lambda: [])
    task_id, _ = dlg.delegate("never good enough")
    task = _wait_status(task_id, {"done_unverified"})
    assert task["repair_loops"] == dlg.MAX_REPAIR_LOOPS
    assert "still wrong" in task["result"]
    assert task["verification"]["status"] == "failed"
    assert len(task["plan"]) == 1 + dlg.MAX_REPAIR_LOOPS


def test_verifier_down_ends_done_unverified(bound, monkeypatch):
    monkeypatch.setattr(dlg, "MODEL_RETRIES", 0)
    inner = make_model()

    def model(messages, decls):
        if phase_of(messages) == "verify":
            raise RuntimeError("verifier unreachable")
        return inner(messages, decls)

    dlg.bind(call_model=model, dispatch=lambda n, a: "ok",
             tool_declarations=lambda: [])
    task_id, _ = dlg.delegate("unverifiable")
    task = _wait_status(task_id, {"done_unverified"})
    assert task["verification"]["status"] == "unavailable"
    assert "Unverified" in task["result"]


def test_verifier_garbage_ends_done_unverified(bound):
    inner = make_model()

    def model(messages, decls):
        if phase_of(messages) == "verify":
            return ("I think it went fine, honestly.", [])
        return inner(messages, decls)

    dlg.bind(call_model=model, dispatch=lambda n, a: "ok",
             tool_declarations=lambda: [])
    task_id, _ = dlg.delegate("judged by prose")
    task = _wait_status(task_id, {"done_unverified"})
    assert task["verification"]["status"] == "invalid"


# ── step budget ─────────────────────────────────────────────────────────

def test_step_turn_budget_fails_the_task(bound, monkeypatch):
    monkeypatch.setattr(dlg, "STEP_TURNS", 2)
    bound["make"](make_model(
        step_fn=lambda messages, n: ("", [{"name": "ping",
                                            "args": {}}])))
    task_id, _ = dlg.delegate("one endless step")
    task = _wait_status(task_id, {"failed"})
    assert "Step 1" in task["result"] and "turns" in task["result"]


# ── task_resume: paths + refusals ───────────────────────────────────────

def test_task_resume_action_and_refusals(bound):
    bound["make"](make_model())
    out = tr_action.task_resume({"task_id": 4242})
    assert out.startswith("status=failed")
    assert "no background task" in out
    out = tr_action.task_resume({"task_id": "abc"})
    assert out.startswith("status=failed")
    task_id, _ = dlg.delegate("quick")
    _wait_status(task_id, {"done"})
    ok, message = dlg.resume(task_id)
    assert not ok and "already done" in message


def test_resume_refuses_confirm_block_and_active(bound):
    _write_store(bound["store"], [
        _record(id=1, status="blocked", phase="execute",
                blocked_reason="confirmation gate"),
        _record(id=2, status="running", phase="execute"),
    ], next_id=3)
    dlg.reset_for_tests()
    # The bind recovers task 2 (no in-flight call) and resumes it;
    # hold its step turn on a gate so it is genuinely ACTIVE while
    # the refusals are checked — and wait for it at the end: a
    # worker left running past the test would outlive the
    # fixture's tmp store and write into the real one.
    gate = threading.Event()
    inner = make_model()

    def model(messages, decls):
        if phase_of(messages) == "step":
            gate.wait(timeout=10)
        return inner(messages, decls)

    bound["make"](model)
    _wait_status(2, {"running"})
    ok, message = dlg.resume(1)
    assert not ok and "confirmation gate" in message
    ok, message = dlg.resume(2)
    assert not ok and "already" in message
    gate.set()
    _wait_status(2, {"done"})


def test_resume_counts_against_max_active(bound):
    release = threading.Event()
    inner = make_model()

    def blocking(messages, decls):
        release.wait(timeout=10)
        return inner(messages, decls)

    # Record 50 goes in BEFORE any worker starts: the store's
    # writers are module-internal (load-modify-save under the
    # module lock), so an external write while workers are live
    # can be lost under a worker's checkpoint save — the flake
    # this ordering removes. delegate() then appends under the
    # lock and record 50 survives every checkpoint.
    _write_store(bound["store"],
                 [_record(id=50, status="blocked_input",
                          blocked_reason="needs the user")],
                 next_id=99)
    bound["make"](blocking)
    ids = [dlg.delegate(f"job {i}")[0] for i in range(dlg.MAX_ACTIVE)]
    ok, message = dlg.resume(50)
    assert not ok and "already in flight" in message
    release.set()
    for tid in ids:
        _wait_status(tid, {"done"})


# ── rendering + history bounds ──────────────────────────────────────────

def test_rendering_shows_engine_state():
    task = _record(status="blocked_model",
                   blocked_reason="the model could not be reached",
                   verification={"status": "failed",
                                 "gaps": ["thin evidence"],
                                 "loops": 1, "at": 4.0})
    text = dlg.render_task(task)
    assert "step 2 of 2" in text
    assert "first evidence" in text
    assert "Checkpoint:" in text
    assert "Blocked: the model could not be reached" in text
    assert "Verification: failed" in text
    assert "thin evidence" in text
    listing = dlg.render_list([task])
    assert "blocked_model" in listing and "step" in listing


def test_rendering_tolerates_legacy_records():
    legacy = {"id": 7, "prompt": "old sprint task", "status": "done",
              "result": "kept", "created": 1.0, "started": 2.0,
              "finished": 3.0}
    text = dlg.render_task(legacy)
    assert "old sprint task" in text and "kept" in text
    assert "Plan" not in text
    assert dlg.render_list([legacy]).startswith("#7 [done]")


def test_trim_history_bounds_text_and_length():
    long_text = "x" * (dlg.HISTORY_TEXT_CAP + 500)
    messages = [{"role": "user", "text": "open"}] + [
        {"role": "tool",
         "results": [{"name": "ping", "result": long_text}]}
        for _ in range(dlg.HISTORY_MAX_MSGS + 10)]
    trimmed = dlg._trim_history(messages)
    assert len(trimmed) == dlg.HISTORY_MAX_MSGS
    assert trimmed[0]["text"] == "open"   # the opening survives
    result = trimmed[-1]["results"][0]["result"]
    assert len(result) < len(long_text) and "truncated" in result
