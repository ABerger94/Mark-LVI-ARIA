"""Phase F1 — routines (ARIA's Module 1, ported).

The failures being pinned shut are the ones a record/replay system can
commit quietly: capturing its own control tools into a routine (a
routine that stops its own recording, or replays itself), running a
step with a literal {{placeholder}} still in its arguments, carrying on
past a step that failed as if it had happened, and — the dangerous one
— a replay that walks a destructive step past the confirmation gate
because "the routine is trusted" was asserted somewhere the gate never
looked.

The dispatch used here is the real one for actions: a genuine
ActionRegistry over stub action files, wrapped the way main.py's
_execute_tool wraps it (execute, then offer the completed call to the
capture hook). The hook's placement in main.py itself is pinned by
source inspection, the same technique as test_commands' wiring test.
"""
import inspect
import json
import sys
import time
import zipfile
from pathlib import Path

import pytest

from core import confirm, routines
from core.action_loader import discover_actions

ROOT = Path(__file__).resolve().parent.parent

TEN = ("routine_record_start", "routine_record_stop", "run_routine",
       "list_routines", "describe_routine", "delete_routine",
       "trust_routine", "untrust_routine", "export_routine_pack",
       "import_routine_pack")


# ── stub actions ─────────────────────────────────────────────────────────────
# greet: benign. explode: fails with an outcome line. wobble: unverified.
# wipe_thing: destructive, gated exactly the way file_controller's delete
# is gated — the same confirm.request call, the same pending pre-check.

_STUBS = {
    "greet": '''
from core import outcome


def greet(parameters=None, response=None, player=None,
          session_memory=None, speak=None):
    who = (parameters or {}).get("who", "world")
    return outcome.done("greet", who=who)


TOOL = {"name": "greet", "description": "Say hello to someone.",
        "parameters": {"type": "OBJECT",
                       "properties": {"who": {"type": "STRING"}}},
        "handler": greet}
''',
    "explode": '''
from core import outcome


def explode(parameters=None, response=None, player=None,
            session_memory=None, speak=None):
    return outcome.failed("explode", "it blew up on purpose")


TOOL = {"name": "explode", "description": "Always fails, on purpose.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": explode}
''',
    "wobble": '''
from core import outcome


def wobble(parameters=None, response=None, player=None,
           session_memory=None, speak=None):
    return outcome.unverified("wobble", "the result could not be checked")


TOOL = {"name": "wobble", "description": "Always unverified, on purpose.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": wobble}
''',
    "wipe_thing": '''
from core import confirm, outcome

STATE = {"done": []}


def _do_wipe():
    STATE["done"].append(True)
    return outcome.done("wipe_thing", wiped=True)


def wipe_thing(parameters=None, response=None, player=None,
               session_memory=None, speak=None):
    if confirm.pending_title():
        return ("There is already a confirmation waiting on screen — "
                "answer that one first. Nothing was wiped.")
    return confirm.request(
        key="stub_wipe",
        title="Wipe the stub thing",
        detail="A test gate, shaped like file deletion's.",
        run=_do_wipe,
    )


TOOL = {"name": "wipe_thing", "description": "Destructive stub, gated.",
        "parameters": {"type": "OBJECT", "properties": {}},
        "handler": wipe_thing}
''',
}


class Dispatch:
    """JarvisLive._execute_tool, reduced to what routines interact with:
    run the call through the real registry, then offer the completed
    call to the capture hook. Records what actually executed."""

    def __init__(self, *registries):
        self.registries = registries
        self.calls = []

    def __call__(self, name, args=None):
        args = dict(args or {})
        self.calls.append((name, args))
        for reg in self.registries:
            if reg.has(name):
                result = reg.run(name, args, {})
                break
        else:
            result = f"Unknown tool: {name}"
        routines.maybe_capture(name, args)
        return result

    def names(self):
        out = set(TEN)
        for reg in self.registries:
            out |= reg.names()
        return out

    def count(self, name):
        return sum(1 for n, _ in self.calls if n == name)


@pytest.fixture(scope="module")
def repo_actions():
    return discover_actions(ROOT / "actions", reserved_names=set(),
                            logger=lambda msg: None)


@pytest.fixture(scope="module")
def stub_actions(tmp_path_factory):
    d = tmp_path_factory.mktemp("stub_actions")
    for stem, src in _STUBS.items():
        (d / f"{stem}.py").write_text(src, encoding="utf-8")
    return discover_actions(d, reserved_names=set(),
                            logger=lambda msg: None)


@pytest.fixture()
def env(tmp_path, monkeypatch, repo_actions, stub_actions):
    """A clean routine store, no executor, no UI, no parked banner."""
    monkeypatch.setattr(routines, "ROUTINES_DIR", tmp_path / "routines")
    routines._RECORDING.update({"name": None, "steps": [],
                                 "started_ts": None})
    routines.bind_executor(None, None)
    confirm.bind(None, None)
    with confirm._lock:
        confirm._pending = None
    wipe = sys.modules.get("actions.wipe_thing")
    if wipe is not None:
        wipe.STATE["done"].clear()
    dispatch = Dispatch(repo_actions, stub_actions)
    routines.bind_executor(dispatch, dispatch.names)
    return dispatch


def _record(dispatch, name, steps):
    """Drive a recording through the dispatch, as a live session would."""
    start = dispatch("routine_record_start", {"name": name})
    assert start.startswith("Recording routine"), start
    for tool, args in steps:
        dispatch(tool, args)
    stop = dispatch("routine_record_stop", {})
    assert stop.startswith("Saved routine"), stop
    return stop


def _wait_for(predicate, timeout=10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


# ── discovery ────────────────────────────────────────────────────────────────

def test_all_ten_routine_tools_are_registered(repo_actions):
    assert set(TEN) <= repo_actions.names()
    decls = {d["name"]: d for d in repo_actions.get_tool_declarations()}
    for name in TEN:
        assert decls[name]["description"].strip(), name


# ── record / stop / list / describe ──────────────────────────────────────────

def test_record_stop_list_describe_roundtrip(env):
    d = env
    _record(d, "Morning Setup", [
        ("greet", {"who": "alek"}),
        ("greet", {"who": "{{who}}"}),
    ])

    path = routines.ROUTINES_DIR / "morning_setup.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["name"] == "morning_setup"
    assert data["trusted"] is False  # never born trusted
    assert [(s["tool"], s["args"]) for s in data["steps"]] == [
        ("greet", {"who": "alek"}), ("greet", {"who": "{{who}}"})]

    listed = d("list_routines", {})
    assert "morning_setup" in listed
    assert "2 step(s)" in listed and "trusted: no" in listed

    desc = d("describe_routine", {"name": "morning_setup"})
    assert "greet(who='alek')" in desc
    assert "Parameters: {{who}}" in desc


def test_capture_excludes_routine_tools_and_save_memory(env):
    d = env
    d("routine_record_start", {"name": "exclusions"})
    for name in TEN + ("save_memory",):
        routines.maybe_capture(name, {"x": 1})
    d("greet", {"who": "kept"})
    stop = d("routine_record_stop", {})
    assert "1 step(s)" in stop
    data = json.loads(
        (routines.ROUTINES_DIR / "exclusions.json").read_text())
    assert [s["tool"] for s in data["steps"]] == ["greet"]


def test_record_start_refusals(env):
    d = env
    d("routine_record_start", {"name": "first"})
    again = d("routine_record_start", {"name": "second"})
    assert again.startswith("status=failed")
    assert "Already recording" in again
    d("routine_record_stop", {})

    dupe = d("routine_record_start", {"name": "first"})
    assert dupe.startswith("status=failed")
    assert "already exists" in dupe

    stop = d("routine_record_stop", {})
    assert stop.startswith("status=failed")  # nothing was recording


# ── replay ───────────────────────────────────────────────────────────────────

def test_replay_substitutes_params_everywhere(env):
    d = env
    _record(d, "param_demo", [
        ("greet", {"who": "{{who}}",
                   "nested": {"items": ["{{who}}", 3]}}),
    ])
    d.calls.clear()
    result = d("run_routine", {
        "name": "param_demo",
        "params_json": json.dumps({"who": "april"}),
    })
    assert result.startswith("status=done"), result
    greet_args = [a for n, a in d.calls if n == "greet"]
    assert greet_args == [{"who": "april",
                           "nested": {"items": ["april", 3]}}]


def test_replay_missing_param_fails_before_any_step(env):
    d = env
    _record(d, "needs_param", [("greet", {"who": "{{who}}"})])
    d.calls.clear()
    result = d("run_routine", {"name": "needs_param", "params_json": "{}"})
    assert result.startswith("status=failed"), result
    assert "missing parameter(s): who" in result
    assert d.count("greet") == 0  # nothing ran


def test_replay_preflight_unknown_tool(env, tmp_path):
    d = env
    routines.ROUTINES_DIR.mkdir(parents=True, exist_ok=True)
    (routines.ROUTINES_DIR / "ghost.json").write_text(json.dumps({
        "name": "ghost", "created": "2026-10-21T00:00:00+00:00",
        "trusted": False,
        "steps": [{"tool": "no_such_tool", "args": {}}],
    }), encoding="utf-8")
    result = d("run_routine", {"name": "ghost"})
    assert result.startswith("status=failed"), result
    assert "unknown tool(s): no_such_tool" in result
    assert d.count("no_such_tool") == 0


def test_replay_failing_step_halts(env):
    d = env
    _record(d, "boom", [("greet", {"who": "one"}),
                        ("explode", {}),
                        ("greet", {"who": "three"})])
    d.calls.clear()
    result = d("run_routine", {"name": "boom"})
    assert "stopped: step 2 (explode) failed" in result, result
    assert not result.startswith("status=done")
    # The step after the failure never ran.
    assert [a["who"] for n, a in d.calls if n == "greet"] == ["one"]

    report = routines.run_routine("boom", {})
    assert report.completed == 1 and report.total == 3
    assert "step 2" in (report.halted or "")


def test_replay_unverified_step_continues(env):
    d = env
    _record(d, "wobbly", [("wobble", {}), ("greet", {"who": "after"})])
    result = d("run_routine", {"name": "wobbly"})
    assert result.startswith("status=done"), result
    assert "unverified — continuing" in result
    assert d.count("greet") >= 1


def test_replay_unknown_routine_and_bad_params(env):
    d = env
    missing = d("run_routine", {"name": "nope"})
    assert missing.startswith("status=failed")
    assert "not found" in missing
    bad = d("run_routine", {"name": "nope", "params_json": "{not json"})
    assert bad.startswith("status=failed")
    assert "not valid JSON" in bad


# ── trust and the gate ───────────────────────────────────────────────────────

def _bind_fake_ui():
    shown = []
    confirm.bind(show=lambda title, detail: shown.append(title),
                 hide=lambda: None, log=None)
    return shown


def _wipe_state():
    return sys.modules["actions.wipe_thing"].STATE["done"]


def _clear_banner(shown=None):
    """Recording performs the steps LIVE, so recording a gated step with
    a UI bound parks a real banner for it — the user answers it at the
    time, as they would in a session. Clear it (and the show record) so
    each replay starts from a clean screen."""
    with confirm._lock:
        confirm._pending = None
    if shown is not None:
        shown.clear()


def test_untrusted_replay_parks_at_the_gate_and_halts(env):
    d = env
    shown = _bind_fake_ui()
    _record(d, "wipe_demo", [("greet", {"who": "before"}),
                             ("wipe_thing", {}),
                             ("greet", {"who": "after"})])
    _clear_banner(shown)  # the live wipe's banner, answered at the time
    d.calls.clear()
    result = d("run_routine", {"name": "wipe_demo"})

    # The destructive step did NOT run; it is parked for a human, and
    # the step after it did not run either.
    assert _wipe_state() == []
    assert confirm.pending_title() == "Wipe the stub thing"
    assert shown == ["Wipe the stub thing"]
    assert "stopped: step 2 (wipe_thing)" in result, result
    assert "waiting at the confirmation gate" in result
    assert [a["who"] for n, a in d.calls if n == "greet"] == ["before"]

    # The human answering the banner is what runs the parked step.
    confirm.resolve(True)
    assert _wait_for(lambda: _wipe_state() == [True])


def test_trusted_replay_runs_destructive_steps_without_asking(env):
    d = env
    shown = _bind_fake_ui()
    _record(d, "wipe_trusted", [("greet", {"who": "before"}),
                                ("wipe_thing", {}),
                                ("greet", {"who": "after"})])
    _clear_banner(shown)  # the live wipe's banner, answered at the time
    trust = d("trust_routine", {"name": "wipe_trusted"})
    assert "now trusted" in trust

    d.calls.clear()
    result = d("run_routine", {"name": "wipe_trusted"})
    assert result.startswith("status=done"), result
    assert "trusted=yes" in result
    assert _wipe_state() == [True]      # the step ran, inline
    assert shown == []                   # and nobody was asked
    assert confirm.pending_title() == ""
    assert [a["who"] for n, a in d.calls if n == "greet"] == \
        ["before", "after"]

    # Untrusting restores the gate.
    d("untrust_routine", {"name": "wipe_trusted"})
    _wipe_state().clear()
    d.calls.clear()
    result = d("run_routine", {"name": "wipe_trusted"})
    assert _wipe_state() == []
    assert confirm.pending_title() == "Wipe the stub thing"
    confirm.resolve(False)


def test_untrusted_replay_without_ui_cannot_run_destructive_step(env):
    d = env
    # env bound no interface: confirm.bind(None, None) in the fixture.
    _record(d, "wipe_headless", [("wipe_thing", {})])
    result = d("run_routine", {"name": "wipe_headless"})
    assert _wipe_state() == []
    assert "stopped: step 1 (wipe_thing)" in result, result


def test_trust_untrust_roundtrip_and_missing(env):
    d = env
    _record(d, "flags", [("greet", {})])
    assert "now trusted" in d("trust_routine", {"name": "flags"})
    data = json.loads((routines.ROUTINES_DIR / "flags.json").read_text())
    assert data["trusted"] is True
    assert "trusted: yes" in d("describe_routine", {"name": "flags"})
    assert "now untrusted" in d("untrust_routine", {"name": "flags"})
    data = json.loads((routines.ROUTINES_DIR / "flags.json").read_text())
    assert data["trusted"] is False
    missing = d("trust_routine", {"name": "ghost"})
    assert missing.startswith("status=failed")


# ── delete, gated ────────────────────────────────────────────────────────────

def test_delete_routine_is_confirm_gated(env):
    d = env
    shown = _bind_fake_ui()
    _record(d, "disposable", [("greet", {})])
    path = routines.ROUTINES_DIR / "disposable.json"
    assert path.exists()

    result = d("delete_routine", {"name": "disposable"})
    assert "[CONFIRMATION_PENDING]" in result, result
    assert shown and "Delete routine 'disposable'" in shown[0]
    assert path.exists()  # nothing deleted yet

    confirm.resolve(False)  # CANCEL: still there
    assert path.exists()

    d("delete_routine", {"name": "disposable"})
    confirm.resolve(True)   # CONFIRM: the parked callable runs
    assert _wait_for(lambda: not path.exists())

    missing = d("delete_routine", {"name": "disposable"})
    assert missing.startswith("status=failed")


# ── packs ────────────────────────────────────────────────────────────────────

def test_export_import_pack_lands_untrusted_and_never_overwrites(
        env, tmp_path, monkeypatch):
    d = env
    _record(d, "alpha", [("greet", {"who": "a"})])
    _record(d, "beta", [("greet", {"who": "b"})])
    d("trust_routine", {"name": "alpha"})  # trusted at the source

    pack = tmp_path / "pack.zip"
    out = d("export_routine_pack", {"dest_path": str(pack)})
    assert out.startswith("Exported 2 routine(s)"), out
    assert pack.exists()

    # A fresh store, with a DIFFERENT 'beta' already living in it.
    other = tmp_path / "other"
    monkeypatch.setattr(routines, "ROUTINES_DIR", other)
    other.mkdir(parents=True)
    (other / "beta.json").write_text(json.dumps({
        "name": "beta", "created": "2026-01-01T00:00:00+00:00",
        "trusted": True,
        "steps": [{"tool": "explode", "args": {}}],
    }), encoding="utf-8")

    result = d("import_routine_pack", {"pack_path": str(pack)})
    assert "imported: alpha" in result, result
    assert "skipped (already exist): beta" in result

    alpha = json.loads((other / "alpha.json").read_text())
    assert alpha["trusted"] is False  # trust does not travel
    beta = json.loads((other / "beta.json").read_text())
    assert beta["trusted"] is True   # and the local copy is untouched
    assert beta["steps"][0]["tool"] == "explode"

    again = d("import_routine_pack", {"pack_path": str(pack)})
    assert "imported:" not in again
    assert "alpha" in again and "beta" in again  # both skipped now


def test_import_pack_refusals(env, tmp_path):
    d = env
    not_zip = tmp_path / "nope.zip"
    not_zip.write_text("this is not a zip", encoding="utf-8")
    r1 = d("import_routine_pack", {"pack_path": str(not_zip)})
    assert r1.startswith("status=failed") and "Not a valid pack" in r1

    no_manifest = tmp_path / "bare.zip"
    with zipfile.ZipFile(no_manifest, "w") as z:
        z.writestr("alpha.json", "{}")
    r2 = d("import_routine_pack", {"pack_path": str(no_manifest)})
    assert r2.startswith("status=failed") and "no manifest" in r2

    wrong_format = tmp_path / "wrong.zip"
    with zipfile.ZipFile(wrong_format, "w") as z:
        z.writestr("manifest.json",
                   json.dumps({"format": "something-else"}))
    r3 = d("import_routine_pack", {"pack_path": str(wrong_format)})
    assert r3.startswith("status=failed") and "refusing" in r3

    r4 = d("import_routine_pack", {"pack_path": str(tmp_path / "gone.zip")})
    assert r4.startswith("status=failed") and "not found" in r4.lower()


# ── wiring pins ──────────────────────────────────────────────────────────────

def test_main_capture_hook_and_executor_binding():
    """The hook must sit in the one funnel both engines feed, and the
    executor must be bound where the other shared services are."""
    import main

    # The funnel is a wrapper (the Fleet registry's begin/finish)
    # around _execute_tool_impl, which holds the capture hook —
    # the hook must sit in that one implementation both engines feed.
    execute_src = inspect.getsource(main.JarvisLive._execute_tool_impl)
    assert "routines_core.maybe_capture(name, args)" in execute_src

    run_src = inspect.getsource(main.JarvisLive.run)
    assert "routines_core.bind_executor(self._execute_routine_step," \
        in run_src
    assert "self._all_tool_names" in run_src

    step_src = inspect.getsource(main.JarvisLive._execute_routine_step)
    assert "self._execute_tool" in step_src  # replays use the same funnel


def test_confirm_trusted_scope_is_wired_into_request():
    import inspect as _inspect

    src = _inspect.getsource(confirm.request)
    assert "in_trusted_scope()" in src
    assert not confirm.in_trusted_scope()
    with confirm.trusted_scope():
        assert confirm.in_trusted_scope()
        with confirm.trusted_scope():
            assert confirm.in_trusted_scope()
        assert confirm.in_trusted_scope()  # nesting is counted
    assert not confirm.in_trusted_scope()
