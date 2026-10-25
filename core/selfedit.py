"""
core/selfedit.py — she can change her own code, and the change is
validated before it is allowed to be true.

THE SHAPE OF THE THING (ported from ARIA's brain.py + tools/selfedit.py,
2026-10-21 fix package — the flow Alek watched work)

    Three tools (actions/edit_own_code.py, actions/patch_own_code.py,
    actions/replace_lines_own_code.py) write
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
    (confirm-gated, and it restarts the COMPUTER). The turn-end
    watcher that drives the restart now lives in core/codewatch.py
    (CodeWatcher): it composes the validation and restore machinery
    below, and on a passing change main.py relaunches this same
    script and exits — the shutdown path's shape (save the session
    summary, relaunch, exit). The restart is deliberately NOT
    routed through core/confirm.py: the
    announcement IS the notice, exactly as ARIA's self-restart was
    announced-not-gated.

READ BEFORE EDIT, IMPORT BEFORE RESTART (Alek's rules, 2026-10-22)

    Two rules, both enforced here rather than merely taught:

    READ BEFORE EDITING — TWO TIERS (Alek's rules, 2026-10-22;
    region scope added by his ruling, 2026-10-25). read_own_code()
    reads a watched file in explicit line ranges and records
    coverage — per file, keyed to the file's current sha256, the
    union of ranges read this process. What the gate requires
    depends on the edit:

    A FULL REWRITE (edit_own_code) requires coverage of every
    line at the current hash: a rewrite re-emits the whole file,
    so the whole file must have been read. The rule exists
    because the general read_file truncates at 4,000 chars with
    no offset: through it, a file like ui.py (~7,700 lines)
    could never be read in full, so she rewrote it blind. Lines
    received through read_file DO count toward coverage
    (file_controller notes them, truncation respected);
    read_own_code is how she finishes the job.

    A PATCH OR LINE-REPLACE (patch_own_code,
    replace_lines_own_code) requires only the region being
    changed plus _PATCH_CONTEXT_LINES (60) on each side. The
    full-file rule made a one-line fix to ui.py (9,727 lines)
    cost 5–25 read calls first; the intent — she must have read
    what she is about to change, at the version she is about to
    change it in — is preserved at region scope. Region scope
    is safe because a patch is anchored to exact text found
    inside the read region and a replace names explicit line
    numbers inside it; neither can touch a line beyond the
    region and its margin. The refusal names the exact unread
    lines and the read that fixes it.

    COVERAGE CARRY-OVER. The hash binding exists so she never
    edits a version she hasn't read. When THIS module performs
    a successful patch, replace or rewrite, the new version's
    content is known exactly, so coverage translates to the new
    hash instead of resetting: lines before the change keep
    their numbers, the changed lines are covered (she authored
    them), lines after shift by the size change, and a rewrite
    covers every line. A change made ANYWHERE ELSE — her
    general file tools, the user, git — changes the hash with
    no carry-over and resets the requirement: she re-reads the
    version she is about to edit, never an ancestor of it.
    Coverage lives in-process (session) only — a restart means
    re-reading before the next edit, which is the point.

    IMPORT-CHECK EVERY CHANGED FILE. Every watched .py — root
    scripts included — is smoke-imported in a subprocess before a
    restart may load it; compile-only is never sufficient. ui.py is
    a library to main.py (`from ui import JarvisUI`), and main.py's
    `if __name__ == "__main__":` guard makes importing it execute
    its module level without launching the app. The 2026-10-22
    break is the case this closes: an invented
    `class HUDReactorCore(ui.View)` COMPILES, so the old
    compile-only treatment of root scripts passed it and the
    watcher restarted her into the AttributeError. core/prompt.txt
    is not importable Python and stays hash/existence-checked.
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
from typing import Dict, List, Optional, Tuple


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

# How much read context a patch/replace must have around the lines it
# changes (Alek's ruling, 2026-10-25): the region gate covers the
# changed span plus this many lines on each side.
_PATCH_CONTEXT_LINES = 60
# The largest span one replace_lines_own_code call may rewrite —
# larger changes split into smaller replaces.
_MAX_REPLACE_SPAN = 400


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


# ── Read-before-edit coverage ─────────────────────────────────────────────
#
# The registry the gate consults: per file, the union of line ranges read
# at the file's CURRENT sha256. Hash binding is the honesty — coverage
# recorded against any other bytes counts for nothing, so an outside
# change resets the requirement to read the new version; an edit made
# through THIS module carries its coverage over instead (see
# _carry_coverage). In-process only; see the module header for why that
# is a feature. All functions here never raise into a read or an edit.

_coverage_lock = threading.Lock()
_coverage: Dict[str, Tuple[str, set]] = {}   # abspath -> (sha256, {lines})


def _line_count(text: str) -> int:
    """Lines in a file's text, the way an editor counts them: a final
    newline ends the last line rather than starting a phantom one."""
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def note_read_coverage(path: str, start_line: int, end_line: int) -> None:
    """Record that lines [start_line, end_line] (1-based, inclusive) of
    the file at `path` were read at its current hash."""
    try:
        sha = _file_sha256(path)
        if sha is None:
            return
        start = max(1, int(start_line))
        end = int(end_line)
        with _coverage_lock:
            entry = _coverage.get(path)
            if entry is None or entry[0] != sha:
                entry = (sha, set())
                _coverage[path] = entry
            if end >= start:
                entry[1].update(range(start, end + 1))
    except Exception:
        pass


def note_read_text(path: str, text: str,
                   root: Optional[Path] = None,
                   start_line: int = 1) -> None:
    """Record coverage for a read that returned `text` beginning at
    `start_line` — the shape of the general file reader (actions/
    file_controller.read_file): the whole file in one return, or
    one window of it when paged by character offset. Only lines
    actually present in the returned text count. Watched files
    only; anything else is ignored."""
    try:
        target, refusal = _resolve_target(str(path), root)
        if refusal or not target or not text:
            return
        first = max(1, int(start_line))
        note_read_coverage(target, first,
                           first + _line_count(text) - 1)
    except Exception:
        pass


def _unread_lines(path: str) -> Optional[Tuple[int, int]]:
    """(total_lines, unread_count) for the file at its CURRENT hash,
    or None when the file cannot be read at all."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            total = _line_count(f.read())
    except Exception:
        return None
    sha = _file_sha256(path)
    with _coverage_lock:
        entry = _coverage.get(path)
        covered = entry[1] if entry and entry[0] == sha else set()
    unread = sum(1 for n in range(1, total + 1) if n not in covered)
    return total, unread


def _covered_lines(path: str) -> set:
    """The lines of the file at `path` covered at its CURRENT hash —
    empty when nothing is covered at this version."""
    sha = _file_sha256(path)
    with _coverage_lock:
        entry = _coverage.get(path)
        if entry and entry[0] == sha:
            return set(entry[1])
    return set()


def _region_gate_refusal(target: str, root: Optional[Path],
                         first: int, last: int) -> Optional[str]:
    """The region gate for patch/replace: a refusal when any line of
    [first, last] — the changed span plus _PATCH_CONTEXT_LINES on
    each side, clamped to the file by the caller — is uncovered at
    the file's CURRENT hash, else None. The refusal names the exact
    unread lines inside the region and the one read_own_code call
    that covers the region."""
    try:
        with open(target, "r", encoding="utf-8") as f:
            total = _line_count(f.read())
    except Exception:
        return None   # unreadable: the edit's own I/O fails honestly
    covered = _covered_lines(target)
    unread = [n for n in range(first, last + 1) if n not in covered]
    if not unread:
        return None
    ranges = []
    run_start = prev = unread[0]
    for n in unread[1:]:
        if n == prev + 1:
            prev = n
            continue
        ranges.append((run_start, prev))
        run_start = prev = n
    ranges.append((run_start, prev))
    named = ", ".join(f"{a}–{b}" if a != b else str(a)
                      for a, b in ranges)
    return (f"Self-edit refused: you have not read the region you "
            f"are changing in {_rel(target, root)} at its current "
            f"version — {len(unread)} of its {total} lines are "
            f"unread, including region lines {named}. The whole "
            f"file is no longer required for patch_own_code or "
            f"replace_lines_own_code — only the region being "
            f"changed and {_PATCH_CONTEXT_LINES} lines of context "
            f"around it — but that region must be read first: "
            f"read_own_code with offset={first}, "
            f"limit={last - first + 1}. (A full rewrite with "
            f"edit_own_code still requires the whole file.)")


def _carry_coverage(path: str, pre_sha: Optional[str], old_first: int,
                    old_last: int, new_span_lines: int) -> None:
    """Translate read coverage across an edit THIS module just made.

    The edit replaced old lines [old_first, old_last] with
    `new_span_lines` new lines. The new content is known exactly
    (this module wrote it), so coverage recorded at the pre-edit
    hash becomes coverage at the post-edit hash: lines before the
    span keep their numbers, the new span is covered (she authored
    it), lines after the span shift by the size change. Runs only
    when a coverage entry existed at the pre-edit hash; never
    invents coverage. Never raises."""
    try:
        if not pre_sha:
            return
        new_sha = _file_sha256(path)
        if new_sha is None:
            return
        delta = new_span_lines - (old_last - old_first + 1)
        with _coverage_lock:
            entry = _coverage.get(path)
            if entry is None or entry[0] != pre_sha:
                return
            moved = set()
            for n in entry[1]:
                if n < old_first:
                    moved.add(n)
                elif n > old_last:
                    moved.add(n + delta)
            moved.update(range(old_first,
                               old_first + new_span_lines))
            _coverage[path] = (new_sha, moved)
    except Exception:
        pass


def _cover_all_lines(path: str, total: int) -> None:
    """Record full coverage of the file at its CURRENT hash — used
    after a successful edit_own_code rewrite or creation: she
    authored every line of the new version, so every line of it
    counts as read. Never raises."""
    try:
        sha = _file_sha256(path)
        if sha is None:
            return
        with _coverage_lock:
            _coverage[path] = (sha, set(range(1, total + 1)))
    except Exception:
        pass


def _read_gate_refusal(target: str, root: Optional[Path]) -> Optional[str]:
    """The read-before-edit gate: a refusal when the file at `target`
    has not been read in full at its current version, else None. A
    file that does not exist yet has nothing to read — creating it is
    edit_own_code's stated job, so it passes."""
    if not os.path.isfile(target):
        return None
    state = _unread_lines(target)
    if state is None:
        return None   # unreadable: the edit's own I/O fails honestly
    total, unread = state
    if unread == 0:
        return None
    return (f"Self-edit refused: you have not read {_rel(target, root)} "
            f"in full — {unread} of its {total} lines are unread at "
            f"its current version. Read the whole file first with "
            f"read_own_code (it reads in line ranges and reports the "
            f"total and what remains), then edit. Coverage is tied to "
            f"the file's current content: any change to it — including "
            f"your own last edit — means reading the new version "
            f"before the next edit.")


def read_own_code(path: str, offset: int = 1, limit: int = 400,
                  root: Optional[Path] = None) -> Tuple[bool, str]:
    """Read a chunk of one of her own source files, in line ranges —
    the reader the read-before-edit gate counts. Returns (ok, text):
    a header naming the range and the running coverage, then the
    lines, numbered. The general read_file cannot do this job: it
    truncates at a character cap with no offset, so a large file
    (ui.py is ~7,700 lines) could never be read in full through it.
    """
    target, refusal = _resolve_target(path, root)
    if refusal:
        return False, f"Read refused: {refusal}."
    try:
        with open(target, "r", encoding="utf-8") as f:
            lines = f.read().split("\n")
    except FileNotFoundError:
        return False, (f"Read failed: {_rel(target, root)} does not "
                       f"exist.")
    except Exception as e:
        return False, f"Read failed: {type(e).__name__}: {e}"
    # A trailing newline leaves a phantom empty element; the gate
    # counts lines the editor's way, so drop it — reads and the gate
    # must count the same lines.
    if lines and lines[-1] == "":
        lines.pop()
    total = len(lines)
    if total == 0:
        note_read_coverage(target, 1, 0)
        return True, f"{_rel(target, root)} is empty (0 lines) — fully read."
    start = min(max(1, int(offset or 1)), total)
    lim = min(max(1, int(limit or 400)), 2000)
    end = min(total, start + lim - 1)
    note_read_coverage(target, start, end)
    state = _unread_lines(target)
    unread = state[1] if state else 0
    chunk = "\n".join(f"{n}: {lines[n - 1]}"
                      for n in range(start, end + 1))
    if unread == 0:
        status = "fully read — you may edit this file"
    else:
        status = (f"{total - unread} of {total} lines read; {unread} "
                  f"still unread — keep reading ranges until none "
                  f"remain before editing")
    return True, (f"--- {_rel(target, root)} lines {start}–{end} of "
                  f"{total} ({status}) ---\n{chunk}")


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
    gate = _read_gate_refusal(target, root)
    if gate:
        return False, gate
    try:
        _atomic_write(target, content or "")
    except Exception as e:
        return False, f"Self-edit failed: {type(e).__name__}: {e}"
    _cover_all_lines(target, _line_count(content or ""))
    print(f"[SelfEdit] rewrote {_rel(target, root)} "
          f"({len(content or '')} chars)")
    return True, (f"Rewrote {_rel(target, root)} "
                  f"({len(content or '')} characters)." + _RESTART_NOTE)


def patch_own_code(path: str, old_text: str, new_text: str,
                   root: Optional[Path] = None) -> Tuple[bool, str]:
    """Replace exactly one exact-match occurrence in one of her own
    source files. Refuses when old_text is absent or matches more than
    one place — a partial or ambiguous self-patch is how a working file
    becomes a broken one. (ARIA's semantics, verbatim.) The read gate
    is region-scoped (Alek's ruling, 2026-10-25): the match's lines
    plus _PATCH_CONTEXT_LINES around them must have been read at the
    current version — not the whole file."""
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
    start = current.find(old_text)
    first = current.count("\n", 0, start) + 1
    last = first + _line_count(old_text) - 1
    total = _line_count(current)
    gate = _region_gate_refusal(
        target, root, max(1, first - _PATCH_CONTEXT_LINES),
        min(total, last + _PATCH_CONTEXT_LINES))
    if gate:
        return False, gate
    pre_sha = _file_sha256(target)
    try:
        _atomic_write(target, current.replace(old_text, new_text, 1))
    except Exception as e:
        return False, f"Self-edit failed: {type(e).__name__}: {e}"
    _carry_coverage(target, pre_sha, first, last,
                    _line_count(new_text or ""))
    print(f"[SelfEdit] patched {_rel(target, root)} "
          f"({len(old_text)} -> {len(new_text or '')} chars)")
    return True, (f"Patched {_rel(target, root)}: replaced {len(old_text)} "
                  f"characters with {len(new_text or '')}." + _RESTART_NOTE)


def replace_lines_own_code(path: str, start_line: int, end_line: int,
                           new_text: str,
                           root: Optional[Path] = None) -> Tuple[bool, str]:
    """Replace lines [start_line, end_line] (1-based, inclusive) of
    one of her own source files with `new_text` — the line-addressed
    counterpart to patch_own_code's exact-text anchor, for edits
    where reproducing the old text byte-exactly is the fragile part
    (Alek's ruling, 2026-10-25). An empty new_text deletes the span.
    Region-gated exactly like patch: the span plus
    _PATCH_CONTEXT_LINES around it must have been read at the
    current version."""
    target, refusal = _resolve_target(path, root)
    if refusal:
        return False, f"Self-edit refused: {refusal}."
    try:
        with open(target, "r", encoding="utf-8") as f:
            current = f.read()
    except FileNotFoundError:
        return False, (f"Self-edit failed: {_rel(target, root)} does not "
                       f"exist — use edit_own_code to create it.")
    except Exception as e:
        return False, f"Self-edit failed: {type(e).__name__}: {e}"
    lines = current.split("\n")
    if lines and lines[-1] == "":
        lines.pop()   # a trailing newline ends the last line
    total = len(lines)
    try:
        start = int(start_line)
        end = int(end_line)
    except (TypeError, ValueError):
        return False, ("Self-edit failed: start_line and end_line "
                       "must be whole line numbers.")
    if start < 1 or end < start or end > total:
        return False, (f"Self-edit failed: lines {start}–{end} are "
                       f"out of range — {_rel(target, root)} has "
                       f"{total} lines. No change made.")
    span = end - start + 1
    if span > _MAX_REPLACE_SPAN:
        return False, (f"Self-edit refused: that span is {span} "
                       f"lines — one replace may change at most "
                       f"{_MAX_REPLACE_SPAN}. Split the change into "
                       f"smaller replaces. No change made.")
    gate = _region_gate_refusal(
        target, root, max(1, start - _PATCH_CONTEXT_LINES),
        min(total, end + _PATCH_CONTEXT_LINES))
    if gate:
        return False, gate
    if new_text:
        new_lines = new_text.split("\n")
        if new_text.endswith("\n"):
            new_lines.pop()   # a trailing newline ends the last new line
    else:
        new_lines = []
    trailing = "\n" if current.endswith("\n") else ""
    pre_sha = _file_sha256(target)
    try:
        _atomic_write(target, "\n".join(
            lines[:start - 1] + new_lines + lines[end:]) + trailing)
    except Exception as e:
        return False, f"Self-edit failed: {type(e).__name__}: {e}"
    _carry_coverage(target, pre_sha, start, end, len(new_lines))
    print(f"[SelfEdit] replaced lines {start}–{end} of "
          f"{_rel(target, root)} ({span} -> {len(new_lines)} lines)")
    return True, (f"Replaced lines {start}–{end} of "
                  f"{_rel(target, root)} ({span} lines) with "
                  f"{len(new_lines)} new lines." + _RESTART_NOTE)


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
    """Import-smoke-test EVERY changed watched .py in a subprocess.

    py_compile passing is NOT enough: a file can parse yet fail at
    import. Each changed module is imported in a fresh interpreter
    with cwd=repo root — the same import the restart would do.
    Parent packages are stubbed (empty __init__, real __path__) so
    the test exercises THE MODULE's own imports rather than package
    __init__ side effects. The root scripts are imported too, by
    bare module name: ui.py is a library to main.py, and main.py's
    __main__ guard means importing it runs its module level — every
    import startup performs — without launching the app. Failures
    caused solely by a missing third-party dependency are skipped
    (unverifiable in this env, not a broken edit). Returns
    [(path, stderr_tail)]. Never raises.
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
        if len(parts) >= 2 and parts[0] not in _PACKAGE_DIRS:
            continue   # unreachable via the watched set; stay safe
        # Root scripts import by bare module name (ui, main); package
        # modules by dotted path. The subprocess runs at the repo
        # root either way, so the import tried is startup's own.
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
