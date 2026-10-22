"""core/codewatch.py — the in-process code watcher (the old ARIA's
design, ported 2026-10-22 when the updater sidecar was retired).

What is pinned here: the boot-snapshot diff (modified detected,
unchanged ignored, deleted skipped), the persona file as a
hash-only watched file, validation through selfedit's compile +
smoke import (with the missing-third-party-dependency exemption),
restore-from-git on broken code, no restart when the restore
fails, exactly one armed restart through the shared arbiter, the
speech-drain wait, and the ledger update_status reads back.
"""
import json
import subprocess
import threading
from pathlib import Path

import pytest

from core import codewatch, incidents


# ── fixture tree (test_selfedit's shape) ────────────────────────────────────

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
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "fixture")
    return tmp_path


@pytest.fixture(autouse=True)
def _incidents_to_tmp(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")


def _watcher(tree: Path, **kw) -> codewatch.CodeWatcher:
    kw.setdefault("announce", lambda text: None)
    kw.setdefault("restart", lambda: None)
    kw.setdefault("speech_idle", lambda: True)
    kw.setdefault("arbiter", codewatch.RestartArbiter())
    kw.setdefault("sleep_fn", lambda s: None)   # tests never really sleep
    kw.setdefault("settle_delay", 0.0)
    return codewatch.CodeWatcher(root=tree, **kw)


# ── the boot snapshot and the diff ──────────────────────────────────────────

def test_clean_tree_is_clean(tree):
    said = []
    w = _watcher(tree, announce=said.append)
    assert w.check_after_turn() == "clean"
    assert said == []


def test_modified_file_is_detected(tree):
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append, restart=restarted.set)
    _write(tree, "core/mod.py", "VALUE = 2\n")          # no tool involved
    assert w.check_after_turn() == "restart_armed"
    assert said == ["My code changed — restarting now to load the "
                    "new version."]
    assert restarted.wait(5)


def test_change_before_boot_is_the_baseline(tree):
    """The snapshot is taken at construction: a file already changed
    when the watcher is built is the code she booted with, not a
    change to react to."""
    _write(tree, "core/mod.py", "VALUE = 2\n")
    w = _watcher(tree)
    assert w.check_after_turn() == "clean"


def test_deleted_file_is_skipped(tree):
    """ARIA's rule: the running process still holds the module in
    memory; a deletion alone never arms a restart."""
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append, restart=restarted.set)
    (tree / "core/mod.py").unlink()
    assert w.check_after_turn() == "clean"
    assert said == []
    assert not restarted.wait(0.2)


# ── the persona file: hash-only ─────────────────────────────────────────────

def test_prompt_txt_change_triggers_flow(tree):
    restarted = threading.Event()
    w = _watcher(tree, restart=restarted.set)
    _write(tree, "core/prompt.txt", "You are ARIA, revised.\n")
    assert w.check_after_turn() == "restart_armed"
    assert restarted.wait(5)
    # Nothing restored it — the new persona stays.
    assert "revised" in (tree / "core/prompt.txt").read_text(
        encoding="utf-8")


def test_prompt_txt_is_never_compiled_or_imported(tree):
    """Invalid Python in the persona file would fail any compile —
    the flow must not care: prompt.txt is validated by hash and
    existence only."""
    restarted = threading.Event()
    w = _watcher(tree, restart=restarted.set)
    _write(tree, "core/prompt.txt", "def broken(:\n")
    assert w.check_after_turn() == "restart_armed"
    assert restarted.wait(5)
    assert (tree / "core/prompt.txt").read_text(
        encoding="utf-8") == "def broken(:\n"      # not restored


# ── validation: broken code is restored, never restarted into ──────────────

def test_compile_failure_restores_from_git_and_restarts_restored(tree):
    said, restarted = [], threading.Event()
    restores = []
    real_restore = lambda p: codewatch._selfedit._restore_last_committed(
        p, tree)

    def restore_spy(p):
        restores.append(p)
        return real_restore(p)

    w = _watcher(tree, announce=said.append, restart=restarted.set,
                 restore_fn=restore_spy)
    _write(tree, "core/mod.py", "VALUE = (\n")          # syntax error
    assert w.check_after_turn() == "restored_restart_armed"
    assert len(restores) == 1
    # The committed version is back; the broken one is kept beside it.
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 1\n"
    assert list(tree.glob("core/mod.py.broken-*"))
    assert any("restored the last working mod.py" in s for s in said)
    assert restarted.wait(5), "restart must load the RESTORED tree"
    kinds = [e["kind"] for e in w.status()["events"]]
    assert "restored" in kinds and "restart_armed" in kinds


def test_smoke_import_failure_is_treated_as_broken(tree):
    """Compiles fine, dies at import — the case py_compile alone
    cannot see (validated through selfedit's smoke import)."""
    w = _watcher(tree)
    _write(tree, "core/mod.py", "import core.no_such_module_xyz\n")
    assert w.check_after_turn() == "restored_restart_armed"
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 1\n"


def test_missing_third_party_dep_is_exempt(tree):
    """An import failing only because an external package is absent
    is an environment gap, not a broken edit — the change stands
    and the restart is armed on it."""
    restarted = threading.Event()
    w = _watcher(tree, restart=restarted.set)
    _write(tree, "core/mod.py",
           "import some_third_party_pkg_that_does_not_exist_xyz\n")
    assert w.check_after_turn() == "restart_armed"
    assert restarted.wait(5)
    assert "some_third_party_pkg" in (tree / "core/mod.py").read_text(
        encoding="utf-8")                              # not restored


def test_restore_failure_announces_and_never_restarts(tree):
    said, restarted = [], threading.Event()
    w = _watcher(tree, announce=said.append, restart=restarted.set,
                 restore_fn=lambda p: None)             # restore fails
    _write(tree, "core/mod.py", "VALUE = (\n")
    assert w.check_after_turn() == "restore_failed"
    assert any("new version is broken" in s for s in said)
    assert not restarted.wait(0.3)
    assert w._waiter is None
    kinds = [e["kind"] for e in w.status()["events"]]
    assert "restore_failed" in kinds


# ── exactly one restart, through the shared arbiter ─────────────────────────

def test_restart_armed_exactly_once(tree):
    restarted = threading.Event()
    calls = []
    arbiter = codewatch.RestartArbiter()
    w = _watcher(tree, arbiter=arbiter,
                 restart=lambda: (calls.append(1), restarted.set()))
    _write(tree, "core/mod.py", "VALUE = 2\n")
    assert w.check_after_turn() == "restart_armed"
    assert restarted.wait(5)
    # A second check while the restart is armed is a no-op.
    assert w.check_after_turn() == "restart_pending"
    w._waiter.join(5)
    assert calls == [1]
    assert arbiter.armed_by == "codewatch"


def test_arbiter_first_arm_wins():
    a = codewatch.RestartArbiter()
    assert a.try_arm("codewatch") is True
    assert a.try_arm("watchdog") is False
    assert a.armed_by == "codewatch"
    a.reset()
    assert a.try_arm("watchdog") is True
    assert a.armed_by == "watchdog"


def test_watcher_stands_down_when_arbiter_already_armed(tree):
    """The watchdog armed a restart first: the watcher records the
    suppression and starts no waiter of its own — one relaunch per
    process, whoever armed it."""
    arbiter = codewatch.RestartArbiter()
    assert arbiter.try_arm("watchdog")
    restarted = threading.Event()
    w = _watcher(tree, arbiter=arbiter, restart=restarted.set)
    _write(tree, "core/mod.py", "VALUE = 2\n")
    w.check_after_turn()
    assert w._waiter is None
    assert not restarted.wait(0.3)
    kinds = [e["kind"] for e in w.status()["events"]]
    assert "restart_suppressed" in kinds


# ── the speech-drain wait ───────────────────────────────────────────────────

def test_drain_wait_polls_until_idle_before_restarting(tree):
    polls = []

    def idle():
        polls.append(1)
        return len(polls) >= 3          # busy for two polls, then done

    sleeps = []
    restarted = threading.Event()
    w = _watcher(tree, speech_idle=idle, sleep_fn=sleeps.append,
                 poll_interval=0.25, settle_delay=0.75,
                 restart=restarted.set)
    _write(tree, "core/mod.py", "VALUE = 2\n")
    assert w.check_after_turn() == "restart_armed"
    assert restarted.wait(5)
    assert len(polls) == 3
    # Two poll-interval waits while she was speaking, then the
    # settle delay — the restart came only after speech drained.
    assert sleeps == [0.25, 0.25, 0.75]


def test_drain_wait_is_bounded(tree):
    """Speech that never finishes cannot hold the restart forever:
    past the drain bound, she restarts anyway."""
    polls = []
    restarted = threading.Event()
    w = _watcher(tree,
                 speech_idle=lambda: polls.append(1) or False,
                 sleep_fn=lambda s: None,
                 drain_timeout=0.15, settle_delay=0.0,
                 restart=restarted.set)
    _write(tree, "core/mod.py", "VALUE = 2\n")
    assert w.check_after_turn() == "restart_armed"
    assert restarted.wait(5)
    assert len(polls) > 1


# ── the say-function seam (old ARIA's call shape) ────────────────────────────

def test_say_fn_overrides_the_bound_announce(tree):
    bound, passed = [], []
    w = _watcher(tree, announce=bound.append)
    _write(tree, "core/mod.py", "VALUE = 2\n")
    assert w.check_after_turn(say_fn=passed.append) == "restart_armed"
    assert passed and bound == []


# ── the ledger update_status reads ──────────────────────────────────────────

def test_ledger_written_and_read_by_update_status(tree):
    from actions import update_status

    w = _watcher(tree)
    w.mark_boot()
    assert w.check_after_turn() == "clean"
    ledger_path = tree / "memory" / "codewatch.json"
    ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
    head = subprocess.run(
        ["git", "-C", str(tree), "rev-parse", "HEAD"],
        capture_output=True, text=True).stdout.strip()
    assert ledger["boot"]["head"] == head
    assert ledger["boot"]["watched"] >= 5
    assert ledger["checks"] == 1
    assert ledger["last_check_ts"] is not None

    text = update_status.render_status(ledger, head, now=(
        ledger["last_check_ts"] + 30))
    assert "in-process" in text
    assert f"Files watched: {ledger['boot']['watched']}." in text
    assert "Turn-end checks so far: 1" in text

    empty = update_status.render_status(None, None, now=0.0)
    assert "no record yet" in empty


def test_ledger_events_are_capped(tree):
    w = _watcher(tree, restore_fn=lambda p: None)
    _write(tree, "core/new_broken.py", "def broken(:\n")
    for _ in range(12):                 # 2 events per failed check
        assert w.check_after_turn() == "restore_failed"
    events = w.status()["events"]
    assert len(events) == codewatch.EVENTS_CAP
    on_disk = json.loads(
        (tree / "memory" / "codewatch.json").read_text(
            encoding="utf-8"))
    assert len(on_disk["events"]) == codewatch.EVENTS_CAP
