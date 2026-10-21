"""
core/selfedit.py — she can change her own code, and the change is
validated before it is allowed to be true.

THE SHAPE OF THE THING (ported from ARIA's brain.py + tools/selfedit.py,
2026-10-21 fix package — the flow Alek watched work)

    Two tools (actions/edit_own_code.py, actions/patch_own_code.py) write
    to an allowlist that is EXACTLY the watched set below. The tools
    themselves validate nothing and restart nothing. After each turn, the
    watcher re-hashes the watched set; anything that changed — through
    the tools OR through her general file tools, which is the entire
    point of watching hashes instead of watching tool calls — is
    compiled, smoke-imported, announced in her voice, and loaded by a
    restart. Code that fails validation is restored from git (the broken
    copy kept beside it as <file>.broken-<epoch>), the restore is
    announced, and the restart loads the restored tree. Code whose
    restore FAILS is announced as broken and nothing restarts into it.

THE WATCHED SET — what "her own code" means in this tree

    main.py, ui.py, every .py under core/, actions/, memory/,
    providers/, dashboard/, plugins/ — plus core/prompt.txt, the
    persona file (ARIA's SOUL equivalent; a persona edit deserves the
    same announce-and-restart as a code edit).

    Deliberately NOT watched, for ARIA's stated reason: tests/, tools/
    and setup.py are repo tooling, not the running assistant. A broken
    test file must never force a restore of her source or a restart of
    her process, and setup.py is an installer script — smoke-importing
    it would RUN it.

    Deleted files are skipped, as they were in ARIA: the running
    process still holds the module in memory, and restarting into a
    tree with a missing module would just crash the new process at
    import.

THE RESTART ITSELF

    LVI has no app-restart lifecycle to reuse — the only exits are
    shutdown_jarvis (os._exit) and the PC restart in computer_settings
    (confirm-gated, and it restarts the COMPUTER). So the watcher takes
    a `restart` callback; main.py supplies one that mirrors the shutdown
    path's shape (save the session summary, relaunch this same script,
    exit). It is deliberately NOT routed through core/confirm.py: the
    announcement IS the notice, exactly as ARIA's self-restart was
    announced-not-gated.
"""

from __future__ import annotations

import hashlib
import os
import py_compile
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

try:
    from core import incidents
except Exception:  # pragma: no cover — incidents must never block an edit
    incidents = None


def _base_dir() -> Path:
    # Same rule as memory/memory_manager.get_base_dir and
    # core/incidents._base_dir: beside the executable when frozen, the
    # repo root when running from source.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# The persona file — this tree's SOUL. Phase A put her identity text here.
PROMPT_PATH_PARTS = ("core", "prompt.txt")

_PACKAGE_DIRS = ("core", "actions", "memory", "providers", "dashboard",
                 "plugins")
_ROOT_SCRIPTS = ("main.py", "ui.py")
# Top-level names that are THIS app, for the missing-dependency test in
# the smoke import: a missing one of these is a broken edit; a missing
# anything else is an environment gap (ARIA's rule, same reasoning).
_OWN_TOP_LEVEL = set(_PACKAGE_DIRS) | {"main", "ui"}

_RESTART_NOTE = (
    " The post-turn restart check will validate the change, "
    "announce it, and restart me to load it — or restore the last "
    "committed version from git if the new code is broken.")
# The action wrappers surface the same note to the model as a fact.
RESTART_NOTE = _RESTART_NOTE.strip()


# ── The watched set ─────────────────────────────────────────────────────────

def watched_source_files(root: Optional[Path] = None) -> List[Path]:
    """Every file whose change means "her code changed". See the header."""
    root = Path(root) if root else _base_dir()
    files: List[Path] = []
    for name in _ROOT_SCRIPTS:
        p = root / name
        if p.is_file():
            files.append(p)
    for dirname in _PACKAGE_DIRS:
        d = root / dirname
        if d.is_dir():
            for p in sorted(d.rglob("*.py")):
                if "__pycache__" not in p.parts:
                    files.append(p)
    prompt = root.joinpath(*PROMPT_PATH_PARTS)
    if prompt.is_file():
        files.append(prompt)
    return sorted(files)


def _file_sha256(path) -> Optional[str]:
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


def snapshot_hashes(root: Optional[Path] = None) -> Dict[str, Optional[str]]:
    return {str(p): _file_sha256(p) for p in watched_source_files(root)}


# ── The edit tools ──────────────────────────────────────────────────────────

def _resolve_target(path: str, root: Optional[Path] = None):
    """(absolute_path, None) when the target is allowlisted, else
    (None, refusal_reason). The allowlist IS the watched set — a file
    she may edit is a file whose edit the watcher will validate.
    Relative paths resolve against the repo root."""
    raw = (path or "").strip()
    if not raw:
        return None, "no path given"
    root = Path(root) if root else _base_dir()
    repo = os.path.realpath(root)
    norm = raw.replace("\\", "/")
    if os.path.isabs(raw) or (len(norm) > 1 and norm[1] == ":"):
        cand = os.path.realpath(raw)
    else:
        cand = os.path.realpath(os.path.join(repo, *norm.split("/")))
    if os.path.commonpath([cand, repo]) != repo:
        return None, f"'{path}' resolves outside my source tree"
    prompt = os.path.realpath(os.path.join(repo, *PROMPT_PATH_PARTS))
    if cand == prompt:
        return cand, None
    rel = os.path.relpath(cand, repo).replace(os.sep, "/")
    if rel in _ROOT_SCRIPTS:
        return cand, None
    parts = rel.split("/")
    if len(parts) > 1 and parts[0] in _PACKAGE_DIRS and cand.endswith(".py"):
        return cand, None
    return None, (f"'{path}' is not one of my editable files — I can "
                  f"only edit main.py, ui.py, .py files inside "
                  f"{', '.join(_PACKAGE_DIRS)}, or core/prompt.txt")


def _atomic_write(path: str, content: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = f"{path}.tmp-selfedit"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(content)
    os.replace(tmp, path)


def _rel(path: str, root: Optional[Path] = None) -> str:
    root = Path(root) if root else _base_dir()
    return os.path.relpath(path, root)


def edit_own_code(path: str, content: str,
                  root: Optional[Path] = None) -> Tuple[bool, str]:
    """Rewrite one of her own source files in full.
    Returns (ok, message) — the action wrapper renders the outcome."""
    target, refusal = _resolve_target(path, root)
    if refusal:
        return False, f"Self-edit refused: {refusal}."
    try:
        _atomic_write(target, content or "")
    except Exception as e:
        return False, f"Self-edit failed: {type(e).__name__}: {e}"
    print(f"[SelfEdit] rewrote {_rel(target, root)} "
          f"({len(content or '')} chars)")
    return True, (f"Rewrote {_rel(target, root)} "
                  f"({len(content or '')} characters)." + _RESTART_NOTE)


def patch_own_code(path: str, old_text: str, new_text: str,
                   root: Optional[Path] = None) -> Tuple[bool, str]:
    """Replace exactly one exact-match occurrence in one of her own
    source files. Refuses when old_text is absent or matches more than
    one place — a partial or ambiguous self-patch is how a working file
    becomes a broken one. (ARIA's semantics, verbatim.)"""
    target, refusal = _resolve_target(path, root)
    if refusal:
        return False, f"Self-edit refused: {refusal}."
    if not old_text:
        return False, "Self-edit failed: old_text is empty — nothing to match."
    try:
        with open(target, "r", encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        return False, (f"Self-edit failed: {_rel(target, root)} does not "
                       f"exist — use edit_own_code to create it.")
    except Exception as e:
        return False, f"Self-edit failed: {type(e).__name__}: {e}"
    n = current.count(old_text)
    if n == 0:
        return False, ("Self-edit failed: old_text not found in "
                       f"{_rel(target, root)} — no change made.")
    if n > 1:
        return False, (f"Self-edit failed: old_text matches {n} places in "
                       f"{_rel(target, root)} — include more surrounding "
                       f"context so it matches exactly one. No change made.")
    try:
        _atomic_write(target, current.replace(old_text, new_text, 1))
    except Exception as e:
        return False, f"Self-edit failed: {type(e).__name__}: {e}"
    print(f"[SelfEdit] patched {_rel(target, root)} "
          f"({len(old_text)} -> {len(new_text or '')} chars)")
    return True, (f"Patched {_rel(target, root)}: replaced {len(old_text)} "
                  f"characters with {len(new_text or '')}." + _RESTART_NOTE)


# ── Validation (ARIA's compile + smoke-import, adapted) ────────────────────

def _restore_last_committed(path: str, root: Path) -> Optional[str]:
    """Restore a broken source file from git.

    The broken version is preserved as <path>.broken-<epoch> first, then
    `git checkout -- <path>` restores the last committed copy. Returns
    the backup path when the restored file compiles cleanly, else None
    (the broken file is left in place, exactly as before).
    Never raises. (ARIA's, unchanged in shape.)
    """
    try:
        backup = f"{path}.broken-{int(time.time())}"
        shutil.copy2(path, backup)
        r = subprocess.run(
            ["git", "checkout", "--", path],
            cwd=str(root), capture_output=True, timeout=30)
        if r.returncode != 0:
            return None
        if path.endswith(".py"):
            py_compile.compile(path, doraise=True)
        return backup
    except Exception as e:
        print(f"[SelfEdit] auto-restore failed for "
              f"{os.path.basename(path)}: {e}")
        return None


def _missing_third_party_dep(stderr_tail: str) -> bool:
    """True when the import failed ONLY because an EXTERNAL module is
    missing — an environment gap, not a broken edit. A missing module
    from this app's own packages is OUR code — always a genuine failure.
    (ARIA's rule; her `aria.*` test becomes the _OWN_TOP_LEVEL set.)"""
    m = re.search(r"ModuleNotFoundError: No module named '([^']+)'",
                  stderr_tail)
    if not m:
        return False
    return m.group(1).split(".")[0] not in _OWN_TOP_LEVEL


def _smoke_import_changed(changed: List[str], root: Path) -> list:
    """Import-smoke-test changed package modules in a subprocess.

    py_compile passing is NOT enough: a file can parse yet fail at
    import. Each changed module under one of the package dirs is
    imported in a fresh interpreter with cwd=repo root — the same
    import the restart would do. Parent packages are stubbed (empty
    __init__, real __path__) so the test exercises THE MODULE's own
    imports rather than package __init__ side effects. The root
    scripts (main.py, ui.py) are compile-checked only: they are entry
    points, not importable modules. Failures caused solely by a missing
    third-party dependency are skipped (unverifiable in this env, not
    a broken edit). Returns [(path, stderr_tail)]. Never raises.
    """
    code = (
        "import sys, types, os, importlib\n"
        "mod = sys.argv[1]\n"
        "repo = os.getcwd()\n"
        "parts = mod.split('.')\n"
        "for i in range(1, len(parts)):\n"
        "    name = '.'.join(parts[:i])\n"
        "    pkg = types.ModuleType(name)\n"
        "    pkg.__path__ = [os.path.join(repo, *parts[:i])]\n"
        "    sys.modules[name] = pkg\n"
        "importlib.import_module(mod)\n"
    )
    failed = []
    for p in changed:
        if not (p.endswith(".py") and os.path.isfile(p)):
            continue
        rel = os.path.relpath(p, str(root)).replace(os.sep, "/")
        parts = rel.split("/")
        if len(parts) < 2 or parts[0] not in _PACKAGE_DIRS:
            continue   # root script — compile was its whole test
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        try:
            r = subprocess.run(
                [sys.executable, "-c", code, mod],
                cwd=str(root), capture_output=True, timeout=90)
            if r.returncode != 0:
                err = r.stderr.decode("utf-8", "replace").strip().splitlines()
                tail = err[-1][-300:] if err else "import failed"
                if _missing_third_party_dep(tail):
                    continue
                failed.append((p, tail))
        except Exception as e:
            failed.append((p, str(e)[:200]))
    return failed


# ── The watcher ─────────────────────────────────────────────────────────────

class SelfEditWatcher:
    """Boot-snapshot hash watcher. main.py builds one at startup and
    calls check() after every completed turn (from the one receive loop
    both engines share). Callbacks keep this class testable and keep
    main.py's lifecycle (speak / relaunch) where it belongs:

        announce(text)  — say it in her voice (main.py's speak)
        restart()       — perform the relaunch (main.py supplies it)
        speech_idle()   — True when nothing is being spoken, so the
                          restart waits for her to finish the sentence
                          (ARIA waited on her speech queues; this is
                          the same wait against LVI's speaking flag)
    """

    def __init__(self,
                 root: Optional[Path] = None,
                 announce: Optional[Callable[[str], None]] = None,
                 restart: Optional[Callable[[], None]] = None,
                 speech_idle: Optional[Callable[[], bool]] = None):
        self.root = Path(root) if root else _base_dir()
        self._announce = announce or (lambda text: None)
        self._restart = restart or (lambda: None)
        self._speech_idle = speech_idle
        self._boot = snapshot_hashes(self.root)
        self._lock = threading.Lock()
        self._checking = False
        self._restart_pending = False
        self._restart_timer: Optional[threading.Timer] = None

    # -- internals -----------------------------------------------------------

    def _record(self, kind: str, message: str) -> None:
        if incidents is not None:
            incidents.record("selfedit", kind, message)

    def _arm_restart(self) -> None:
        self._restart_pending = True

        def _waiter():
            if self._speech_idle is not None:
                deadline = time.time() + 120
                while time.time() < deadline:
                    try:
                        if self._speech_idle():
                            break
                    except Exception:
                        break
                    time.sleep(0.5)
            time.sleep(2.0)
            try:
                self._restart()
            except Exception as e:
                print(f"[SelfEdit] restart callback failed: {e}")

        self._restart_timer = threading.Timer(0.5, _waiter)
        self._restart_timer.daemon = True
        self._restart_timer.start()

    # -- the check ------------------------------------------------------------

    def check(self) -> str:
        """One post-turn check. Returns a short status string:
        "clean" | "restart_armed" | "restored_restart_armed" |
        "restore_failed" | "busy" | "restart_pending". Never raises —
        it runs inside the conversation's receive loop."""
        with self._lock:
            if self._checking:
                return "busy"
            if self._restart_pending:
                return "restart_pending"
            self._checking = True
        try:
            return self._check_inner()
        except Exception as e:
            print(f"[SelfEdit] check failed: {e}")
            return "clean"
        finally:
            with self._lock:
                self._checking = False

    def _check_inner(self) -> str:
        cur = snapshot_hashes(self.root)
        # Deleted files are skipped (absent from cur): the running
        # process still holds the module in memory. Restarting into a
        # tree with a missing module would just crash the new process.
        changed = [p for p in cur if cur[p] != self._boot.get(p)]
        if not changed:
            return "clean"

        bad = []  # (path, reason) for files that would crash the restart
        for p in changed:
            if p.endswith(".py") and os.path.isfile(p):
                try:
                    py_compile.compile(p, doraise=True)
                except Exception as e:
                    bad.append((p, f"syntax error: {e}"))
        if not bad:
            # Parses clean but may still fail at import (wrong module,
            # bad dependency). Catch it here instead of crash-looping
            # the restart.
            for p, err in _smoke_import_changed(changed, self.root):
                bad.append((p, f"import failed: {err}"))

        for p, reason in bad:
            print(f"[SelfEdit] restart blocked: {reason} in "
                  f"{os.path.basename(p)}")
            restored = _restore_last_committed(p, self.root)
            if not restored:
                self._record(
                    "self_edit_restore_failed",
                    f"{os.path.basename(p)}: {reason}; restore failed")
                self._announce(
                    f"My code changed but the new version is broken "
                    f"({reason}).")
                return "restore_failed"
            self._record(
                "self_edit_broken",
                f"{os.path.basename(p)}: {reason}; restored from git, "
                f"broken copy kept as {os.path.basename(restored)}")
            print(f"[SelfEdit] restored {os.path.basename(p)} from git, "
                  f"broken copy kept as {restored}")

        if bad:
            restored_names = ", ".join(
                os.path.basename(p) for p, _ in bad)
            self._announce(
                f"My code changed but the new version was broken — "
                f"I restored the last working {restored_names} and "
                f"restarting.")
            self._announce(
                "My code changed — restarting now to load the new version.")
            self._arm_restart()
            return "restored_restart_armed"

        self._announce(
            "My code changed — restarting now to load the new version.")
        print("[SelfEdit] restart armed.")
        self._arm_restart()
        return "restart_armed"
