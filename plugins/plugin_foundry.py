"""
Plugin foundry — ARIA writes new plugins on request.

This is the one hand-written plugin in this folder, and it is reviewed code,
so it ships enabled like everything else in the app. What it MAKES is not
reviewed, and that asymmetry is the whole safety model:

  * A machine-made plugin is generated into memory/foundry/ — a staging
    folder outside the code watcher's patrol — and validated there with the
    REAL plugin loader (the same discover_plugins() main.py runs at
    startup, pointed at a temp dir, with the same reserved names). The
    watcher's watched set includes plugins/, so a file that landed there
    unvalidated would be treated as a broken self-edit; nothing crosses
    into plugins/ until the loader itself has pronounced it valid.
  * If validation fails, the model gets ONE repair retry with the loader's
    own error text. A second failure installs nothing and writes no config;
    the draft stays in the staging folder for inspection.
  * Installing is ordered: the new plugin's enabled flag is written False
    through the config funnel FIRST (plugins are enabled by default the
    moment they are discovered — opt-out — so the off-switch has to exist
    before the file can), and only then is the file copied into plugins/.
    Alek enables it himself in ⚙ settings, after the watcher's turn-end
    restart has let discovery list it.
  * A capability scan (AST, report-only, never blocking) names what the
    generated code can do — shell commands, network, file deletion — in
    the spoken summary, so enabling it is an informed decision.

The foundry never overwrites an installed plugin file, and it generates
through core.gemini like every other plugin is told to (see _template.py):
no private model client, so the failover ladder and the configured
provider apply to generation too.
"""
from __future__ import annotations

import ast
import py_compile
import re
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from core.plugin_loader import discover_plugins
from memory.config_manager import save_plugin_enabled

_NAME_RE = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]{0,63}$")

# Generation is the slow step: a whole file of code, at the SMART tier.
_GENERATION_TIMEOUT_MS = 180_000

_MISSING = object()   # sys.modules sentinel: "was not there before"


PLUGIN = {
    "name": "plugin_foundry",
    "description": (
        "Create a brand-new plugin for ARIA from a plain-words description "
        "and install it, switched off, ready to be enabled in ⚙ settings. "
        "Use when the user says 'make a plugin that…', 'write a plugin "
        "to…', 'create a plugin…' or otherwise asks for a new plugin to be "
        "written. Do NOT use for editing an existing plugin, for changing "
        "ARIA's own code (that is edit_own_code / patch_own_code), or for a "
        "one-off task the existing tools already do."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "request": {
                "type": "STRING",
                "description": "What the new plugin should do, in the user's words",
            },
            "name": {
                "type": "STRING",
                "description": "Optional desired plugin name (letters, digits, underscores)",
            },
        },
        "required": ["request"],
    },
    "behavior": "NON_BLOCKING",
    "brief": "Create a new plugin from a plain-words description.",
}


# ── Paths ────────────────────────────────────────────────────────────────────
# Everything hangs off this file's own location (plugins/ sits at the repo
# root), and the whole set is one namespace so tests can point the foundry
# at a sandbox tree with a single monkeypatch.

def _paths() -> SimpleNamespace:
    root = Path(__file__).resolve().parent.parent
    return SimpleNamespace(
        root=root,
        plugins=root / "plugins",
        staging=root / "memory" / "foundry",
        actions=root / "actions",
        main_py=root / "main.py",
        template=root / "plugins" / "_template.py",
    )


# ── The live tool surface (what a new plugin's name must not collide with) ──

def _inline_tool_names(main_py: Path) -> set[str]:
    """Names in main.py's TOOL_DECLARATIONS, read by AST — importing main
    from inside a plugin would re-enter the module that is discovering the
    plugin, so the declarations are parsed, never imported. Only literal
    dict entries are legible this way, which is the form main.py keeps."""
    names: set[str] = set()
    try:
        tree = ast.parse(main_py.read_text(encoding="utf-8"))
    except Exception:
        return names
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "TOOL_DECLARATIONS"
                   for t in node.targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            for elt in node.value.elts:
                if not isinstance(elt, ast.Dict):
                    continue
                for key, val in zip(elt.keys, elt.values):
                    if (isinstance(key, ast.Constant) and key.value == "name"
                            and isinstance(val, ast.Constant)
                            and isinstance(val.value, str)):
                        names.add(val.value)
    return names


def _core_tool_names(paths: SimpleNamespace) -> set[str]:
    """The reserved set main.py builds for discover_plugins: every inline
    tool plus every discovered action, computed the same way."""
    from core.action_loader import discover_actions
    inline = _inline_tool_names(paths.main_py)
    registry = discover_actions(actions_dir=paths.actions,
                                reserved_names=inline,
                                logger=lambda _msg: None)
    return inline | registry.names()


def _existing_plugin_names(paths: SimpleNamespace) -> dict[str, str]:
    """PLUGIN name -> filename for the plugins already installed, read by
    AST for the same reason as the inline names: validation must not import
    the installed set a second time just to learn its names. A plugin whose
    PLUGIN dict is not a literal is illegible here; the file-exists check in
    _stage_and_validate still covers the overwrite case for it."""
    found: dict[str, str] = {}
    try:
        files = sorted(paths.plugins.glob("*.py"), key=lambda p: p.name)
    except OSError:
        return found
    for path in files:
        if path.name.startswith("_"):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        for node in tree.body:
            if not isinstance(node, ast.Assign):
                continue
            if not any(isinstance(t, ast.Name) and t.id == "PLUGIN"
                       for t in node.targets):
                continue
            try:
                meta = ast.literal_eval(node.value)
            except Exception:
                continue
            if isinstance(meta, dict) and isinstance(meta.get("name"), str):
                found[meta["name"]] = path.name
    return found


# ── Generation ───────────────────────────────────────────────────────────────

def _generate(prompt: str) -> str:
    """One model call for a whole plugin file. core.gemini is looked up at
    call time, per the template's one rule — and so tests can substitute
    the engine without touching the network."""
    from core import gemini
    return gemini.text(prompt, tier=gemini.SMART,
                       timeout_ms=_GENERATION_TIMEOUT_MS)


def _build_prompt(request: str, desired_name: Optional[str],
                  template_text: str, error: str = "") -> str:
    parts = [
        "You are writing a new plugin for ARIA, a desktop voice assistant. "
        "Write ONE complete Python plugin file implementing this request:",
        "",
        f"REQUEST: {request}",
    ]
    if desired_name:
        parts += ["",
                  f"The plugin's PLUGIN['name'] must be exactly \"{desired_name}\"."]
    parts += [
        "",
        "You must follow the plugin contract exactly. Here is the official "
        "template — your file must match its shape:",
        "",
        template_text,
        "",
        "HARD RULES (a file breaking any of these is rejected by the plugin "
        "loader):",
        "- PLUGIN['name'] matches ^[a-zA-Z_][a-zA-Z0-9_]{0,63}$ and is unique.",
        "- PLUGIN defines name, description (one or two sentences, explicit "
        "about trigger phrases), and parameters as a dict with \"type\": "
        "\"OBJECT\".",
        "- Define run(parameters, player=None, session_memory=None) -> str. "
        "It returns a short natural-language string that will be spoken "
        "aloud. It must never raise: catch its own errors and return a "
        "spoken error string instead.",
        "- If the plugin needs to ask a model anything, use core.gemini "
        "(from core import gemini) — never build a genai.Client or call a "
        "model API directly.",
        "- Standard library only, unless the request clearly needs an "
        "installed package.",
        "- No import-time side effects: importing the file must only define "
        "PLUGIN and run().",
        "",
        "Return ONLY a single fenced python code block containing the "
        "complete file. No explanation before or after it.",
    ]
    if error:
        parts += [
            "",
            "Your previous attempt was rejected by the plugin loader with "
            "this error:",
            error,
            "Return the complete corrected file as a single fenced python "
            "code block.",
        ]
    return "\n".join(parts)


_FENCE_RE = re.compile(r"```(?:python|py)?[ \t]*\n(.*?)```", re.DOTALL)


def _extract_code(reply: str) -> Optional[str]:
    """The generated plugin source from a model reply: the first fenced
    block that actually holds a plugin, falling back to a bare reply that
    is plainly the file itself. None when there is no usable code."""
    if not reply:
        return None
    blocks = _FENCE_RE.findall(reply)
    for block in blocks:
        if "PLUGIN" in block and "def run" in block:
            return block.strip() + "\n"
    for block in blocks:
        if "PLUGIN" in block:
            return block.strip() + "\n"
    if not blocks and "PLUGIN" in reply and "def run" in reply:
        return reply.strip() + "\n"
    return None


# ── Validation (the real loader, staged outside the watched tree) ──────────

def _stage_and_validate(source: str, stem: str, paths: SimpleNamespace,
                        core_names: set[str],
                        existing_names: dict[str, str]):
    """Write the draft into staging and put it through the genuine article:
    py_compile, then discover_plugins() against a temp dir holding only
    the staged file, with main.py's own reserved-names set — plus the two
    checks a temp dir cannot see (a name already carried by an installed
    plugin under a different filename, and an install target that already
    exists). Returns (ok, name, description, error); the staged draft is
    left on disk either way, for inspection."""
    paths.staging.mkdir(parents=True, exist_ok=True)
    staged = paths.staging / f"{stem}.py"
    staged.write_text(source, encoding="utf-8")

    tmpdir = Path(tempfile.mkdtemp(prefix=".validate-", dir=str(paths.staging)))
    try:
        try:
            py_compile.compile(str(staged),
                               cfile=str(tmpdir / "staged.pyc"),
                               doraise=True)
        except py_compile.PyCompileError as e:
            return False, None, "", f"Python syntax error: {e.msg}"

        shutil.copyfile(staged, tmpdir / staged.name)
        module_key = f"plugins.{staged.stem}"
        previous = sys.modules.get(module_key, _MISSING)
        try:
            registry = discover_plugins(tmpdir, core_names,
                                        logger=lambda _msg: None)
        finally:
            # The staged module was imported under plugins.<stem>; leave
            # sys.modules as validation found it — the copy in the real
            # registry (after a restart) is the one that should live there.
            if previous is _MISSING:
                sys.modules.pop(module_key, None)
            else:
                sys.modules[module_key] = previous

        entry = next((e for e in registry.list_for_ui()
                      if e["file"] == staged.name), None)
        if entry is None:
            return False, None, "", ("The plugin loader did not report on "
                                     "the staged file.")
        if not entry["valid"]:
            return False, None, "", entry["error"]

        name = entry["name"]
        if name in existing_names:
            return False, name, "", (
                f"Name '{name}' is already used by the plugin in "
                f"plugins/{existing_names[name]} — rejected.")
        if (paths.plugins / f"{name}.py").exists():
            return False, name, "", (
                f"plugins/{name}.py already exists — the foundry never "
                f"overwrites an installed plugin; choose a different name.")
        return True, name, entry["description"], ""
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


# ── Capability scan (AST, report-only) ───────────────────────────────────────

_LABEL_ORDER = (
    "can run shell commands",
    "uses the network",
    "can delete files",
    "can write files",
    "can run dynamically generated code",
)
_NETWORK_MODULES = {"socket", "urllib", "requests", "http", "httpx", "aiohttp"}
_DELETE_CALLS = {"os.remove", "os.unlink", "os.rmdir", "shutil.rmtree"}
_DYNAMIC_CALLS = {"eval", "exec", "__import__"}


def _dotted_name(func) -> Optional[str]:
    """'os.system' / 'subprocess.run' / 'eval' from a call's func node."""
    parts = []
    node = func
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return None


def _scan_capabilities(source: str) -> list[str]:
    """Plain-English labels for what the generated code CAN do, from an
    AST walk: shell, network, delete, write, dynamic code. Advisory only —
    the result is spoken in the summary, never used to block an install."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    shell, network, delete, write, dynamic = (
        _LABEL_ORDER[0], _LABEL_ORDER[1], _LABEL_ORDER[2],
        _LABEL_ORDER[3], _LABEL_ORDER[4])
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top == "subprocess":
                    found.add(shell)
                if top in _NETWORK_MODULES:
                    found.add(network)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            if top == "subprocess":
                found.add(shell)
            if top in _NETWORK_MODULES:
                found.add(network)
            if module == "os":
                for alias in node.names:
                    if alias.name in ("system", "popen"):
                        found.add(shell)
                    if alias.name in ("remove", "unlink", "rmdir"):
                        found.add(delete)
            if module == "shutil":
                for alias in node.names:
                    if alias.name == "rmtree":
                        found.add(delete)
        elif isinstance(node, ast.Call):
            dotted = _dotted_name(node.func)
            if dotted:
                if dotted.split(".")[0] == "subprocess" or dotted in (
                        "os.system", "os.popen"):
                    found.add(shell)
                if dotted in _DELETE_CALLS:
                    found.add(delete)
                if dotted in _DYNAMIC_CALLS:
                    found.add(dynamic)
            if isinstance(node.func, ast.Attribute) and node.func.attr in (
                    "write_text", "write_bytes"):
                found.add(write)
            if dotted in ("open", "io.open"):
                mode = None
                if len(node.args) >= 2 and isinstance(
                        node.args[1], ast.Constant):
                    mode = node.args[1].value
                for kw in node.keywords:
                    if kw.arg == "mode" and isinstance(
                            kw.value, ast.Constant):
                        mode = kw.value.value
                if isinstance(mode, str) and any(c in mode for c in "wax+"):
                    found.add(write)
    return [label for label in _LABEL_ORDER if label in found]


# ── Install ──────────────────────────────────────────────────────────────────

def _install(name: str, staged: Path, paths: SimpleNamespace) -> Path:
    """Order is the safety property: the off-switch is written through the
    config funnel BEFORE the file lands in plugins/, because a discovered
    plugin with no flag is enabled by default. Then the copy itself."""
    save_plugin_enabled(name, False)
    target = paths.plugins / f"{name}.py"
    shutil.copyfile(staged, target)
    return target


# ── The tool ─────────────────────────────────────────────────────────────────

def run(parameters: dict, player=None, session_memory=None) -> str:
    """Never raises: every failure path is a spoken string in her voice."""
    try:
        return _run(parameters or {}, player)
    except Exception as e:
        return (f"Sorry — the plugin foundry hit a problem and made "
                f"nothing: {e}")


def _run(parameters: dict, player) -> str:
    paths = _paths()

    request = str(parameters.get("request") or "").strip()
    if not request:
        return ("Tell me what you'd like the plugin to do — a sentence is "
                "enough — and I'll write it.")

    desired = str(parameters.get("name") or "").strip() or None
    if desired is not None and not _NAME_RE.match(desired):
        return (f"\"{desired}\" can't be a plugin name — plugin names use "
                f"only letters, digits and underscores, and start with a "
                f"letter or underscore. Give me another name, or none and "
                f"I'll pick one.")
    if desired is not None and (paths.plugins / f"{desired}.py").exists():
        return (f"There's already a plugin file called {desired}.py, and "
                f"the foundry never overwrites an installed plugin. Ask me "
                f"for a different name, or remove that one first.")

    try:
        template_text = paths.template.read_text(encoding="utf-8")
    except OSError:
        return ("I can't find my plugin template, so I have no contract "
                "to write against. Nothing was made.")

    core_names = _core_tool_names(paths)
    existing_names = _existing_plugin_names(paths)
    stem = desired or "foundry_draft"

    error = ""
    name = description = None
    staged = paths.staging / f"{stem}.py"
    source = ""
    for _attempt in range(2):
        prompt = _build_prompt(request, desired, template_text, error)
        code = _extract_code(_generate(prompt))
        if code is None:
            error = ("The model did not return a plugin file in a fenced "
                     "python code block.")
            continue
        source = code
        ok, name, description, error = _stage_and_validate(
            code, stem, paths, core_names, existing_names)
        if ok:
            break
    else:
        return (f"I couldn't make a valid plugin from that, even after a "
                f"second try — the loader said: {error} The draft is saved "
                f"in my foundry folder (memory/foundry/{stem}.py) if you "
                f"want to look at it. Nothing was installed.")

    flags = _scan_capabilities(source)
    _install(name, staged, paths)

    if player is not None:
        try:
            player.write_log(
                f"Plugin foundry: created '{name}' — installed disabled.")
        except Exception:
            pass

    summary = (f"Done — I created a new plugin, '{name}': "
               f"{' '.join(description.split())[:240]} "
               f"It's installed but switched OFF. I'll be restarting "
               f"shortly; after that you'll find it in ⚙ settings, under "
               f"plugins, where you can switch it on.")
    if flags:
        summary += f" Heads-up: it {'; it '.join(flags)}."
    return summary
