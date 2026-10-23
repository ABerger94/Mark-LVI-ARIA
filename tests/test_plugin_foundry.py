"""tests/test_plugin_foundry.py — the plugin foundry: ARIA writes new
plugins from plain words, and everything she makes lands validated,
switched OFF, and never on top of an existing file.

Pinned here:
  * a valid generation installs — with the disabled flag written through
    the config funnel BEFORE the file lands in plugins/ (plugins are
    enabled by default at discovery, so the order is the safety property);
  * staging + validation use the real machinery (py_compile, then
    discover_plugins against a temp dir with main.py's reserved names) —
    a syntax-broken first attempt is repaired on the single retry, and a
    second failure installs nothing, writes no config, and leaves the
    draft in memory/foundry/ for inspection;
  * a generated name colliding with a built-in tool is rejected by the
    loader itself and never installed;
  * an installed plugin file is never overwritten — neither via an
    explicit name up front (refused before generation runs) nor via the
    name the model generates (refused at validation);
  * the capability scan is spoken in the summary (report-only): shell and
    network flags appear for a risky plugin and not for a clean one;
  * the foundry module itself validates through the real plugin loader
    against the real plugins/ directory.
"""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from core import gemini as gemini_mod
from core.plugin_loader import discover_plugins
from memory import config_manager as cm


def _load_foundry():
    spec = importlib.util.spec_from_file_location(
        "plugins.plugin_foundry", ROOT / "plugins" / "plugin_foundry.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["plugins.plugin_foundry"] = mod
    spec.loader.exec_module(mod)
    return mod


foundry = _load_foundry()


FAKE_MAIN = '''
TOOL_DECLARATIONS = [
    {"name": "system_status", "description": "x",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "restart_program", "description": "x",
     "parameters": {"type": "OBJECT", "properties": {}}},
    {"name": "shutdown_jarvis", "description": "x",
     "parameters": {"type": "OBJECT", "properties": {}}},
]
'''

VALID_DICE = '''
PLUGIN = {
    "name": "dice_roller",
    "description": "Roll a dice when the user asks to roll a dice, a die, or a d6.",
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

def run(parameters, player=None, session_memory=None):
    return "You rolled a four."
'''

BROKEN_DICE = '''
PLUGIN = {
    "name": "dice_roller",
    "description": "Roll a dice when the user asks to roll a dice.",
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

def run(parameters, player=None, session_memory=None)
    return "the colon is missing above"
'''

COLLIDING = '''
PLUGIN = {
    "name": "restart_program",
    "description": "Restart things when the user asks about restarting.",
    "parameters": {"type": "OBJECT", "properties": {}, "required": []},
}

def run(parameters, player=None, session_memory=None):
    return "ok"
'''

RISKY = '''
import subprocess
import urllib.request

PLUGIN = {
    "name": "net_pinger",
    "description": "Check whether a host is up when the user asks to ping a site.",
    "parameters": {"type": "OBJECT", "properties": {
        "host": {"type": "STRING", "description": "Host to check"}},
        "required": []},
}

def run(parameters, player=None, session_memory=None):
    host = parameters.get("host", "example.com")
    subprocess.run(["ping", "-c", "1", host])
    urllib.request.urlopen("https://" + host, timeout=5)
    return "Done."
'''


def _fenced(source: str) -> str:
    return f"Here is the plugin:\n\n```python\n{source}\n```\n"


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A sandbox app tree: empty plugins/ and actions/, a fake main.py
    with literal TOOL_DECLARATIONS, a stand-in template, and the real
    config funnel redirected at a tmp api_keys.json."""
    root = tmp_path / "approot"
    (root / "plugins").mkdir(parents=True)
    (root / "actions").mkdir()
    (root / "memory").mkdir()
    (root / "plugins" / "_template.py").write_text(
        "# stand-in template text\n", encoding="utf-8")
    (root / "main.py").write_text(FAKE_MAIN, encoding="utf-8")
    paths = SimpleNamespace(
        root=root,
        plugins=root / "plugins",
        staging=root / "memory" / "foundry",
        actions=root / "actions",
        main_py=root / "main.py",
        template=root / "plugins" / "_template.py",
    )
    monkeypatch.setattr(foundry, "_paths", lambda: paths)
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    return paths


def _fake_gemini(monkeypatch, replies):
    """core.gemini.text standing in for the engine; returns replies in
    order (the last one repeats) and records every prompt it was given."""
    prompts = []

    def fake_text(contents, tier=None, config=None, timeout_ms=None,
                  key="", default=""):
        prompts.append(contents)
        idx = min(len(prompts) - 1, len(replies) - 1)
        return replies[idx]

    monkeypatch.setattr(gemini_mod, "text", fake_text)
    return prompts


def _plugin_files(paths):
    return sorted(p.name for p in paths.plugins.glob("*.py")
                  if not p.name.startswith("_"))


# ── Happy path: valid generation, disabled BEFORE the file lands ────────────

def test_valid_generation_installs_disabled(world, monkeypatch):
    _fake_gemini(monkeypatch, [_fenced(VALID_DICE)])

    events = []
    real_save = foundry.save_plugin_enabled

    def spy_save(name, enabled):
        events.append((name, enabled,
                       (world.plugins / f"{name}.py").exists()))
        return real_save(name, enabled)

    monkeypatch.setattr(foundry, "save_plugin_enabled", spy_save)

    result = foundry.run({"request": "a plugin that rolls a dice"})

    installed = world.plugins / "dice_roller.py"
    assert installed.is_file()
    assert installed.read_text(encoding="utf-8") == VALID_DICE.strip() + "\n"
    # The off-switch existed before the file did — that order is the point.
    assert events == [("dice_roller", False, False)]
    assert cm.get_plugin_enabled("dice_roller") is False
    assert "'dice_roller'" in result
    assert "switched OFF" in result
    # A clean plugin earns no capability warning.
    assert "Heads-up" not in result


# ── Repair retry: broken once, valid second time ────────────────────────────

def test_broken_then_valid_retry_succeeds(world, monkeypatch):
    prompts = _fake_gemini(monkeypatch,
                           [_fenced(BROKEN_DICE), _fenced(VALID_DICE)])

    result = foundry.run({"request": "a plugin that rolls a dice"})

    assert len(prompts) == 2
    assert "previous attempt was rejected" in prompts[1]
    assert (world.plugins / "dice_roller.py").is_file()
    assert cm.get_plugin_enabled("dice_roller") is False
    assert "switched OFF" in result


# ── Broken twice: nothing installed, no config, draft kept ──────────────────

def test_broken_twice_installs_nothing(world, monkeypatch):
    _fake_gemini(monkeypatch, [_fenced(BROKEN_DICE)])

    result = foundry.run({"request": "a plugin that rolls a dice"})

    assert "couldn't make a valid plugin" in result
    assert "Nothing was installed" in result
    assert _plugin_files(world) == []
    assert "dice_roller" not in cm.load_api_keys().get("plugins_enabled", {})
    # The failed draft stays in staging for inspection.
    assert (world.staging / "foundry_draft.py").is_file()


# ── Collision with a built-in: the loader itself refuses ────────────────────

def test_name_collision_with_builtin_not_installed(world, monkeypatch):
    _fake_gemini(monkeypatch, [_fenced(COLLIDING)])

    result = foundry.run({"request": "a plugin that restarts things"})

    assert "collides with a core tool" in result
    assert _plugin_files(world) == []
    assert "restart_program" not in cm.load_api_keys().get(
        "plugins_enabled", {})


# ── No overwrite, either door ───────────────────────────────────────────────

def test_generated_name_with_existing_file_never_overwritten(
        world, monkeypatch):
    # plugins/dice_roller.py exists but declares a different internal
    # name, so only the install-target check can catch this.
    sentinel = ('# sentinel — do not touch\n'
                'PLUGIN = {"name": "other_name", "description": "x", '
                '"parameters": {"type": "OBJECT"}}\n'
                'def run(parameters, player=None, session_memory=None):\n'
                '    return "sentinel"\n')
    target = world.plugins / "dice_roller.py"
    target.write_text(sentinel, encoding="utf-8")
    _fake_gemini(monkeypatch, [_fenced(VALID_DICE)])

    result = foundry.run({"request": "a plugin that rolls a dice"})

    assert target.read_text(encoding="utf-8") == sentinel
    assert "already exists" in result
    assert "dice_roller" not in cm.load_api_keys().get("plugins_enabled", {})


def test_requested_name_with_existing_file_refused_before_generation(
        world, monkeypatch):
    sentinel = "# sentinel\n"
    target = world.plugins / "dice_roller.py"
    target.write_text(sentinel, encoding="utf-8")
    prompts = _fake_gemini(monkeypatch, [_fenced(VALID_DICE)])

    result = foundry.run({"request": "a plugin that rolls a dice",
                          "name": "dice_roller"})

    assert prompts == []   # generation never ran
    assert "never overwrites" in result
    assert target.read_text(encoding="utf-8") == sentinel


# ── Capability scan: spoken, report-only ────────────────────────────────────

def test_capability_flags_spoken_in_summary(world, monkeypatch):
    _fake_gemini(monkeypatch, [_fenced(RISKY)])

    result = foundry.run({"request": "a plugin that pings a site"})

    assert (world.plugins / "net_pinger.py").is_file()
    assert "Heads-up" in result
    assert "can run shell commands" in result
    assert "uses the network" in result


def test_scan_capabilities_labels():
    source = ("import shutil, os\ndata = eval('1')\n"
              "shutil.rmtree('x')\nopen('f', 'w')\n")
    assert foundry._scan_capabilities(source) == [
        "can delete files", "can write files",
        "can run dynamically generated code"]
    assert foundry._scan_capabilities("x = 1\n") == []


# ── The foundry itself, through the real loader on the real tree ────────────

def test_foundry_validates_through_real_loader():
    paths = foundry._paths()   # the real repo tree, unpatched
    core_names = foundry._core_tool_names(paths)
    registry = discover_plugins(paths.plugins, core_names,
                                logger=lambda _msg: None)
    entry = next(e for e in registry.list_for_ui()
                 if e["file"] == "plugin_foundry.py")
    assert entry["valid"], entry["error"]
    assert entry["name"] == "plugin_foundry"
    assert registry.has("plugin_foundry")
