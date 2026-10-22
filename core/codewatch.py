"""
core/codewatch.py — she watches her own code, from inside the process.

THE PORT (2026-10-22)

    This is the old ARIA's watcher (aria/agent/brain.py: the boot
    hash snapshot, the post-turn check, _maybe_restart_after_self_
    edit) carried into this tree, replacing BOTH the Phase D
    SelfEditWatcher wiring in main.py and the self-update sidecar
    (updater.py + core/updater_link.py, retired the same day).

    Why in-process, stated once: the sidecar was a second process
    that polled git every minute, guessed which pid was her, and on
    2026-10-22 its restart killed a process it could not positively
    identify and rebooted Alek's PC. The old design has no such
    failure mode because it never kills anything: the watcher lives
    in her process, notices her files changed, and SHE relaunches
    herself — same interpreter, same script — and exits. There is
    no pid to misidentify because the only process she can restart
    is the one she is.

THE INVARIANTS (ARIA's, preserved)

    * At boot, SHA-256 hashes of the whole watched set are
      snapshotted (core/selfedit.py's watched set: main.py, ui.py,
      every .py under core/, actions/, memory/, providers/,
      dashboard/, plugins/ — plus core/prompt.txt, hashed like
      everything else but never compiled or imported).
    * After every completed turn, the set is re-hashed and diffed
      against the boot snapshot. Nothing polls in the background;
      a change is noticed at the next turn boundary, whether it
      came from her edit tools, a `git pull`, or a hand edit.
    * Changed .py files are validated before anything restarts:
      py_compile, then a subprocess smoke import (selfedit's —
      parent packages stubbed; a failure caused ONLY by a missing
      third-party module is an environment gap and is exempt).
    * A changed file that fails validation is restored from git,
      the broken copy kept beside it as <file>.broken-<epoch>, the
      restore is announced, and the restart loads the restored
      tree. A file whose restore FAILS is announced as broken and
      nothing restarts into it.
    * All valid: she announces "My code changed — restarting now
      to load the new version.", waits for speech/playback to
      drain (bounded at 120 s, ARIA's bound), then the restart
      callback main.py supplies relaunches this process and exits.
    * Deleted watched files are skipped — the running process
      still holds the module in memory, and restarting into a
      tree with a missing module would crash the new process.

THE SHARED RESTART GUARD

    RestartArbiter is the one arm-gate every restart path in the
    process consults: this watcher arms through it, and main.py's
    watchdog restart checks it before relaunching. First armer
    wins; a second arm attempt is refused, so the watcher, the
    self-edit flow (which IS this watcher), and the watchdog can
    never race two relaunches in the same process. PROCESS_ARBITER
    is the process-wide instance.

THE LEDGER

    The watcher keeps a small record at memory/codewatch.json:
    the boot record (when, which HEAD, how many files watched),
    the last check time and count, and the most recent events
    (change / restored / restore_failed / restart_armed /
    restart_suppressed), capped at 20. actions/update_status.py
    reads it — that file is the whole contract between the
    watcher and the tool that reports on it. Writing it never
    raises: a broken ledger costs the report, never the check.
"""

from __future__ import annotations

import json
import os
import py_compile
import subprocess
import threading
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from core import selfedit as _selfedit

try:
    from core import incidents
except Exception:  # pragma: no cover — incidents must never block a check
    incidents = None


EVENTS_CAP = 20
LEDGER_PARTS = ("memory", "codewatch.json")
# ARIA's bound: the restart waits for speech to drain, but not forever.
DRAIN_TIMEOUT_S = 120.0


# ── The shared restart guard ────────────────────────────────────────────────

class RestartArbiter:
    """First-arm-wins gate for every restart path in the process.

    try_arm(source) returns True exactly once per process (until
    reset); whoever armed first owns the coming relaunch, and any
    other path that reaches for a restart finds the gate closed
    and stands down. The watcher arms here; main.py's watchdog
    restart checks here. Thread-safe: the watcher arms from a
    worker thread, the watchdog from its supervisor thread.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._armed_by: Optional[str] = None

    def try_arm(self, source: str) -> bool:
        with self._lock:
            if self._armed_by is not None:
                return False
            self._armed_by = source
            return True

    @property
    def armed_by(self) -> Optional[str]:
        with self._lock:
            return self._armed_by

    def reset(self) -> None:
        """Test seam — a live process never un-arms a restart."""
        with self._lock:
            self._armed_by = None


# The process-wide instance. main.py imports this (via the module)
# for the watchdog restart; the watcher uses it by default.
PROCESS_ARBITER = RestartArbiter()


# ── Validation (selfedit's machinery, composed ARIA's way) ──────────────────

def _validate_changed(changed: List[str], root: Path) -> List[Tuple[str, str]]:
    """(path, reason) for every changed file that would crash a
    restart. Compile first; only when everything parses, smoke-
    import through core/selfedit.py (which carries the missing-
    third-party-dependency exemption). Hash-only files — the
    persona file — are in `changed` but are neither compiled nor
    imported; the smoke import skips non-.py paths itself."""
    bad: List[Tuple[str, str]] = []
    for p in changed:
        if p.endswith(".py") and os.path.isfile(p):
            try:
                py_compile.compile(p, doraise=True)
            except Exception as e:
                bad.append((p, f"syntax error: {e}"))
    if not bad:
        for p, err in _selfedit._smoke_import_changed(changed, root):
            bad.append((p, f"import failed: {err}"))
    return bad


def _git_head(root: Path) -> Optional[str]:
    """The checkout's HEAD sha, or None — a boot record that
    cannot name its code says nothing rather than guessing."""
    try:
        r = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=15)
        sha = (r.stdout or "").strip()
        if r.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return None


# ── The watcher ─────────────────────────────────────────────────────────────

class CodeWatcher:
    """Boot-snapshot hash watcher. main.py builds one at startup
    (the snapshot is taken in the constructor — the tree as the
    process found it) and calls check_after_turn() after every
    completed turn, from the one receive-loop choke point both
    engines share. run() calls mark_boot() to stamp the ledger.

    Callbacks and seams keep this class testable and keep main.py's
    lifecycle where it belongs:

        announce(text)   — say it in her voice (main.py's speak)
        restart()        — perform the relaunch (main.py supplies
                           it; spawn-replacement-then-exit, the
                           self-edit restart's existing shape)
        speech_idle()    — True when nothing is being spoken or
                           played, so the restart waits for her to
                           finish the sentence (ARIA waited on her
                           speech queues; this is the same wait)
        snapshot_fn / validate_fn / restore_fn / sleep_fn /
        arbiter / drain_timeout / poll_interval / settle_delay —
        test seams, defaulting to the real machinery.
    """

    def __init__(self,
                 root: Optional[Path] = None,
                 announce: Optional[Callable[[str], None]] = None,
                 restart: Optional[Callable[[], None]] = None,
                 speech_idle: Optional[Callable[[], bool]] = None,
                 arbiter: Optional[RestartArbiter] = None,
                 snapshot_fn: Optional[Callable[[], Dict[str, Optional[str]]]] = None,
                 validate_fn: Optional[Callable[[List[str], Path], List[Tuple[str, str]]]] = None,
                 restore_fn: Optional[Callable[[str], Optional[str]]] = None,
                 sleep_fn: Optional[Callable[[float], None]] = None,
                 poll_interval: float = 0.5,
                 drain_timeout: float = DRAIN_TIMEOUT_S,
                 settle_delay: float = 2.0,
                 ledger_path: Optional[Path] = None):
        self.root = Path(root) if root else _selfedit._base_dir()
        self._announce_cb = announce or (lambda text: None)
        self._restart = restart or (lambda: None)
        self._speech_idle = speech_idle
        self._arbiter = arbiter if arbiter is not None else PROCESS_ARBITER
        self._snapshot_fn = snapshot_fn or (
            lambda: _selfedit.snapshot_hashes(self.root))
        self._validate_fn = validate_fn or _validate_changed
        self._restore_fn = restore_fn or (
            lambda p: _selfedit._restore_last_committed(p, self.root))
        self._sleep = sleep_fn or time.sleep
        self._poll_interval = poll_interval
        self._drain_timeout = drain_timeout
        self._settle_delay = settle_delay
        self._ledger_path = Path(ledger_path) if ledger_path else \
            self.root.joinpath(*LEDGER_PARTS)

        self._boot = self._snapshot_fn()
        self._lock = threading.Lock()
        self._checking = False
        self._restart_pending = False
        self._waiter: Optional[threading.Thread] = None

        self._boot_record: Optional[dict] = None
        self._last_check_ts: Optional[float] = None
        self._checks = 0
        self._events: List[dict] = []

    # -- the ledger ----------------------------------------------------------

    def mark_boot(self) -> None:
        """Stamp the boot record into the ledger: when she booted,
        which code, how many files the watcher holds hashes for.
        Called once from main.py's run() (where the retired
        sidecar's boot stamp used to be written), guarded there —
        and never raises from here either."""
        self._boot_record = {
            "ts": time.time(),
            "head": _git_head(self.root),
            "watched": len(self._boot),
        }
        self._ledger_event("boot",
                           f"watching {len(self._boot)} files")
        self._persist_ledger()

    def _ledger_event(self, kind: str, detail: str) -> None:
        self._events.append(
            {"ts": time.time(), "kind": kind, "detail": detail})
        del self._events[:-EVENTS_CAP]
        self._persist_ledger()

    def _persist_ledger(self) -> None:
        try:
            data = {
                "boot": self._boot_record,
                "last_check_ts": self._last_check_ts,
                "checks": self._checks,
                "restart_pending": self._restart_pending,
                "armed_by": self._arbiter.armed_by,
                "events": list(self._events),
            }
            self._ledger_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._ledger_path.with_name(
                self._ledger_path.name + ".tmp")
            tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
            os.replace(tmp, self._ledger_path)
        except Exception:
            pass

    def status(self) -> dict:
        """The ledger, as data — what update_status renders from
        the file, available in-process for tests and diagnostics."""
        return {
            "boot": self._boot_record,
            "last_check_ts": self._last_check_ts,
            "checks": self._checks,
            "restart_pending": self._restart_pending,
            "armed_by": self._arbiter.armed_by,
            "events": list(self._events),
        }

    # -- internals -------------------------------------------------------------

    def _record(self, kind: str, message: str) -> None:
        if incidents is not None:
            incidents.record("codewatch", kind, message)

    def _arm_restart(self) -> None:
        """Arm the one restart. The shared arbiter decides: if any
        path already armed a restart in this process, this arm is
        refused and no second waiter starts — the process is
        already going away on the first armer's schedule."""
        self._restart_pending = True
        if not self._arbiter.try_arm("codewatch"):
            print(f"[CodeWatch] restart arm refused — already armed "
                  f"by {self._arbiter.armed_by}; standing down.")
            self._ledger_event(
                "restart_suppressed",
                f"a restart was already armed by "
                f"{self._arbiter.armed_by}")
            self._persist_ledger()
            return
        self._ledger_event("restart_armed",
                           "relaunching on the new code")
        self._waiter = threading.Thread(
            target=self._wait_and_restart, daemon=True,
            name="codewatch-restart")
        self._waiter.start()

    def _wait_and_restart(self) -> None:
        """ARIA's waiter: give speech/playback up to the drain
        bound to finish, settle briefly, then restart. Runs on its
        own thread — the turn that armed it is long over."""
        if self._speech_idle is not None:
            deadline = time.monotonic() + self._drain_timeout
            while time.monotonic() < deadline:
                try:
                    if self._speech_idle():
                        break
                except Exception:
                    break
                self._sleep(self._poll_interval)
        self._sleep(self._settle_delay)
        try:
            self._restart()
        except Exception as e:
            print(f"[CodeWatch] restart callback failed: {e}")

    # -- the check -------------------------------------------------------------

    def check_after_turn(self, say_fn: Optional[Callable[[str], None]] = None) -> str:
        """One post-turn check — the turn-end hook's entry point
        (old ARIA called hers with the turn's say function; the
        bound announce callback is the default). Returns a short
        status string: "clean" | "restart_armed" |
        "restored_restart_armed" | "restore_failed" | "busy" |
        "restart_pending". Never raises — it runs alongside the
        conversation's receive loop."""
        announce = say_fn or self._announce_cb
        with self._lock:
            if self._checking:
                return "busy"
            if self._restart_pending:
                return "restart_pending"
            self._checking = True
        try:
            return self._check_inner(announce)
        except Exception as e:
            print(f"[CodeWatch] check failed: {e}")
            return "clean"
        finally:
            with self._lock:
                self._checking = False
            self._last_check_ts = time.time()
            self._checks += 1
            self._persist_ledger()

    def check(self) -> str:
        """Alias for check_after_turn() with the bound announce —
        the name the Phase D watcher used, kept for symmetry."""
        return self.check_after_turn()

    def _check_inner(self, announce: Callable[[str], None]) -> str:
        cur = self._snapshot_fn()
        # Deleted files are skipped (absent from cur): the running
        # process still holds the module in memory. Restarting
        # into a tree with a missing module would just crash the
        # new process at import.
        changed = [p for p in cur if cur[p] != self._boot.get(p)]
        if not changed:
            return "clean"

        names = ", ".join(os.path.basename(p) for p in changed)
        self._ledger_event("change", f"changed: {names}")

        bad = self._validate_fn(changed, self.root)
        for p, reason in bad:
            print(f"[CodeWatch] restart blocked: {reason} in "
                  f"{os.path.basename(p)}")
            restored = self._restore_fn(p)
            if not restored:
                self._record(
                    "code_restore_failed",
                    f"{os.path.basename(p)}: {reason}; restore failed")
                self._ledger_event(
                    "restore_failed",
                    f"{os.path.basename(p)}: {reason}")
                announce(
                    f"My code changed but the new version is broken "
                    f"({reason}).")
                return "restore_failed"
            self._record(
                "code_broken_restored",
                f"{os.path.basename(p)}: {reason}; restored from git, "
                f"broken copy kept as {os.path.basename(restored)}")
            self._ledger_event(
                "restored",
                f"{os.path.basename(p)} restored from git "
                f"({reason})")
            print(f"[CodeWatch] restored {os.path.basename(p)} from "
                  f"git, broken copy kept as {restored}")

        if bad:
            restored_names = ", ".join(
                os.path.basename(p) for p, _ in bad)
            announce(
                f"My code changed but the new version was broken — "
                f"I restored the last working {restored_names} and "
                f"restarting.")

        announce(
            "My code changed — restarting now to load the new version.")
        print("[CodeWatch] restart armed.")
        self._arm_restart()
        return "restored_restart_armed" if bad else "restart_armed"
