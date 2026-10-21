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
