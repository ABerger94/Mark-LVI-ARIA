"""Phase D (P4) — the self-edit tools, the read-before-edit gate, and
the validation primitives.

The failure being pinned shut is the one ARIA shipped until her
2026-10-21 fix package: her only write tool was sandboxed away from
her own source, so "editing her code" wrote a copy somewhere the
restart watcher never looked — nothing validated, nothing announced,
nothing restarted, and the user told it had been.

The turn-end watcher itself now lives in core/codewatch.py
(CodeWatcher, tested in tests/test_codewatch.py); it composes the
primitives kept in core/selfedit.py. This file pins those halves: the
tools write only inside the allowlist (which is the watched set), the
two-tier read-before-edit gate refuses edits to files or regions that
have not been read at their current hash, and the validation primitives — compile is not
enough, so every changed file is smoke-imported, and a broken file
is restored from git.
"""
import subprocess
from pathlib import Path

import pytest

from core import selfedit


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


def _read_fully(tree: Path, rel: str) -> None:
    """Read a fixture file end to end through the gated reader, two
    lines at a time so the chunking itself is exercised."""
    n = len((tree / rel).read_text(encoding="utf-8").splitlines())
    offset = 1
    while offset <= max(n, 1):
        ok, text = selfedit.read_own_code(rel, offset=offset, limit=2,
                                          root=tree)
        assert ok, text
        offset += 2


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
    _read_fully(tree, "core/prompt.txt")   # the read-before-edit gate
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
    _read_fully(tree, "core/mod.py")       # the read-before-edit gate
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 1", "VALUE = 2", root=tree)
    assert ok, msg
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 2\n"


def test_patch_own_code_refuses_not_found(tree):
    _read_fully(tree, "core/mod.py")       # past the gate: the refusal
    ok, msg = selfedit.patch_own_code(     # below is the not-found one
        "core/mod.py", "VALUE = 99", "VALUE = 2", root=tree)
    assert not ok
    assert "not found" in msg
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 1\n"   # untouched


def test_patch_own_code_refuses_non_unique(tree):
    _write(tree, "core/mod.py", "VALUE = 1\nVALUE = 1\n")
    _read_fully(tree, "core/mod.py")       # read AFTER the rewrite:
    ok, msg = selfedit.patch_own_code(     # coverage is hash-bound
        "core/mod.py", "VALUE = 1", "VALUE = 2", root=tree)
    assert not ok
    assert "matches 2 places" in msg
    assert "VALUE = 2" not in (tree / "core/mod.py").read_text(
        encoding="utf-8")                    # untouched


# ── validation primitives: EVERY file is import-checked ─────────────────────

def _smoke(tree: Path, *rels: str):
    return selfedit._smoke_import_changed(
        [str(tree / rel) for rel in rels], tree)


def test_smoke_import_flags_ui_edit_that_dies_at_import(tree):
    """The 2026-10-22 break, replayed at the primitive CodeWatcher
    composes: a ui.py that compiles but dies at module level — a class
    subclassing an attribute a module does not have (the invented-
    framework shape). Root scripts used to be compile-checked only,
    so this restarted her into the crash; the smoke import catches it
    here instead."""
    _write(tree, "ui.py",
           "import core.mod as _m\n\n\n"
           "class HUDReactorCore(_m.View):\n    pass\n")
    failed = _smoke(tree, "ui.py")
    assert [Path(p).name for p, _ in failed] == ["ui.py"]


def test_smoke_import_flags_main_edit_that_dies_at_import(tree):
    """main.py gets the same treatment: importing it runs its module
    level (the __main__ guard keeps the app from launching), so a
    module-level NameError is caught by validation, not by the
    restart crashing into it."""
    _write(tree, "main.py", "VALUE = undefined_name_xyz\n")
    failed = _smoke(tree, "main.py")
    assert [Path(p).name for p, _ in failed] == ["main.py"]


def test_smoke_import_passes_valid_edits_and_never_runs_main(tree):
    """A main.py whose main() would explode if RUN proves the smoke
    import does not launch the app; a valid ui.py edit passes too."""
    _write(tree, "main.py",
           "def main():\n    raise SystemExit('must not run on import')\n"
           "\n\nif __name__ == '__main__':\n    main()\n")
    _write(tree, "ui.py", "VALUE = 'valid ui'\n")
    assert _smoke(tree, "main.py", "ui.py") == []


def test_smoke_import_exempts_missing_third_party_dep(tree):
    """An import that fails only because an external package is absent
    on this machine is an environment gap, not a broken edit — ARIA's
    rule, kept: flagging it would restore good edits forever."""
    _write(tree, "core/mod.py",
           "import some_third_party_pkg_that_does_not_exist_xyz\n")
    assert _smoke(tree, "core/mod.py") == []


def test_restore_last_committed_brings_back_committed_version(tree):
    target = _write(tree, "core/mod.py", "VALUE = (\n")  # syntax error
    backup = selfedit._restore_last_committed(str(target), tree)
    # The committed version is back; the broken one is kept beside it.
    assert backup is not None and Path(backup).exists()
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"
    assert list(tree.glob("core/mod.py.broken-*"))


def test_restore_last_committed_returns_none_for_uncommitted_file(tree):
    """A brand-new file has nothing committed to restore TO: restore
    reports failure and the broken file is left in place, exactly as
    before."""
    target = _write(tree, "core/new_broken.py", "def broken(:\n")
    assert selfedit._restore_last_committed(str(target), tree) is None
    assert target.exists()


# ── the read-before-edit gate ───────────────────────────────────────────────

def test_edit_refused_without_any_read(tree):
    ok, msg = selfedit.edit_own_code(
        "core/mod.py", "VALUE = 2\n", root=tree)
    assert not ok
    assert "not read" in msg and "read_own_code" in msg
    assert "1 of its 1 lines are unread" in msg
    # Untouched.
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "VALUE = 1\n"


def test_patch_refused_with_partial_coverage(tree):
    """The region gate, both directions: a patch whose neighborhood
    is unread is refused — and the refusal names the unread lines and
    the exact read that fixes it — while a patch whose neighborhood
    plus margin HAS been read is allowed with the rest of the file
    unread."""
    _write(tree, "core/mod.py", "A = 1\nB = 2\nC = 3\n")
    ok, _ = selfedit.read_own_code("core/mod.py", offset=1, limit=2,
                                   root=tree)
    assert ok
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "C = 3", "C = 4", root=tree)
    assert not ok
    assert "region lines 3" in msg
    assert "offset=1" in msg and "limit=3" in msg
    assert "C = 3" in (tree / "core/mod.py").read_text(encoding="utf-8")

    lines = "".join(f"L{n:03d} = {n}\n" for n in range(1, 201))
    _write(tree, "core/mod.py", lines)
    # Read ONLY line 130's neighborhood plus the 60-line margin.
    ok, _ = selfedit.read_own_code("core/mod.py", offset=70, limit=121,
                                   root=tree)
    assert ok
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "L130 = 130", "L130 = 131", root=tree)
    assert ok, msg
    assert "L130 = 131" in (tree / "core/mod.py").read_text(
        encoding="utf-8")


def test_edit_allowed_after_full_chunked_read(tree):
    _write(tree, "core/mod.py", "A = 1\nB = 2\nC = 3\n")
    _read_fully(tree, "core/mod.py")
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "B = 2", "B = 20", root=tree)
    assert ok, msg
    assert "B = 20" in (tree / "core/mod.py").read_text(encoding="utf-8")


def test_gate_resets_when_file_changes_after_read(tree):
    _read_fully(tree, "core/mod.py")
    _write(tree, "core/mod.py", "VALUE = 99\n")     # an outside change
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 99", "VALUE = 100", root=tree)
    assert not ok
    assert "not read" in msg
    assert "VALUE = 99" in (tree / "core/mod.py").read_text(
        encoding="utf-8")                            # untouched


def test_her_own_patch_carries_coverage(tree):
    """The carry-over contract (2026-10-25): a patch she makes
    through this module keeps coverage alive at the new hash — the
    new version's content is known exactly because she authored the
    change — so a second patch in the same region succeeds with NO
    re-read. An OUTSIDE change still resets everything; that half
    stays pinned by test_gate_resets_when_file_changes_after_read."""
    _read_fully(tree, "core/mod.py")
    ok, _ = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 1", "VALUE = 2", root=tree)
    assert ok
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 2", "VALUE = 3", root=tree)
    assert ok, msg
    assert "VALUE = 3" in (tree / "core/mod.py").read_text(
        encoding="utf-8")


def test_patch_after_successful_rewrite_needs_no_reread(tree):
    """Authored coverage for the rewrite tier: after her own
    edit_own_code rewrite, every line of the new version counts as
    read, so a patch lands with no re-read. (edit_own_code itself
    still refuses a partial read — the full-tier gate is unchanged,
    pinned by test_edit_refused_without_any_read.)"""
    _read_fully(tree, "core/mod.py")
    ok, _ = selfedit.edit_own_code(
        "core/mod.py", "VALUE = 10\nOTHER = 20\n", root=tree)
    assert ok
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "VALUE = 10", "VALUE = 11", root=tree)
    assert ok, msg
    assert "VALUE = 11" in (tree / "core/mod.py").read_text(
        encoding="utf-8")


def test_read_own_code_reports_ranges_and_total(tree):
    _write(tree, "core/mod.py", "A = 1\nB = 2\nC = 3\nD = 4\nE = 5\n")
    ok, text = selfedit.read_own_code("core/mod.py", offset=2, limit=2,
                                      root=tree)
    assert ok
    assert "of 5" in text and "3: C = 3" in text
    assert "2 of 5 lines read; 3 still unread" in text
    ok, text = selfedit.read_own_code("core/mod.py", offset=4, limit=2,
                                      root=tree)
    assert ok
    # Lines {2,3,4,5} read; line 1 still unread — ranges union.
    assert "4 of 5 lines read; 1 still unread" in text


def test_note_read_text_counts_only_received_lines(tree):
    """The general reader's route into coverage: only the lines in
    the text actually returned count (its truncation respected)."""
    _write(tree, "core/mod.py", "A = 1\nB = 2\nC = 3\n")
    target = str(tree / "core/mod.py")
    selfedit.note_read_text(target, "A = 1\nB = 2\n", root=tree)
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "C = 3", "C = 4", root=tree)
    assert not ok
    assert "1 of its 3 lines are unread" in msg
    selfedit.note_read_text(target, "A = 1\nB = 2\nC = 3\n", root=tree)
    ok, _ = selfedit.patch_own_code(
        "core/mod.py", "C = 3", "C = 4", root=tree)
    assert ok


# ── replace_lines_own_code ──────────────────────────────────────────────────

def _numbered(tree: Path, n: int = 200) -> None:
    _write(tree, "core/mod.py",
           "".join(f"L{i:03d} = {i}\n" for i in range(1, n + 1)))


def test_replace_lines_after_region_only_read(tree):
    _numbered(tree)
    ok, _ = selfedit.read_own_code("core/mod.py", offset=40, limit=123,
                                   root=tree)
    assert ok   # lines 40–162 read; the rest of the file is unread
    ok, msg = selfedit.replace_lines_own_code(
        "core/mod.py", 100, 102, "X = 1\nY = 2", root=tree)
    assert ok, msg
    lines = (tree / "core/mod.py").read_text(
        encoding="utf-8").splitlines()
    assert len(lines) == 199
    assert lines[99] == "X = 1" and lines[100] == "Y = 2"
    assert lines[101] == "L103 = 103"
    assert "post-turn restart check" in msg


def test_replace_lines_refused_without_any_read(tree):
    _write(tree, "core/mod.py", "A = 1\nB = 2\nC = 3\n")
    ok, msg = selfedit.replace_lines_own_code(
        "core/mod.py", 2, 2, "B = 20", root=tree)
    assert not ok
    # The refusal names the unread region lines (span ± margin,
    # clamped to the file).
    assert "region lines 1–3" in msg
    assert "B = 2" in (tree / "core/mod.py").read_text(
        encoding="utf-8")                    # untouched


def test_replace_lines_out_of_range_refusals(tree):
    _write(tree, "core/mod.py", "A = 1\nB = 2\nC = 3\n")
    ok, msg = selfedit.replace_lines_own_code(
        "core/mod.py", 2, 9, "B = 20", root=tree)
    assert not ok
    assert "has 3 lines" in msg   # names the file's actual line count
    ok, msg = selfedit.replace_lines_own_code(
        "core/mod.py", 0, 1, "B = 20", root=tree)
    assert not ok
    assert "out of range" in msg
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "A = 1\nB = 2\nC = 3\n"   # untouched


def test_replace_lines_span_cap_refusal(tree):
    _numbered(tree, 500)
    ok, msg = selfedit.replace_lines_own_code(
        "core/mod.py", 1, 401, "X = 1", root=tree)
    assert not ok
    assert "at most 400" in msg
    assert "L401 = 401" in (tree / "core/mod.py").read_text(
        encoding="utf-8")                    # untouched


def test_replace_lines_empty_text_deletes_span(tree):
    _write(tree, "core/mod.py", "A = 1\nB = 2\nC = 3\n")
    _read_fully(tree, "core/mod.py")
    ok, msg = selfedit.replace_lines_own_code(
        "core/mod.py", 2, 2, "", root=tree)
    assert ok, msg
    assert (tree / "core/mod.py").read_text(
        encoding="utf-8") == "A = 1\nC = 3\n"


def test_replace_lines_insertion_shifts_coverage(tree):
    """Insert lines ABOVE a read region: coverage carries over and
    shifts with the content, so a patch inside the shifted region
    still passes the gate with no re-read."""
    _numbered(tree)
    ok, _ = selfedit.read_own_code("core/mod.py", offset=1, limit=190,
                                   root=tree)
    assert ok   # lines 1–190 covered at this hash
    ok, msg = selfedit.replace_lines_own_code(
        "core/mod.py", 10, 10,
        "N1 = 0\nN2 = 0\nN3 = 0\nN4 = 0\nN5 = 0", root=tree)
    assert ok, msg   # 1 line becomes 5: everything below shifts +4
    lines = (tree / "core/mod.py").read_text(
        encoding="utf-8").splitlines()
    assert lines[123] == "L120 = 120"   # was line 120, now line 124
    ok, msg = selfedit.patch_own_code(
        "core/mod.py", "L120 = 120", "L120 = 121", root=tree)
    assert ok, msg
