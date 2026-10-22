"""
core/streams.py — a missing stdout/stderr must never eat the app.

Launched windowless (pythonw.exe, a GUI-subsystem parent, some IDEs),
Python starts with sys.stdout and/or sys.stderr set to None. Every
print() then has nowhere to go, and any background task that logs on
its startup path — the phone dashboard's serve() above all — can die
silently while the app itself looks perfectly healthy. (2026-10-22:
the phone bridge stopped loading in the desktop-shortcut era, when
the shortcut still targeted pythonw; terminal launches were fine.
The shortcut now targets the console interpreter, and this guard is
the belt to that suspender: NO launch style gets to lose output —
or a task — to a dead stream again.)

ensure_streams() is called once, at the very top of main.py, before
anything prints. Any stream that is None is pointed at an append-mode
UTF-8 log file (logs/aria-console.log beside the app root) so output
always lands somewhere real and readable after the fact. Streams
that exist are NEVER touched — a terminal launch behaves exactly as
it did before this module existed. The function never raises: if
the log file cannot be opened, the streams are left exactly as they
were and the app carries on.
"""
import sys
from pathlib import Path

# The app root, resolved exactly the way dashboard/server.py resolves
# its BASE_DIR: this file lives one level below the root (in core/).
APP_ROOT = Path(__file__).resolve().parent.parent
LOG_NAME = "aria-console.log"


def ensure_streams(log_dir=None):
    """
    Give a None sys.stdout / sys.stderr a real file to write to.

    Returns the Path of the log file when at least one stream was
    redirected, else None — either because both streams already
    existed (nothing to do) or because the log file could not be
    opened (streams then left untouched). Never raises.
    """
    try:
        missing = [name for name in ("stdout", "stderr")
                   if getattr(sys, name, None) is None]
        if not missing:
            return None
        directory = (Path(log_dir) if log_dir is not None
                     else APP_ROOT / "logs")
        directory.mkdir(parents=True, exist_ok=True)
        log_path = directory / LOG_NAME
        # Open BEFORE assigning anything: if this fails, no stream has
        # been touched. One shared handle for both streams keeps their
        # lines in arrival order in the single file. Line-buffered, so
        # a crash never takes unflushed lines with it.
        handle = open(log_path, "a", encoding="utf-8", buffering=1)
        for name in missing:
            setattr(sys, name, handle)
        return log_path
    except Exception:
        return None
