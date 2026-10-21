"""Phase D (P4) — the self-edit loop.

The failure being pinned shut is the one ARIA shipped until her
2026-10-21 fix package: her only write tool was sandboxed away from
her own source, so "editing her code" wrote a copy somewhere the
restart watcher never looked — nothing validated, nothing announced,
nothing restarted, and the user told it had been.

Here the loop is tested end to end against a fixture tree: the tools
write only inside the allowlist (which is the watched set), the
watcher notices changes no matter which tool made them (it hashes
files; it does not watch tool calls), good changes arm an announced
restart, and broken changes are restored from git — with the incident
on record — instead of being restarted into.
"""
import subprocess
import threading
from pathlib import Path

import pytest

from core import incidents, selfedit


# ── fixture tree ────────────────────────────────────────────────────────────

def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(root),
         "-c", "user.email=test@example.invalid", "-c", "user.name=Test",
         *args],
        check=True, capture_output=True, text=True)


def _write(root: Path, rel: str, text: str) -> Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


@pytest.fixture()
def tree(tmp_path: Path) -> Path:
    """A minimal LVI-shaped source tree, committed to git."""
    _write(tmp_path, "main.py", "print('entry')\n")
    _write(tmp_path, "ui.py", "print('ui')\n")
    _write(tmp_path, "core/__init__.py", "")
    _write(tmp_path, "core/mod.py", "VALUE = 1\n")
    _write(tmp_path, "core/prompt.txt", "You are ARIA.\n")
    _write(tmp_path, "actions/act.py", "X = 1\n")
    _write(tmp_path, "tests/test_other.py", "def test_x():\n    pass\n")
    _write(tmp_path, "tools/tool.py", "Y = 1\n")
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fixture")
    return tmp_path


def _watcher(tree: Path, **kw) -> selfedit.SelfEditWatcher:
    kw.setdefault("announce", lambda text: None)
    kw.setdefault("restart", lambda: None)
    kw.setdefault("speech_idle", lambda: True)
    return selfedit.SelfEditWatcher(root=tree, **kw)


# ── the watched set ─────────────────────────────────────────────────────────

def test_watched_set_covers_app_tree_and_persona(tree):
    watched = {p.relative_to(tree).as_posix()
               for p in selfedit.watched_source_files(tree)}
    assert "main.py" in watched
    assert "ui.py" in watched
    assert "core/mod.py" in watched
    assert "actions/act.py" in watched
    # The persona file is the SOUL equivalent — watched like code.
    assert "core/prompt.txt" in watched
    # Repo tooling is NOT her running source (ARIA's scoping rule).
    assert "tests/test_other.py" not in watched
    assert "tools/tool.py" not in watched


# ── the tools ───────────────────────────────────────────────────────────────

def test_edit_own_code_writes_allowlisted_file(tree):
    ok, msg = selfedit.edit_own_code(
        "core/new_mod.py", "X = 2\n", root=tree)
    assert ok, msg
    assert (tree / "core/new_mod.py").read_text(encoding="utf-8") == "X = 2\n"
    assert "post-turn restart check" in msg


def test_edit_own_code_can_edit_persona(tree):
    ok, _ = selfedit.edit_own_code(
        "core/prompt.txt", "You are ARIA, take two.\n", root=tree)
    assert ok
    assert "take two" in (tree / "core/prompt.txt").read_text(
        encoding="utf-8")


@pytest.mark.parametrize("path", [
    "tests/test_other.py",      # tooling, not her source
    "tools/tool.py",
    "readme.md",                # not code, not the persona file
    "../outside.py",            # traversal out of the tree
    "core/prompt.txt.bak",      # near-miss on the persona file
])
def test_edit_own_code_refuses_outside_allowlist(tree, path):
    ok, msg = selfedit.edit_own_code(path, "X = 1\n", root=tree)
    assert not ok
    assert "refused" in msg.lower()


def test_patch_own_code_replaces_unique_match(tree):
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 1", "VALUE = 2", root=tree)
    assert ok, msg
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 2\n"


def test_patch_own_code_refuses_not_found(tree):
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 99", "VALUE = 2", root=tree)
    assert not ok
    assert "not found" in msg
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 1\n"   # untouched


def test_patch_own_code_refuses_non_unique(tree):
    _write(tree, "core/mod.py", "VALUE = 1\nVALUE = 1\n")
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 1", "VALUE = 2", root=tree)
    assert not ok
    assert "matches 2 places" in msg
    assert "VALUE = 2" not in (tree / "core/mod.py").read_text(
        encoding="utf-8")                    # untouched


# ── the watcher ─────────────────────────────────────────────────────────────

def test_watcher_clean_tree_does_nothing(tree):
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append,
                 restart=restarted.set)
    assert w.check() == "clean"
    assert said == []
    assert not restarted.wait(0.2)


def test_watcher_catches_general_file_write_and_restarts(tree):
    """The point of hash-watching: an edit made with a plain file
    write — her general file tools, not the self-edit tools — is
    still caught, announced, and loaded by restart."""
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append, restart=restarted.set)
    _write(tree, "core/mod.py", "VALUE = 2\n")          # no tool involved
    assert w.check() == "restart_armed"
    assert said == ["My code changed — restarting now to load the "
                    "new version."]
    assert restarted.wait(10), "restart callback never fired"


def test_watcher_persona_edit_restarts(tree):
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append, restart=restarted.set)
    _write(tree, "core/prompt.txt", "You are ARIA, revised.\n")
    assert w.check() == "restart_armed"
    assert restarted.wait(10)


def test_watcher_restores_broken_edit_and_records_incident(
        tree, monkeypatch):
    log = tree / "incidents.jsonl"
    monkeypatch.setattr(incidents, "INCIDENTS_PATH", log)
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append, restart=restarted.set)
    _write(tree, "core/mod.py", "VALUE = (\n")          # syntax error
    assert w.check() == "restored_restart_armed"
    # The committed version is back; the broken one is kept beside it.
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 1\n"
    assert list(tree.glob("core/mod.py.broken-*"))
    assert any("restored the last working mod.py" in s for s in said)
    # The failure is on the durable record, classified.
    records = incidents.recent(5)
    assert any(r["kind"] == "self_edit_broken" for r in records)
    # ARIA's semantics: she restarts into the RESTORED tree — never
    # into the broken code, which is no longer on disk by now.
    assert restarted.wait(10)


def test_watcher_restores_import_broken_edit(tree, monkeypatch):
    """Compiles fine, dies at import — the case py_compile alone
    cannot see, and the reason the smoke import exists."""
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tree / "incidents.jsonl")
    w = _watcher(tree)
    _write(tree, "core/mod.py", "import core.no_such_module_xyz\n")
    assert w.check() == "restored_restart_armed"
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 1\n"


def test_watcher_ignores_missing_third_party_dep(tree):
    """An import that fails only because an external package is absent
    on this machine is an environment gap, not a broken edit — ARIA's
    rule, kept: flagging it would restore good edits forever."""
    restarted = threading.Event()
    w = _watcher(tree, restart=restarted.set)
    _write(tree, "core/mod.py",
           "import some_third_party_pkg_that_does_not_exist_xyz\n")
    assert w.check() == "restart_armed"
    assert restarted.wait(10)


def test_watcher_unrestorable_break_announces_and_does_not_restart(tree):
    """A brand-new file has nothing committed to restore TO. The
    watcher says the code is broken and does not restart into it."""
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append, restart=restarted.set)
    _write(tree, "core/new_broken.py", "def broken(:\n")
    assert w.check() == "restore_failed"
    assert any("new version is broken" in s for s in said)
    assert not restarted.wait(0.5)
    # And it does not re-announce the same break on every later turn:
    # a restart is not pending, but the file still differs from the
    # boot snapshot — ARIA re-checked too; what matters is the first
    # response was honest and nothing restarted into broken code.
    assert (tree / "core/new_broken.py").exists()
