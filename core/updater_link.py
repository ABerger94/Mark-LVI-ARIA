"""
core/updater_link.py — the app side of the self-update seam.

updater.py (the sidecar) imports NOTHING from this app — it has to
work when the app is broken — so the two processes meet only
through files in memory/. This module owns the app's half of those
files:

  memory/boot_stamp.json      written once at startup: the git HEAD
                              she booted with, the timestamp, her
                              pid and her interpreter. The sidecar's
                              Case B ("pulled under her") is the
                              stamp disagreeing with the disk.
  memory/app_presence.json    refreshed every few seconds by the
                              link loop in main.py: awake? session
                              live? The sidecar's announce-first
                              decision reads it — a stale presence
                              file means she is not running, and the
                              update proceeds without announcing.
  memory/announce_drop.jsonl  lines the sidecar queues; the link
                              loop drains them through
                              JarvisLive._scheduler_say — the same
                              pending-announce queue the scheduler
                              speaks from (queued while she sleeps,
                              spoken on wake). A line the hook
                              refuses (no session) stays in the file
                              for the next pass.

main.py also spawns the sidecar from here at startup (detached, so
it survives her restarts; its lockfile makes repeat spawns exit
quietly). Everything in this module never raises: a broken link
costs the updater its manners, never the app its startup.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def checkout_head(root: Path) -> str:
    """The checkout's current HEAD sha, or "unknown" — a stamp
    that cannot name its code says so rather than guessing."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15)
        sha = (r.stdout or "").strip()
        if r.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return "unknown"


def write_boot_stamp(root: Path | None = None) -> dict:
    """Stamp this boot: {head, ts, pid, executable}. Called once,
    early in JarvisLive.run()."""
    root = Path(root) if root else base_dir()
    stamp = {
        "head": checkout_head(root),
        "ts": time.time(),
        "pid": os.getpid(),
        "executable": sys.executable,
    }
    try:
        _write_json_atomic(root / "memory" / "boot_stamp.json", stamp)
    except Exception:
        pass
    return stamp


def write_presence(root: Path, *, awake: bool,
                   session_live: bool) -> None:
    """Refresh the presence file the sidecar's announce-first
    decision reads. Cheap; called by the link loop every few
    seconds for the life of the process."""
    try:
        _write_json_atomic(root / "memory" / "app_presence.json", {
            "awake": bool(awake),
            "session": bool(session_live),
            "pid": os.getpid(),
            "ts": time.time(),
        })
    except Exception:
        pass


def drain_announcements(root: Path, say) -> int:
    """Feed queued sidecar announcements through `say` (main.py
    passes _scheduler_say). A line is consumed only when say()
    accepts it (True); the first refusal stops the drain and the
    rest stay queued for the next pass. Returns how many were
    delivered. Never raises."""
    drop = Path(root) / "memory" / "announce_drop.jsonl"
    delivered = 0
    try:
        if not drop.is_file():
            return 0
        lines = drop.read_text(encoding="utf-8").splitlines()
        remaining = []
        stopped = False
        for line in lines:
            text = line.strip()
            if not text:
                continue
            if stopped:
                remaining.append(line)
                continue
            try:
                payload = json.loads(text)
                message = str(payload.get("text") or "")
            except Exception:
                message = text
            if not message:
                continue
            try:
                accepted = bool(say(message))
            except Exception:
                accepted = False
            if accepted:
                delivered += 1
            else:
                stopped = True
                remaining.append(line)
        if remaining:
            _write_lines_atomic(drop, remaining)
        else:
            drop.unlink(missing_ok=True)
    except Exception:
        pass
    return delivered


def _write_lines_atomic(path: Path, lines) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def spawn_updater(root: Path | None = None) -> bool:
    """Launch updater.py detached, if this is a git checkout and
    the script is beside us. Detached the same way the restarts
    detach: she must not take the sidecar down with her, and it
    must not hold her console. The sidecar's lockfile makes a
    repeat spawn a quiet no-op, so this is safe on every boot.
    Returns whether a process was launched."""
    root = Path(root) if root else base_dir()
    try:
        script = root / "updater.py"
        if not script.is_file() or not (root / ".git").exists():
            return False
        log_path = root / "memory" / "updater.log"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        log = open(log_path, "ab")
        kwargs = dict(cwd=str(root), stdin=subprocess.DEVNULL,
                      stdout=log, stderr=log, close_fds=True)
        if os.name == "nt":
            kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen([sys.executable, str(script)], **kwargs)
        return True
    except Exception:
        return False
