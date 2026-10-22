"""The desktop shortcut must launch ARIA on Python 3.12 — the
interpreter her dependencies are installed for — regardless of
which Python the running process uses (2026-10-22: launched under
the default 3.14, her push library was silently missing).

And it must launch the CONSOLE interpreter (python.exe), never
windowless pythonw: under pythonw there is no stdout/stderr at all,
so the phone dashboard died silently in the shortcut era while
terminal launches worked."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ui


class _R:
    def __init__(self, rc, out=""):
        self.returncode = rc
        self.stdout = out


def test_resolver_uses_py_launcher(tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("")
    def fake_runner(argv, **kw):
        assert argv[:2] == ["py", "-3.12"]
        return _R(0, str(exe) + "\n")
    got = ui.MainWindow._resolve_python312(
        runner=fake_runner, version_info=(3, 14, 0),
        executable="/elsewhere/python.exe")
    assert got == exe


def test_resolver_falls_back_to_current_when_current_is_312():
    def failing_runner(argv, **kw):
        return _R(1, "")
    got = ui.MainWindow._resolve_python312(
        runner=failing_runner, version_info=(3, 12, 10),
        executable="/py312/python.exe")
    assert got == Path("/py312/python.exe")


def test_resolver_none_when_no_312_anywhere():
    def failing_runner(argv, **kw):
        raise FileNotFoundError("no py launcher")
    got = ui.MainWindow._resolve_python312(
        runner=failing_runner, version_info=(3, 14, 0),
        executable="/py314/python.exe")
    assert got is None


# ── The shortcut TARGET: console python.exe, never pythonw ──────────────────
# The choice lives inline in _create_desktop_shortcut's Windows branch, so
# these tests drive that real branch with its collaborators stubbed and
# capture the target handed to the .lnk writer.

class _Log:
    def __init__(self):
        self.lines = []

    def append_log(self, line):
        self.lines.append(line)


def _shortcut_target(monkeypatch, tmp_path, resolved):
    captured = {}
    win = ui.MainWindow.__new__(ui.MainWindow)
    win._log = _Log()
    monkeypatch.setattr(ui.platform, "system", lambda: "Windows")
    monkeypatch.setattr(ui.MainWindow, "_resolve_python312",
                        staticmethod(lambda: resolved))
    monkeypatch.setattr(ui.MainWindow, "_get_desktop_dir",
                        staticmethod(lambda: tmp_path))

    def fake_lnk(lnk, target, args, work_dir, icon_loc):
        captured["target"] = target

    monkeypatch.setattr(ui.MainWindow, "_create_lnk_windows",
                        staticmethod(fake_lnk))
    win._create_desktop_shortcut()
    return captured.get("target"), win._log.lines


def _install_dir(tmp_path, *names):
    install = tmp_path / "Python312"
    install.mkdir()
    for name in names:
        (install / name).write_text("")
    return install


def test_shortcut_target_prefers_console_when_base_is_pythonw(
        tmp_path, monkeypatch):
    install = _install_dir(tmp_path, "python.exe", "pythonw.exe")
    target, lines = _shortcut_target(
        monkeypatch, tmp_path, install / "pythonw.exe")
    assert target == str(install / "python.exe")
    # The SYS line names the interpreter that was actually chosen.
    assert any("Desktop shortcut created" in line and target in line
               for line in lines)


def test_shortcut_target_pythonw_only_when_no_console_sibling(
        tmp_path, monkeypatch):
    install = _install_dir(tmp_path, "pythonw.exe")
    target, _lines = _shortcut_target(
        monkeypatch, tmp_path, install / "pythonw.exe")
    assert target == str(install / "pythonw.exe")


def test_shortcut_target_keeps_console_base(tmp_path, monkeypatch):
    # The resolver normally returns python.exe itself; even with a
    # pythonw sibling sitting beside it, the target stays the console
    # interpreter. (The old code preferred pythonw here — the bug.)
    install = _install_dir(tmp_path, "python.exe", "pythonw.exe")
    target, _lines = _shortcut_target(
        monkeypatch, tmp_path, install / "python.exe")
    assert target == str(install / "python.exe")
