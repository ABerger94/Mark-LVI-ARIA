"""
core/routines.py — named, replayable multi-step automations.

"Watch me do this, save it as X." The user performs a task once, talking
the assistant through it; every tool call is captured; the saved routine
replays the whole sequence on a word, with {{param}} placeholders filled
in per run.

LINEAGE
    A port of ARIA's aria/routines.py — the semantics are hers, kept
    deliberately: the storage shape (one JSON per routine, same fields),
    the name sanitisation, the pack format (an ARIA-exported pack imports
    here unchanged, and vice versa), imported routines landing untrusted,
    and packs never overwriting an existing routine. What changed is the
    machinery underneath, because LVI is built differently:

    CAPTURE — ARIA hooked her dispatch.execute_tool. LVI has exactly one
        execution funnel, JarvisLive._execute_tool in main.py, which the
        Live session and the providers pipeline both feed (the pipeline
        implements the same session interface, so its tool calls arrive
        in the same receive loop). main.py calls maybe_capture() there,
        after execution completes, for every call — inline, action or
        plugin. Steps executed BY a replay are not captured (ARIA skipped
        preauthorized calls for the same reason: a replay must not
        re-record itself while a recording happens to be open).

    REPLAY — steps execute through a bound executor (bind_executor,
        wired in main.py's run() beside confirm.bind) that routes each
        step back through _execute_tool itself, so a replayed step is
        indistinguishable from a spoken one: same gates, same failure
        text, same outcome lines.

    TRUST — ARIA kept a central DESTRUCTIVE_TOOLS list and passed
        preauthorized=False for destructive steps of untrusted routines.
        LVI has no central list: the gate lives inside the destructive
        actions themselves (core/confirm.py, entered per action since
        Phase C). So the mapping is emergent, and simpler: an untrusted
        routine's destructive step parks itself at the gate exactly as a
        live call would, the replay sees the parked result and HALTS —
        later steps may depend on the one waiting for a human. A trusted
        routine replays inside confirm.trusted_scope(), and the gate
        runs the step immediately: trusting the routine was the human
        answering in advance, for a step list describe_routine shows.

TWO DELIBERATE STRENGTHENINGS OVER ARIA
    1. Missing parameters fail BEFORE any step runs. ARIA's substitution
       silently left an unsupplied {{param}} in the argument text, so a
       routine could run halfway with a literal placeholder as a file
       name. Here the placeholders are collected from every step first,
       and a missing one aborts the replay with the names listed.
    2. A failed step HALTS the replay. ARIA's loop recorded each result
       and carried on; a step that did not happen was followed by steps
       written assuming it had. Here the report names the step and the
       reason, and the steps after it do not run. An unverified step is
       NOT a failure (core/outcome.py's distinction) — it is marked in
       the report and the replay continues.
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from core import confirm

MAX_NAME_LEN = 40

# Tools never captured into a routine: the routine tools themselves (a
# routine that contains run_routine would recurse; one that contains
# routine_record_stop would end its own recording) and save_memory —
# memory writes are personal, not procedural. ARIA's set also named her
# approval controls; LVI has no model-callable approval tools to exclude
# (the gate is UI-issued, core/confirm.py), so they are absent here.
_EXCLUDED_TOOLS = frozenset({
    "routine_record_start",
    "routine_record_stop",
    "run_routine",
    "list_routines",
    "delete_routine",
    "describe_routine",
    "trust_routine",
    "untrust_routine",
    "export_routine_pack",
    "import_routine_pack",
    "save_memory",
})


def _base_dir() -> Path:
    # Same rule as core/incidents.py and memory/memory_manager.py: beside
    # the executable when frozen, the repo root when running from source.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Routines live beside her memory store — memory/routines/, one JSON file
# per routine — NOT inside long_term.json: that file is a capped fact
# store with its own trim discipline, and a routine is a program, not a
# fact. Tests monkeypatch this constant to a tmp dir.
ROUTINES_DIR = _base_dir() / "memory" / "routines"

_NAME_BAD_CHARS = re.compile(r"[^a-z0-9_-]")
_PARAM_RE = re.compile(r"\{\{(\w+)\}\}")

# Module-level recording state, as ARIA kept it — one recording at a
# time, because there is one conversation. Guarded by _lock: dispatch
# runs on executor threads.
_lock = threading.Lock()
_RECORDING = {"name": None, "steps": [], "started_ts": None}

# Replays are serialised: two routines stepping on the same desktop at
# once would interleave into nonsense neither recording describes.
_replay_lock = threading.Lock()
_REPLAYING = False

# Bound once at startup by main.py (the confirm.bind pattern): the
# executor routes a step back through JarvisLive._execute_tool, and the
# name source feeds the pre-flight check. Until bound, run_routine
# refuses rather than guessing at a dispatch path of its own.
_executor = None    # fn(name: str, args: dict) -> str
_tool_names = None  # fn() -> set[str]


def bind_executor(execute, tool_names=None) -> None:
    """Wire replay to the live dispatch. Called once from main.py."""
    global _executor, _tool_names
    _executor = execute
    _tool_names = tool_names


def _log(msg: str) -> None:
    print(f"[Routines] {msg}")


def _routines_dir() -> Path:
    return ROUTINES_DIR


def _routine_path(name: str) -> Path:
    return _routines_dir() / f"{name}.json"


def sanitize_name(name) -> str:
    """Lowercase, alnum + `_`/`-` only, max 40 chars. '' if nothing valid."""
    s = _NAME_BAD_CHARS.sub("_", str(name or "").lower().strip())
    s = re.sub(r"_+", "_", s).strip("_-")
    return s[:MAX_NAME_LEN]


def routine_exists(name) -> bool:
    """Is there a saved routine under this (unsanitised) name?"""
    sname = sanitize_name(name)
    return bool(sname) and _routine_path(sname).exists()


def routine_names() -> list:
    """The sanitised names of every saved routine, sorted."""
    d = _routines_dir()
    if not d.is_dir():
        return []
    return sorted(p.stem for p in d.glob("*.json"))


# ─────────────────────────────────────────────────────────────────────────────
#  Recording
# ─────────────────────────────────────────────────────────────────────────────

def is_recording() -> bool:
    with _lock:
        return _RECORDING["name"] is not None


def start_recording(name: str) -> str:
    """Begin capturing tool calls into a new routine."""
    with _lock:
        if _RECORDING["name"] is not None:
            return (f"[Already recording routine '{_RECORDING['name']}'. "
                    f"Call routine_record_stop() first.]")
        sname = sanitize_name(name)
        if not sname:
            return ("[Routine name has no valid characters — use letters, "
                    "numbers, _ or -.]")
        if _routine_path(sname).exists():
            return (f"[Routine '{sname}' already exists. Delete it first "
                    f"(delete_routine) or pick a new name.]")
        _RECORDING["name"] = sname
        _RECORDING["steps"] = []
        _RECORDING["started_ts"] = time.time()
    _log(f"recording started: '{sname}'")
    return (f"Recording routine '{sname}'. Perform the steps now — each "
            f"tool call is captured — then call routine_record_stop().")


def maybe_capture(tool: str, args=None) -> bool:
    """Record one executed call. No-op unless a recording is open.

    Called from main.py's _execute_tool after execution completes — the
    single funnel both session engines feed, so inline tools, actions
    and plugins are all seen. Returns True when the step was captured.
    Calls made BY a replay are never captured: a replay running while a
    recording is open must not pour its steps into the new routine.
    """
    if _REPLAYING:
        return False
    if tool in _EXCLUDED_TOOLS:
        return False
    with _lock:
        if _RECORDING["name"] is None:
            return False
        try:
            _RECORDING["steps"].append({"tool": tool,
                                        "args": dict(args or {})})
        except Exception:
            return False
    return True


def stop_recording() -> str:
    """End capture, persist the routine JSON, return step count + summary."""
    with _lock:
        if _RECORDING["name"] is None:
            return "[Not recording — call routine_record_start(name) first.]"
        name = _RECORDING["name"]
        steps = _RECORDING["steps"]
        _RECORDING["name"] = None
        _RECORDING["steps"] = []
        _RECORDING["started_ts"] = None
    data = {
        "name": name,
        "created": datetime.now(timezone.utc).isoformat(),
        "trusted": False,  # a fresh recording is never born trusted
        "steps": steps,
    }
    try:
        _save_json(_routine_path(name), data)
    except Exception as e:
        return f"[Failed to save routine '{name}': {e}]"
    _log(f"saved '{name}' ({len(steps)} steps)")
    return (f"Saved routine '{name}': {len(steps)} step(s). "
            f"Replay it with run_routine('{name}'). "
            f"Use {{{{param}}}} placeholders in args for parameterized "
            f"replays.")


def _save_json(path: Path, data: dict) -> None:
    """Write JSON atomically — temp file + replace, so a crash mid-save
    leaves the previous routine intact rather than a torn one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def _load_routine(sname: str):
    """(data, None) or (None, error string in ARIA's bracketed form)."""
    path = _routine_path(sname)
    if not path.exists():
        return None, (f"[Routine '{sname}' not found. "
                      f"Call list_routines() to see saved routines.]")
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return None, f"[Routine '{sname}' is unreadable: {e}]"
    return data, None


# ─────────────────────────────────────────────────────────────────────────────
#  Replay
# ─────────────────────────────────────────────────────────────────────────────

def _substitute(value, params: dict):
    """Recursively replace {{param}} placeholders in string values."""
    if isinstance(value, str):
        return _PARAM_RE.sub(lambda m: str(params.get(m.group(1),
                                                      m.group(0))),
                             value)
    if isinstance(value, dict):
        return {k: _substitute(v, params) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute(v, params) for v in value]
    return value


def _placeholders(value) -> set:
    """Every {{param}} name a step's args mention — same traversal as
    _substitute, so what is collected is exactly what would be filled."""
    found = set()
    if isinstance(value, str):
        found.update(_PARAM_RE.findall(value))
    elif isinstance(value, dict):
        for v in value.values():
            found |= _placeholders(v)
    elif isinstance(value, list):
        for v in value:
            found |= _placeholders(v)
    return found


def _preflight_steps(steps, known_tools):
    """The sorted tool names in `steps` that are not in `known_tools`.

    ARIA's self-repair pre-flight, kept: validate every step's tool
    exists before replaying anything — fail fast with a clear message
    instead of dying midway through a multi-step routine. Never raises.
    """
    try:
        known = set(known_tools or [])
        return sorted({str(s.get("tool", "?")) for s in (steps or [])
                       if str(s.get("tool", "?")) not in known})
    except Exception:
        return []


def _step_failed(result: str) -> bool:
    """Did this step's result say, in any of LVI's dialects, 'it did not
    happen'? Outcome lines are the primary dialect; the registry's crash
    and refusal sentences are the others."""
    return (result.startswith("status=failed ")
            or result.startswith("Unknown tool:")
            or result.startswith("Action '") and "is not available" in result
            or (result.startswith("Tool '") and " failed: " in result))


def _step_parked(result: str) -> bool:
    """Did this step stop at the confirmation gate instead of running?

    Primary signal: a confirmation is actually pending on screen now.
    The textual signals cover the cases where nothing could be parked —
    no interface bound — where the gate's refusal sentence is the only
    evidence the step did not run."""
    if "[CONFIRMATION_PENDING]" in result:
        return True
    if result.startswith("I cannot confirm"):
        return True
    try:
        return bool(confirm.pending_title())
    except Exception:
        return False


@dataclass
class ReplayReport:
    """What run_routine established — the facts; the action formats them.

    halted is None only when every step ran. awaiting_confirmation says
    the halt was the gate, not a failure: the step is parked for a human,
    and the routine's remaining steps did not run because they may have
    been written assuming it had."""
    name: str = ""
    trusted: bool = False
    total: int = 0
    completed: int = 0
    lines: list = field(default_factory=list)
    halted: str | None = None
    awaiting_confirmation: bool = False
    missing_params: list = field(default_factory=list)


def run_routine(name: str, params=None) -> ReplayReport:
    """Replay a routine's steps through the bound executor.

    {{param}} placeholders in string arg values are substituted from
    `params`. Untrusted routines' destructive steps park at the gate
    (and halt the replay); trusted routines replay inside
    confirm.trusted_scope(). A failed step halts the replay — see the
    module docstring for why both differ from ARIA's loop.
    """
    global _REPLAYING
    params = params or {}
    report = ReplayReport()
    sname = sanitize_name(name)
    report.name = sname
    if not sname:
        report.halted = "that name has no valid characters"
        return report
    data, err = _load_routine(sname)
    if err:
        # ARIA's bracketed text, unbracketed: the reason is the report.
        report.halted = err.strip("[]")
        return report
    steps = data.get("steps", [])
    report.total = len(steps)
    report.trusted = bool(data.get("trusted", False))
    if not steps:
        report.halted = f"routine '{sname}' has no steps"
        return report
    if _executor is None:
        report.halted = ("no dispatch is bound — the assistant is not "
                         "running, so there is nothing to replay through")
        return report

    # Strengthening 1: every placeholder must have a value BEFORE the
    # first step runs — never discover a missing parameter at step 4
    # with steps 1–3 already done.
    needed = set()
    for step in steps:
        needed |= _placeholders(step.get("args") or {})
    report.missing_params = sorted(needed - set(params))
    if report.missing_params:
        report.halted = ("missing parameter(s): "
                         + ", ".join(report.missing_params))
        return report

    # Pre-flight (ARIA's): every step's tool must still exist.
    if _tool_names is not None:
        try:
            known = set(_tool_names() or [])
        except Exception:
            known = set()
        if known:
            missing_tools = _preflight_steps(steps, known)
            if missing_tools:
                report.halted = ("unknown tool(s): "
                                 + ", ".join(missing_tools)
                                 + " — fix the routine before replaying")
                return report

    if not _replay_lock.acquire(blocking=False):
        report.halted = "another routine is already replaying"
        return report
    try:
        _REPLAYING = True
        for i, step in enumerate(steps, 1):
            tool = str(step.get("tool", "?"))
            args = _substitute(step.get("args", {}), params)
            exc = None
            try:
                if report.trusted:
                    with confirm.trusted_scope():
                        result = _executor(tool, args)
                else:
                    result = _executor(tool, args)
            except Exception as e:  # the executor itself, not the tool
                result, exc = "", e
            if exc is not None:
                report.lines.append(f"{i}. {tool} -> FAILED: {exc}")
                report.halted = f"step {i} ({tool}) raised: {exc}"
                break
            result = str(result or "")
            preview = result.replace("\n", " ")[:200]
            if _step_parked(result):
                report.lines.append(
                    f"{i}. {tool} -> waiting for on-screen confirmation "
                    f"(did not run)")
                report.awaiting_confirmation = True
                report.halted = (
                    f"step {i} ({tool}) is a destructive step in an "
                    f"untrusted routine, so it is waiting at the "
                    f"confirmation gate and did not run; the remaining "
                    f"steps did not run either")
                break
            if _step_failed(result):
                report.lines.append(f"{i}. {tool} -> {preview}")
                report.halted = f"step {i} ({tool}) failed: {preview}"
                break
            report.completed += 1
            mark = " (unverified — continuing)" \
                if result.startswith("status=unverified ") else ""
            report.lines.append(f"{i}. {tool} -> {preview}{mark}")
    finally:
        _REPLAYING = False
        _replay_lock.release()
    _log(f"ran '{sname}' ({report.completed}/{report.total} steps, "
         f"trusted={report.trusted}, halted={report.halted is not None})")
    return report


# ─────────────────────────────────────────────────────────────────────────────
#  The remaining tools' logic
# ─────────────────────────────────────────────────────────────────────────────

def list_routines() -> str:
    d = _routines_dir()
    if not d.is_dir():
        return "[No routines saved yet.]"
    entries = []
    for path in sorted(d.glob("*.json")):
        try:
            with open(path, encoding="utf-8") as f:
                entries.append(json.load(f))
        except Exception:
            continue
    if not entries:
        return "[No routines saved yet.]"
    lines = []
    for data in entries:
        created = str(data.get("created", "?"))[:10]
        trusted = "yes" if data.get("trusted") else "no"
        lines.append(f"{data.get('name', '?')} — "
                     f"{len(data.get('steps', []))} step(s) — "
                     f"created {created} — trusted: {trusted}")
    return f"{len(lines)} routine(s):\n" + "\n".join(lines)


def describe_routine(name: str) -> str:
    sname = sanitize_name(name)
    if not sname:
        return "[Routine name has no valid characters.]"
    data, err = _load_routine(sname)
    if err:
        return err
    steps = data.get("steps", [])
    lines = [f"Routine '{sname}' — {len(steps)} step(s), "
             f"created {str(data.get('created', '?'))[:16]}, "
             f"trusted: {'yes' if data.get('trusted') else 'no'}"]
    for i, step in enumerate(steps, 1):
        argstr = ", ".join(f"{k}={v!r}" for k, v in
                           (step.get("args") or {}).items())
        lines.append(f"  {i}. {step.get('tool', '?')}({argstr})")
    needed = set()
    for step in steps:
        needed |= _placeholders(step.get("args") or {})
    if needed:
        lines.append("Parameters: "
                     + ", ".join("{{" + p + "}}" for p in sorted(needed)))
    return "\n".join(lines)


def delete_routine(name: str) -> str:
    """The removal itself. The ACTION gates it (core/confirm.py) — this
    function is what the gate's callable runs, and what tests call."""
    sname = sanitize_name(name)
    if not sname:
        return "[Routine name has no valid characters.]"
    path = _routine_path(sname)
    if not path.exists():
        return f"[Routine '{sname}' not found.]"
    try:
        path.unlink()
    except Exception as e:
        return f"[Failed to delete routine '{sname}': {e}]"
    _log(f"deleted '{sname}'")
    return f"Deleted routine '{sname}'."


def _save_trusted(sname: str, trusted: bool) -> str:
    data, err = _load_routine(sname)
    if err:
        return err
    data["trusted"] = bool(trusted)
    try:
        _save_json(_routine_path(sname), data)
    except Exception as e:
        return f"[Failed to update routine '{sname}': {e}]"
    _log(f"'{sname}' trusted={trusted}")
    state = "trusted" if trusted else "untrusted"
    extra = (" Destructive steps will now replay without asking."
             if trusted else
             " Destructive steps will stop at the confirmation gate "
             "again.")
    return f"Routine '{sname}' is now {state}.{extra}"


def set_trusted(name: str, trusted: bool) -> str:
    sname = sanitize_name(name)
    if not sname:
        return "[Routine name has no valid characters.]"
    return _save_trusted(sname, trusted)


# ─────────────────────────────────────────────────────────────────────────────
#  Routine packs — a zip: manifest.json + one <name>.json per routine.
#  Shareable: export a pack, send the file, import it on another machine.
#  The format string is ARIA's, so her packs and this build's packs are
#  interchangeable.
# ─────────────────────────────────────────────────────────────────────────────

PACK_FORMAT = "aria-routine-pack"
PACK_VERSION = 1


def _pack_manifest(names):
    return {
        "format": PACK_FORMAT,
        "version": PACK_VERSION,
        "exported": datetime.now(timezone.utc).isoformat(),
        "routines": sorted(names),
    }


def export_pack(names, dest_path: str) -> str:
    """Zip the named routines + manifest to dest_path."""
    wanted = []
    for n in names or []:
        sname = sanitize_name(n)
        if not sname:
            continue
        p = _routine_path(sname)
        if p.exists():
            wanted.append((sname, p))
    if not wanted:
        return "[No matching routines to export.]"
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json",
                   json.dumps(_pack_manifest([s for s, _ in wanted]),
                              indent=2))
        for sname, p in wanted:
            z.write(p, f"{sname}.json")
    return f"Exported {len(wanted)} routine(s) to {dest_path}"


def _valid_routine_data(data) -> bool:
    return (isinstance(data, dict)
            and isinstance(data.get("steps"), list)
            and bool(sanitize_name(data.get("name"))))


def import_pack(pack_path: str) -> str:
    """Validate and import a routine pack. Never overwrites an existing
    routine, and imported routines land UNTRUSTED however the pack's copy
    was flagged — trust is a decision about this machine's copy, made by
    this machine's user."""
    if not pack_path or not Path(pack_path).exists():
        return f"[Pack not found: {pack_path}]"
    try:
        z = zipfile.ZipFile(pack_path)
    except Exception as e:
        return f"[Not a valid pack file: {e}]"
    try:
        try:
            manifest = json.loads(z.read("manifest.json"))
        except KeyError:
            return "[Pack has no manifest.json — refusing to import.]"
        if manifest.get("format") != PACK_FORMAT:
            return "[Not an ARIA routine pack — refusing to import.]"
        imported, skipped, bad = [], [], []
        _routines_dir().mkdir(parents=True, exist_ok=True)
        for info in z.infolist():
            if info.is_dir() or info.filename == "manifest.json":
                continue
            if not info.filename.endswith(".json") or "/" in info.filename:
                bad.append(info.filename)
                continue
            sname = sanitize_name(info.filename[:-5])
            if not sname:
                bad.append(info.filename)
                continue
            try:
                data = json.loads(z.read(info.filename))
            except Exception:
                bad.append(info.filename)
                continue
            if not _valid_routine_data(data):
                bad.append(info.filename)
                continue
            dest = _routine_path(sname)
            if dest.exists():
                skipped.append(sname)
                continue
            data["name"] = sname
            data["trusted"] = False
            _save_json(dest, data)
            imported.append(sname)
    finally:
        z.close()
    parts = []
    if imported:
        parts.append(f"imported: {', '.join(imported)}")
    if skipped:
        parts.append(f"skipped (already exist): {', '.join(skipped)}")
    if bad:
        parts.append(f"rejected: {', '.join(bad)}")
    return "; ".join(parts) or "[Pack contained no routines.]"
