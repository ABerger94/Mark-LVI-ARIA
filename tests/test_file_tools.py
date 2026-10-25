"""Phase F-REST group 1 — the file trio (ARIA's fileops, ported).

Pinned shut:
  * the two-stage duplicate hash reports REAL duplicates only — same
    first-8KB-different-tail files must not group, and empty files are
    not "duplicates" of each other;
  * find_advanced's filters combine and the content filter actually
    reads contents;
  * organise's dry run touches NOTHING, and the real run only happens
    through the confirmation gate (refused outright with no UI bound)
    and pushes one undo that puts everything back.
"""
import os
import time

import pytest

from core import confirm
from actions import file_duplicates as dup_action
from actions import file_find_advanced as find_action
from actions import file_organize as organize_action


@pytest.fixture(autouse=True)
def _unbind_confirm():
    confirm.bind(None, None, None)
    yield
    confirm.bind(None, None, None)


def _write(path, data: bytes):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


# ── duplicates ───────────────────────────────────────────────────────────────

def test_duplicates_groups_identical_content(tmp_path):
    _write(tmp_path / "a.bin", b"x" * 9000)
    _write(tmp_path / "sub" / "b.bin", b"x" * 9000)
    _write(tmp_path / "c.bin", b"y" * 9000)
    result = dup_action.file_duplicates({"directory": str(tmp_path)})
    assert "status=done" in result
    assert "groups=1" in result
    assert "a.bin" in result and "b.bin" in result


def test_duplicates_same_prefix_different_tail_is_not_a_duplicate(tmp_path):
    # Same size AND same first 8 KB (quick hash collides), different
    # tails: only the full hash separates them. This is the case the
    # two-stage design exists for.
    head = b"h" * 8192
    _write(tmp_path / "a.bin", head + b"A" * 100)
    _write(tmp_path / "b.bin", head + b"B" * 100)
    result = dup_action.file_duplicates({"directory": str(tmp_path)})
    assert "groups=0" in result
    assert "No duplicates found" in result


def test_duplicates_ignores_empty_files_and_dotfiles(tmp_path):
    _write(tmp_path / "empty1", b"")
    _write(tmp_path / "empty2", b"")
    _write(tmp_path / ".hidden1", b"same")
    _write(tmp_path / ".hidden2", b"same")
    result = dup_action.file_duplicates({"directory": str(tmp_path)})
    assert "groups=0" in result


def test_duplicates_missing_directory_fails_honestly(tmp_path):
    result = dup_action.file_duplicates(
        {"directory": str(tmp_path / "nope")})
    assert "status=failed" in result


# ── find advanced ────────────────────────────────────────────────────────────

def test_find_by_pattern_and_content(tmp_path):
    _write(tmp_path / "notes.txt", b"the quick brown fox")
    _write(tmp_path / "other.txt", b"nothing here")
    _write(tmp_path / "notes.md", b"the quick brown fox")
    result = find_action.file_find_advanced(
        {"directory": str(tmp_path), "pattern": "*.txt",
         "content_contains": "quick brown"})
    assert "matches=1" in result
    assert "notes.txt" in result and "notes.md" not in result


def test_find_by_size_and_age(tmp_path):
    big = _write(tmp_path / "big.bin", b"z" * 3000)
    small = _write(tmp_path / "small.bin", b"z" * 10)
    old = _write(tmp_path / "old.bin", b"z" * 3000)
    past = time.time() - 10 * 86400
    os.utime(old, (past, past))
    result = find_action.file_find_advanced(
        {"directory": str(tmp_path), "min_size_mb": 0.002,
         "max_age_days": 5})
    assert "matches=1" in result
    assert "big.bin" in result
    assert "old.bin" not in result and "small.bin" not in result


# ── organise ─────────────────────────────────────────────────────────────────

def _populate(folder):
    _write(folder / "photo.jpg", b"img")
    _write(folder / "report.pdf", b"doc")
    _write(folder / "song.mp3", b"aud")
    _write(folder / "mystery.xyz", b"?")
    _write(folder / ".hidden", b"h")
    (folder / "subdir").mkdir()
    _write(folder / "subdir" / "nested.jpg", b"n")


def test_organize_dry_run_plans_and_touches_nothing(tmp_path):
    _populate(tmp_path)
    result = organize_action.file_organize({"directory": str(tmp_path)})
    assert "status=done" in result and "dry_run=true" in result
    assert "photo.jpg -> Images" in result.replace("\\", "/")
    assert "mystery.xyz -> Other" in result.replace("\\", "/")
    # Nothing moved, no category folders created.
    assert (tmp_path / "photo.jpg").exists()
    assert not (tmp_path / "Images").exists()
    # Non-recursive + dotfiles respected in the plan.
    assert "nested.jpg" not in result and ".hidden" not in result


def test_organize_real_run_is_refused_without_a_ui(tmp_path):
    _populate(tmp_path)
    result = organize_action.file_organize(
        {"directory": str(tmp_path), "dry_run": False})
    assert "have not done it" in result or "not done it" in result
    assert (tmp_path / "photo.jpg").exists()


def test_organize_real_run_through_the_gate(tmp_path):
    _populate(tmp_path)
    shown = []
    confirm.bind(show=lambda t, d: shown.append((t, d)),
                 hide=lambda: None, log=None)
    result = organize_action.file_organize(
        {"directory": str(tmp_path), "dry_run": False})
    assert "CONFIRMATION_PENDING" in result
    assert shown, "the gate was never shown"
    assert (tmp_path / "photo.jpg").exists()  # not yet

    confirm.resolve(True)
    # resolve() runs the work on a worker thread; wait for the run to
    # FINISH, not for its first move. The old wait broke on photo.jpg
    # alone and asserted the rest immediately — on a loaded machine
    # (CI) the worker was still mid-run and report.pdf wasn't moved
    # yet: the flake this replaces (~1 in 30 under CPU load, captured
    # 2026-10-24). The run's own completion signal is its undo push,
    # which lands after the last move.
    from core import undo as _undo
    for _ in range(200):
        if (tmp_path / "Images" / "photo.jpg").exists() and _undo.can_undo():
            break
        time.sleep(0.05)
    assert (tmp_path / "Images" / "photo.jpg").exists()
    assert (tmp_path / "Documents" / "report.pdf").exists()
    assert (tmp_path / "Audio" / "song.mp3").exists()
    assert (tmp_path / "Other" / "mystery.xyz").exists()
    assert (tmp_path / ".hidden").exists()

    # The one undo reverses the whole run.
    from core import undo
    msg = undo.undo_last()
    assert "put back" in msg
    assert (tmp_path / "photo.jpg").exists()
    assert not (tmp_path / "Images").exists()


def test_organize_empty_folder_is_a_quiet_done(tmp_path):
    result = organize_action.file_organize({"directory": str(tmp_path)})
    assert "status=done" in result and "moves=0" in result
