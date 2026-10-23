"""tests/test_restart_program.py — restart_program: "restart your
program" relaunches ARIA herself, never the computer.

The gap this closes: the power-intent gate (correctly) refuses to arm
the COMPUTER restart for phrases like "restart your program" — but
until now the phrase routed nowhere, because ARIA had no program
restart action at all. restart_program is the missing target, built
beside shutdown_jarvis (an inline declaration + an inline dispatch
branch in main.py) and riding the code watcher's machinery: speak()
announces, core/codewatch's PROCESS_ARBITER is armed, a waiter thread
holds for the bounded speech drain, and _restart_after_self_edit
does the relaunch (summary save -> Popen same interpreter + script
-> exit).

Pinned here:
  * registration — the inline declaration (immediately after
    shutdown_jarvis), the merged Commands registry, and the headless
    providers-schema parse all carry the tool;
  * the sequence — announce, drain, settle, relaunch, in that order,
    with the shared arbiter armed under "restart_program";
  * the computer is never touched — no computer_settings subprocess,
    no confirmation, no power-intent arm spent or created;
  * honest unavailability — no session loop, or no script to
    relaunch, returns a refusal in her voice and arms/announces
    nothing; a restart already armed by another path is reported,
    not doubled;
  * exclusions mirror shutdown_jarvis exactly — out of the delegated
    tool set (main.py's _DELEGATED_EXCLUDE + core/delegation.py's
    stated rule), and treated identically by core/routines;
  * the prompt routes the phrases to restart_program in [EXECUTION].
"""
import ast
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main
from core import codewatch as codewatch_core
from core import confirm, power_intent, routines


@pytest.fixture(autouse=True)
def _clean_state():
    codewatch_core.PROCESS_ARBITER.reset()
    power_intent._reset()
    yield
    codewatch_core.PROCESS_ARBITER.reset()
    power_intent._reset()


# ── A JarvisLive with only the seams _restart_program touches ───────────────

class _SyncThread:
    """threading.Thread stand-in: start() runs the target inline, so
    the announce -> drain -> relaunch order lands in one event list
    the test can read end to end."""

    def __init__(self, target=None, daemon=None, name=None):
        self._target = target
        self.name = name

    def start(self):
        self._target()


def _make_live(monkeypatch, events, speaking=False):
    live = main.JarvisLive.__new__(main.JarvisLive)
    live._loop = object()  # a running loop: the seam is available
    live._is_speaking = speaking
    live.ui = SimpleNamespace(
        write_log=lambda msg: events.append(("log", msg)))
    live.speak = lambda text: events.append(("speak", text))
    live._restart_after_self_edit = lambda: events.append(("restart",))
    monkeypatch.setattr(threading, "Thread", _SyncThread)
    monkeypatch.setattr(
        time, "sleep", lambda s: events.append(("sleep", s)))
    return live


# ── Registration ─────────────────────────────────────────────────────────────

def test_declared_inline_beside_shutdown():
    names = [t["name"] for t in main.TOOL_DECLARATIONS]
    assert names.count("restart_program") == 1
    assert names.index("restart_program") == \
        names.index("shutdown_jarvis") + 1
    decl = next(t for t in main.TOOL_DECLARATIONS
                if t["name"] == "restart_program")
    assert decl["description"]  # a row with no description is broken


def test_merged_registry_carries_it_as_builtin():
    from core.action_loader import discover_actions
    from core.commands import build_command_list
    from core.plugin_loader import discover_plugins

    inline = main.TOOL_DECLARATIONS
    reserved = {t["name"] for t in inline}
    actions = discover_actions(ROOT / "actions", reserved_names=reserved,
                               logger=lambda msg: None)
    plugins = discover_plugins(
        ROOT / "plugins",
        core_tool_names=reserved | actions.names(),
        logger=lambda msg: None)
    rows = build_command_list(inline, actions.get_tool_declarations(),
                              plugins.get_tool_declarations())
    row = next(r for r in rows if r["name"] == "restart_program")
    assert row["source"] == "Built-in"


def test_headless_schema_parse_carries_it():
    """The providers pipeline reads TOOL_DECLARATIONS out of main.py's
    source (providers/schema.py's method) — the tool must survive
    that parse and the OpenAI conversion the headless path applies."""
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    decls = None
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(
                getattr(t, "id", "") == "TOOL_DECLARATIONS"
                for t in node.targets):
            decls = ast.literal_eval(node.value)
            break
    assert decls is not None
    names = [d["name"] for d in decls]
    assert "restart_program" in names

    from providers.schema import to_openai
    decl = next(d for d in decls if d["name"] == "restart_program")
    converted = to_openai([decl])
    assert converted[0]["function"]["name"] == "restart_program"


def test_commands_example_phrase_names_it():
    from core.commands import EXAMPLE_PHRASES
    assert EXAMPLE_PHRASES.get("restart_program") == "Restart your program"


# ── The sequence: announce -> drain -> settle -> relaunch ────────────────────

def test_restart_announces_drains_and_relaunches(monkeypatch):
    events = []
    live = _make_live(monkeypatch, events, speaking=False)
    result = live._restart_program()
    assert result == "Restarting my program now."
    kinds = [k for k, *_ in events]
    assert ("speak", "Restarting my program now — back in a moment.") \
        in events
    # Not speaking: no drain polls, just the settle, then the relaunch.
    assert events[-2:] == [("sleep", 2.0), ("restart",)]
    assert kinds.index("speak") < kinds.index("restart")
    assert codewatch_core.PROCESS_ARBITER.armed_by == "restart_program"


def test_restart_waits_for_speech_to_drain(monkeypatch):
    events = []
    live = _make_live(monkeypatch, events, speaking=True)
    polls = []

    def fake_sleep(s):
        events.append(("sleep", s))
        if s == 0.5:
            polls.append(s)
            if len(polls) >= 2:
                live._is_speaking = False

    monkeypatch.setattr(time, "sleep", fake_sleep)
    result = live._restart_program()
    assert result == "Restarting my program now."
    sleeps = [e[1] for e in events if e[0] == "sleep"]
    assert sleeps[:2] == [0.5, 0.5]        # drain polls while speaking
    assert sleeps[-1] == 2.0              # then the settle
    kinds = [k for k, *_ in events]
    assert kinds.index("restart") == len(kinds) - 1


def test_no_computer_power_path_is_touched(monkeypatch):
    import actions.computer_settings as cs
    calls = []
    monkeypatch.setattr(cs.subprocess, "run",
                        lambda *a, **k: calls.append(a))
    events = []
    live = _make_live(monkeypatch, events)
    assert live._restart_program() == "Restarting my program now."
    assert calls == []                              # no shutdown argv, ever
    assert confirm.pending_title() == ""            # no banner requested
    assert power_intent.armed("restart") is False   # no arm created/spent
    assert power_intent.armed("shutdown") is False


# ── Honest unavailability ────────────────────────────────────────────────────

def test_no_session_loop_is_an_honest_refusal(monkeypatch):
    events = []
    live = _make_live(monkeypatch, events)
    live._loop = None  # headless: nothing to schedule a relaunch onto
    result = live._restart_program()
    assert "can't restart my program" in result
    assert events == []  # nothing announced, nothing relaunched
    assert codewatch_core.PROCESS_ARBITER.armed_by is None


def test_no_script_to_relaunch_is_an_honest_refusal(monkeypatch):
    events = []
    live = _make_live(monkeypatch, events)
    monkeypatch.setattr(sys, "argv", [""])
    result = live._restart_program()
    assert "can't restart my program" in result
    assert events == []
    assert codewatch_core.PROCESS_ARBITER.armed_by is None


def test_restart_already_armed_is_reported_not_doubled(monkeypatch):
    events = []
    live = _make_live(monkeypatch, events)
    assert codewatch_core.PROCESS_ARBITER.try_arm("codewatch")
    result = live._restart_program()
    assert "already on its way" in result
    assert events == []
    assert codewatch_core.PROCESS_ARBITER.armed_by == "codewatch"


# ── Exclusions: restart_program mirrors shutdown_jarvis exactly ─────────────

def test_excluded_from_delegated_tasks_like_shutdown():
    excluded = main.JarvisLive._DELEGATED_EXCLUDE
    assert "shutdown_jarvis" in excluded
    assert "restart_program" in excluded


def test_delegated_declarations_filter_uses_the_exclusion_set():
    import inspect
    src = inspect.getsource(main.JarvisLive._delegated_tool_declarations)
    assert "_DELEGATED_EXCLUDE" in src


def test_delegation_docstring_states_the_rule_for_both():
    src = (ROOT / "core" / "delegation.py").read_text(encoding="utf-8")
    assert "shutdown_jarvis and restart_program" in src


def test_routines_treatment_matches_shutdown():
    # shutdown_jarvis is in NO routines exclusion list (capture or
    # replay); restart_program must be treated identically — neither
    # added, neither missing.
    assert ("restart_program" in routines._EXCLUDED_TOOLS) == \
           ("shutdown_jarvis" in routines._EXCLUDED_TOOLS)


# ── Wiring + prompt routing (source pins) ────────────────────────────────────

def test_dispatch_branch_beside_shutdown():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert 'elif name == "shutdown_jarvis":' in src
    assert 'elif name == "restart_program":' in src
    assert src.index('elif name == "restart_program":') > \
        src.index('elif name == "shutdown_jarvis":')
    assert "result = self._restart_program()" in src


def test_prompt_routes_the_phrases_in_execution():
    text = (ROOT / "core" / "prompt.txt").read_text(encoding="utf-8")
    execution = text.split("[EXECUTION]", 1)[1].split("[YOUR OWN CODE]", 1)[0]
    assert "Only call shutdown_jarvis when the user explicitly ends " \
           "the session." in execution
    assert '"Restart your program", "restart yourself", ' \
           '"restart the app" mean' in execution
    assert "restart_program — your own program relaunching, never " \
           "the computer." in execution
    assert execution.index("restart_program — your own program") > \
        execution.index("Only call shutdown_jarvis")
