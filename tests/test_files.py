"""Phase C2 — full file access + the D3 gates (blueprint P5).

Two rulings pinned here:

  * FULL ACCESS. file_controller confined every operation to the home
    folder (_SAFE_ROOTS) and answered 'Access denied' outside it; the
    screenshot path in computer_control silently substituted a Desktop
    file for any path outside home. Both are gone. These tests run a
    full round-trip in pytest's tmp dir — outside the repo, outside
    anywhere the old fence would have allowed if home were elsewhere —
    and assert the operations simply work there, plus path-resolution
    units (shortcut prefixes, absolute pass-through, drive letters on
    Windows).

  * THE GATES. The destructive classes from blueprint D3 now sit behind
    core/confirm.py's token: send_message, game installs, the
    shut-down-when-done path in game_updater (previously reachable with
    no human near it), dev_agent's builds (which pip-install and run
    what they write), and file_controller's delete. Each gate test
    binds a fake UI, shows the action refuses/pends without a press,
    and runs only after resolve(True) — never after resolve(False).
    The one deliberate exception is pinned too: a plain game UPDATE is
    not gated, because the OS-scheduled --scheduled entry point calls
    it with no interface bound and gating it would silently kill
    scheduled updates.
"""
import os
import time
from pathlib import Path

import pytest

import actions.computer_control as cc
import actions.dev_agent as da
import actions.file_controller as fc
import actions.game_updater as gu
import actions.send_message as sm
from core import confirm


def _wait_for(pred, timeout=5.0):
    end = time.time() + timeout
    while time.time() < end:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


# ── full access: the round-trip outside the repo ────────────────────────────

def test_file_round_trip_outside_the_repo(tmp_path):
    root = tmp_path / "elsewhere" / "deep" / "tree"
    other = tmp_path / "other-side"
    other.mkdir()   # copy/move treat an existing dir as the container

    out = fc.create_file(str(root), name="a.txt", content="hello")
    assert "Access denied" not in out and (root / "a.txt").exists()

    assert fc.read_file(str(root), name="a.txt") == "hello"

    fc.write_file(str(root), name="a.txt", content=" world", append=True)
    assert fc.read_file(str(root), name="a.txt") == "hello world"

    assert "a.txt" in fc.list_files(str(root))

    out = fc.copy_file(str(root), name="a.txt", destination=str(other))
    assert "Access denied" not in out and (other / "a.txt").exists()

    out = fc.rename_file(str(other), name="a.txt", new_name="b.txt")
    assert "Access denied" not in out and (other / "b.txt").exists()

    out = fc.move_file(str(other), name="b.txt", destination=str(root))
    assert "Access denied" not in out and (root / "b.txt").exists()

    assert "b.txt" in fc.find_files(name="b.txt", path=str(tmp_path))
    assert "b.txt" in fc.get_file_info(str(root), name="b.txt")
    assert "Access denied" not in fc.get_largest_files(str(tmp_path))
    assert "sub" in fc.create_folder(str(root), name="sub")
    assert (root / "sub").is_dir()


def test_absolute_path_as_the_whole_argument(tmp_path):
    target = tmp_path / "direct.txt"
    out = fc.create_file(str(target), content="x")
    assert "Access denied" not in out and target.read_text() == "x"


def test_resolve_path_absolute_passes_through(tmp_path):
    assert fc._resolve_path(str(tmp_path / "x.txt")) == tmp_path / "x.txt"


def test_resolve_path_shortcut_prefix():
    p = fc._resolve_path("documents/notes/a.md")
    assert p == Path.home() / "Documents" / "notes" / "a.md"


@pytest.mark.skipif(os.name != "nt", reason="drive letters are Windows paths")
def test_resolve_path_drive_letter():
    assert fc._resolve_path(r"E:\Games\file.txt") == Path(r"E:\Games\file.txt")


def test_screenshot_path_is_the_requested_path(tmp_path):
    target = tmp_path / "shots" / "s.png"
    assert cc._screenshot_path(str(target)) == target
    assert target.parent.is_dir()          # the folder is made, as before
    fallback = cc._screenshot_path(None)
    assert fallback.name == "jarvis_screenshot.png"


# ── the gates ────────────────────────────────────────────────────────────────

@pytest.fixture
def ui_bound():
    """core.confirm bound to a recording fake UI for one test."""
    shown = []
    confirm.bind(show=lambda t, d: shown.append((t, d)),
                 hide=lambda: None, log=None)
    yield shown
    if confirm.pending_title():
        confirm.resolve(False)
    confirm.bind(None, None)


@pytest.fixture
def ui_unbound():
    confirm.bind(None, None)
    yield
    confirm.bind(None, None)


def _fake_trash(monkeypatch, tmp_path):
    """send2trash stand-in: actually moves the file, like the real one."""
    bin_dir = tmp_path / "trashbin"
    bin_dir.mkdir(exist_ok=True)

    class _Fake:
        @staticmethod
        def send2trash(s):
            Path(s).replace(bin_dir / Path(s).name)

    monkeypatch.setattr(fc, "_SEND2TRASH", True)
    monkeypatch.setattr(fc, "send2trash", _Fake)
    return bin_dir


def test_delete_is_gated(ui_bound, tmp_path, monkeypatch):
    bin_dir = _fake_trash(monkeypatch, tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("data")

    out = fc.file_controller({"action": "delete", "path": str(tmp_path),
                                "name": "victim.txt"})
    assert "CONFIRMATION_PENDING" in out
    assert victim.exists()                       # not yet

    confirm.resolve(True)
    assert _wait_for(lambda: not victim.exists())
    assert (bin_dir / "victim.txt").exists()     # trashed, not vaporised


def test_delete_cancelled_by_the_user(ui_bound, tmp_path, monkeypatch):
    _fake_trash(monkeypatch, tmp_path)
    victim = tmp_path / "keepme.txt"
    victim.write_text("data")

    fc.file_controller({"action": "delete", "path": str(tmp_path),
                        "name": "keepme.txt"})
    confirm.resolve(False)
    assert victim.exists()


def test_delete_refused_with_no_ui(ui_unbound, tmp_path, monkeypatch):
    _fake_trash(monkeypatch, tmp_path)
    victim = tmp_path / "victim.txt"
    victim.write_text("data")
    out = fc.file_controller({"action": "delete", "path": str(tmp_path),
                              "name": "victim.txt"})
    assert "have not done it" in out
    assert victim.exists()


def test_send_message_is_gated(ui_bound, monkeypatch):
    sent = []
    monkeypatch.setattr(sm, "_PYAUTOGUI", True)
    monkeypatch.setattr(sm, "_send_and_report",
                        lambda *a: sent.append(a))

    out = sm.send_message({"receiver": "Sam", "message_text": "hi",
                           "platform": "whatsapp"})
    assert "CONFIRMATION_PENDING" in out
    assert sent == []                            # nothing typed yet

    confirm.resolve(True)
    assert _wait_for(lambda: len(sent) == 1)
    assert sent[0][2] == "Sam" and sent[0][3] == "hi"


def test_send_message_cancelled(ui_bound, monkeypatch):
    sent = []
    monkeypatch.setattr(sm, "_PYAUTOGUI", True)
    monkeypatch.setattr(sm, "_send_and_report",
                        lambda *a: sent.append(a))
    sm.send_message({"receiver": "Sam", "message_text": "hi",
                     "platform": "whatsapp"})
    confirm.resolve(False)
    time.sleep(0.2)
    assert sent == []


def test_game_install_is_gated(ui_bound, monkeypatch):
    calls = []
    monkeypatch.setattr(gu, "_game_updater_impl",
                        lambda p, player=None, speak=None:
                        calls.append(p) or "IMPL")
    out = gu.game_updater({"action": "install", "game_name": "Rust"})
    assert "CONFIRMATION_PENDING" in out and calls == []
    confirm.resolve(True)
    assert _wait_for(lambda: len(calls) == 1)


def test_game_update_with_shutdown_is_gated(ui_bound, monkeypatch):
    """The second shutdown path: update + shutdown_when_done used to
    power the machine off from a background thread, ungated."""
    calls = []
    monkeypatch.setattr(gu, "_game_updater_impl",
                        lambda p, player=None, speak=None:
                        calls.append(p) or "IMPL")
    out = gu.game_updater({"action": "update",
                           "shutdown_when_done": True})
    assert "CONFIRMATION_PENDING" in out and calls == []
    confirm.resolve(True)
    assert _wait_for(lambda: len(calls) == 1)


def test_plain_game_update_is_not_gated(ui_bound, monkeypatch):
    """Deliberate exception, pinned: the OS-scheduled run calls
    action=update with no interface bound, where the gate refuses by
    design. Gating this would silently kill scheduled updates."""
    calls = []
    monkeypatch.setattr(gu, "_game_updater_impl",
                        lambda p, player=None, speak=None:
                        calls.append(p) or "IMPL")
    out = gu.game_updater({"action": "update", "platform": "both"})
    assert out == "IMPL" and len(calls) == 1


def test_dev_agent_build_is_gated(ui_bound, monkeypatch):
    builds = []
    monkeypatch.setattr(da, "_build_project",
                        lambda **kw: builds.append(kw) or "BUILT")
    out = da.dev_agent({"description": "a to-do app"})
    assert "CONFIRMATION_PENDING" in out and builds == []
    confirm.resolve(True)
    assert _wait_for(lambda: len(builds) == 1)
    assert builds[0]["description"] == "a to-do app"


def test_dev_agent_refused_with_no_ui(ui_unbound, monkeypatch):
    builds = []
    monkeypatch.setattr(da, "_build_project",
                        lambda **kw: builds.append(kw) or "BUILT")
    out = da.dev_agent({"description": "a to-do app"})
    assert "have not done it" in out and builds == []


# ── the tree action + folder routing (2026-10-22) ────────────────────────────
#
# The live failure these pin: asked to look at all the files in a
# folder, she answered that she could not access it — read_file on a
# directory was a dead end ("Not a file"), list was one level, find
# capped at 20 hits, and nothing routed a folder to a listing at all.

def _mk_proj(root: Path) -> Path:
    proj = root / "proj"
    (proj / "core").mkdir(parents=True)
    (proj / "core" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (proj / "ui.py").write_text("y = 2\n", encoding="utf-8")
    git = proj / ".git"
    git.mkdir()
    (git / "config").write_text("secret\n", encoding="utf-8")
    cache = proj / "core" / "__pycache__"
    cache.mkdir()
    (cache / "mod.cpython.pyc").write_bytes(b"\x00\x01")
    return proj


def test_tree_lists_recursively_dirs_first_and_skips_junk(tmp_path):
    out = fc.list_tree(str(_mk_proj(tmp_path)))
    assert "mod.py" in out and "ui.py" in out
    assert ".git" not in out and "__pycache__" not in out
    assert "config" not in out                    # .git never entered
    assert out.index("core/") < out.index("ui.py")   # dirs first


def test_tree_depth_cap_is_honored(tmp_path):
    deep = tmp_path / "a" / "b" / "c" / "d"
    deep.mkdir(parents=True)
    (deep / "leaf.txt").write_text("x", encoding="utf-8")
    out = fc.list_tree(str(tmp_path / "a"), depth=1)
    assert "b/" in out and "c/" not in out
    out = fc.list_tree(str(tmp_path / "a"))          # default depth 3
    assert "d/" in out and "leaf.txt" not in out
    out = fc.list_tree(str(tmp_path / "a"), depth=6)
    assert "leaf.txt" in out
    out = fc.list_tree(str(tmp_path / "a"), depth=99)   # clamped to 6
    assert "leaf.txt" in out


def test_tree_entry_cap_truncates_with_a_note(tmp_path):
    big = tmp_path / "big"
    big.mkdir()
    for i in range(320):
        (big / f"f{i:03}.txt").write_text("x", encoding="utf-8")
    out = fc.list_tree(str(big))
    assert out.count("📄") == 300
    assert "+20 more" in out and "truncated at 300" in out


def test_tree_dispatcher_route(tmp_path):
    out = fc.file_controller({"action": "tree",
                              "path": str(_mk_proj(tmp_path))})
    assert "mod.py" in out and "ui.py" in out


def test_read_on_a_directory_points_at_list_and_tree(tmp_path):
    out = fc.read_file(str(tmp_path))
    assert "Not a file" in out
    assert "'list'" in out and "'tree'" in out


# ── read_file: whole files, then honest paging (2026-10-22) ─────────────────
#
# The live failure these pin: asked to REVIEW a file, she got its first
# 4,000 characters, a "[Truncated]" note and no way onward — and answered
# that she needed to continue. read_file now returns the whole file in
# one call up to a 200,000-char ceiling, and past the ceiling it pages by
# character offset, each partial return naming the exact next call.

def test_read_returns_whole_file_under_the_ceiling(tmp_path):
    body = "a line of text\n" * 1000          # 15,000 chars — past the old cap
    (tmp_path / "big.txt").write_text(body, encoding="utf-8")
    out = fc.read_file(str(tmp_path), name="big.txt")
    assert out == body
    assert "Truncated" not in out and "Partial read" not in out


def test_read_just_over_the_ceiling_pages_to_eof(tmp_path):
    body = "a" * (fc._READ_CEILING + 50)
    (tmp_path / "huge.txt").write_text(body, encoding="utf-8")
    first = fc.read_file(str(tmp_path), name="huge.txt")
    assert first.startswith(body[:fc._READ_CEILING])
    assert f"of {len(body)} total" in first
    assert f"offset={fc._READ_CEILING} to continue" in first
    rest = fc.read_file(str(tmp_path), name="huge.txt",
                        offset=fc._READ_CEILING)
    assert rest == "a" * 50 + f"\n\n[End of file — {len(body)} total chars.]"


def test_read_mid_file_offset_returns_the_right_slice(tmp_path):
    body = "".join(str(i % 10) for i in range(1000))
    (tmp_path / "digits.txt").write_text(body, encoding="utf-8")
    out = fc.read_file(str(tmp_path), name="digits.txt",
                       offset=100, limit=50)
    assert out.startswith(body[100:150])
    assert "chars 100–150 of 1000 total" in out
    assert "offset=150 to continue" in out
    tail = fc.read_file(str(tmp_path), name="digits.txt",
                        offset=950, limit=100)
    assert tail == body[950:] + "\n\n[End of file — 1000 total chars.]"


def test_read_dispatcher_pages_via_parameters(tmp_path):
    body = "x" * 300
    (tmp_path / "d.txt").write_text(body, encoding="utf-8")
    out = fc.file_controller({"action": "read", "path": str(tmp_path),
                              "name": "d.txt", "offset": 250, "limit": 25})
    assert out.startswith(body[250:275])
    assert "offset=275 to continue" in out


def test_read_binary_file_is_refused_not_dumped(tmp_path):
    blob = b"\x89PNG\r\n\x1a\n" + b"\x00\xff\xfe" * 40
    (tmp_path / "blob.bin").write_bytes(blob)
    out = fc.read_file(str(tmp_path), name="blob.bin")
    assert "Not text" in out and "binary" in out


def _watched_tree(tmp_path, monkeypatch):
    """A fake source tree core/selfedit treats as her repo root."""
    from core import selfedit
    tree = tmp_path / "repo"
    (tree / "core").mkdir(parents=True)
    target = tree / "core" / "mod.py"
    monkeypatch.setattr(selfedit, "_base_dir", lambda: tree)
    return selfedit, tree, target


def test_full_read_of_a_watched_file_notes_full_coverage(tmp_path,
                                                         monkeypatch):
    selfedit, _tree, target = _watched_tree(tmp_path, monkeypatch)
    target.write_text("A = 1\nB = 2\nC = 3\n", encoding="utf-8")
    out = fc.read_file(str(target))
    assert out == "A = 1\nB = 2\nC = 3\n"
    assert selfedit._unread_lines(os.path.realpath(target)) == (3, 0)


def test_paged_read_of_a_watched_file_notes_only_the_window(tmp_path,
                                                            monkeypatch):
    selfedit, _tree, target = _watched_tree(tmp_path, monkeypatch)
    target.write_text("AAAA\nBBBB\nCCCC\n", encoding="utf-8")
    fc.read_file(str(target), limit=5)              # line 1 only
    assert selfedit._unread_lines(os.path.realpath(target)) == (3, 2)
    fc.read_file(str(target), offset=5)             # lines 2–3
    assert selfedit._unread_lines(os.path.realpath(target)) == (3, 0)
