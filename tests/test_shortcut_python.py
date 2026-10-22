"""The desktop shortcut must launch ARIA on Python 3.12 — the
interpreter her dependencies are installed for — regardless of
which Python the running process uses (2026-10-22: launched under
the default 3.14, her push library was silently missing)."""
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
