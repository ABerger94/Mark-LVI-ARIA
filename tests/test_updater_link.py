"""App-side updater link helpers (core/updater_link.py)."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import updater_link


def test_no_window_kwargs_windows():
    assert updater_link.no_window_kwargs("nt") == {
        "creationflags": getattr(subprocess, "CREATE_NO_WINDOW",
                                 0x08000000)}


def test_no_window_kwargs_posix():
    assert updater_link.no_window_kwargs("posix") == {}


def test_sidecar_executable_prefers_pythonw_on_windows(tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("")
    w = tmp_path / "pythonw.exe"
    w.write_text("")
    assert updater_link.sidecar_executable(str(exe), "nt") == str(w)


def test_sidecar_executable_falls_back_without_pythonw(tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("")
    assert updater_link.sidecar_executable(str(exe), "nt") == str(exe)


def test_sidecar_executable_untouched_off_windows(tmp_path):
    exe = tmp_path / "python.exe"
    exe.write_text("")
    (tmp_path / "pythonw.exe").write_text("")
    assert updater_link.sidecar_executable(str(exe), "posix") == str(exe)
