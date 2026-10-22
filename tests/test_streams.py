"""core.streams.ensure_streams — the missing-stream guard.

A windowless launch (pythonw) starts Python with sys.stdout and
sys.stderr set to None; prints then vanish and a background task
that logs on startup (the phone dashboard) can die silently.
ensure_streams must give the missing streams a real log file, must
never touch streams that exist, and must never raise.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core import streams


def test_none_streams_redirected_to_log_file(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    result = streams.ensure_streams(log_dir=tmp_path)
    assert result == tmp_path / "aria-console.log"
    assert sys.stdout is not None
    assert sys.stderr is not None
    # One shared handle, so the two streams interleave in arrival order.
    assert sys.stdout is sys.stderr
    sys.stdout.write("marker-out\n")
    sys.stderr.write("marker-err\n")
    sys.stdout.flush()
    sys.stdout.close()
    text = (tmp_path / "aria-console.log").read_text(encoding="utf-8")
    assert "marker-out" in text
    assert "marker-err" in text


def test_only_the_missing_stream_is_redirected(tmp_path, monkeypatch):
    sentinel_out = sys.stdout
    monkeypatch.setattr(sys, "stderr", None)
    result = streams.ensure_streams(log_dir=tmp_path)
    assert result == tmp_path / "aria-console.log"
    assert sys.stdout is sentinel_out          # untouched — same object
    assert sys.stderr is not None
    sys.stderr.close()


def test_existing_streams_untouched_and_no_file_created(tmp_path, monkeypatch):
    sentinel_out, sentinel_err = sys.stdout, sys.stderr
    log_dir = tmp_path / "logs"
    result = streams.ensure_streams(log_dir=log_dir)
    assert result is None
    assert sys.stdout is sentinel_out
    assert sys.stderr is sentinel_err
    assert not log_dir.exists()                # nothing was even created


def test_default_location_is_logs_beside_app_root(tmp_path, monkeypatch):
    monkeypatch.setattr(streams, "APP_ROOT", tmp_path)
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    result = streams.ensure_streams()
    assert result == tmp_path / "logs" / "aria-console.log"
    assert (tmp_path / "logs" / "aria-console.log").exists()
    sys.stdout.close()


def test_failure_is_silent_and_leaves_streams_alone(tmp_path, monkeypatch):
    blocker = tmp_path / "blocker"
    blocker.write_text("")                     # a FILE where a dir must go
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    result = streams.ensure_streams(log_dir=blocker / "logs")
    assert result is None                      # no raise, quiet None
    assert sys.stdout is None                  # left exactly as they were
    assert sys.stderr is None
