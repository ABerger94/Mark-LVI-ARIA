"""
updater.py — the self-update sidecar. She pulls; this restarts her.

THE DEFECT THIS FIXES (Alek, 2026-10-22)
    "When I have her git pull her newest code from the GitHub repo,
    she doesn't restart when the new files are pulled." The only
    restart hook in the app lived on the self-edit path: she edits a
    file, her watcher notices, she restarts. A `git pull` — run by
    Alek at a terminal, or by ARIA herself through the shell tool —
    changes the same files on disk and nothing notices. The new code
    sat there until somebody restarted her by hand.

    This sidecar is the watcher for that case. It is a STANDALONE
    process on purpose: it must work when the app is broken, wedged,
    or down, so it imports nothing from the app — stdlib and the git
    CLI only. The app spawns it detached at startup; it survives her
    restarts (that is the point), and a lockfile keeps exactly one
    of it alive.

WHAT IT DOES, EVERY ~60 SECONDS
    1. Kill switch: "auto_update_enabled": false in
       config/api_keys.json means idle — checked every poll, so the
       switch works without restarting anything.
    2. Fetch origin. Two cases can mean "new code should be live":
         Case A — the remote's main is AHEAD of the disk (a commit
                  she hasn't pulled). Gate: the tracked working tree
                  must be clean. Dirty → announce once per remote
                  commit and wait; never force, never stash.
         Case B — the disk is ahead of the code she BOOTED with
                  (memory/boot_stamp.json, written by the app at
                  startup): somebody — Alek, or ARIA's own shell
                  tool — already pulled under her.
       A diverged history (neither side an ancestor of the other)
       is announced and left alone. Fast-forward is the only road.
    3. Smoke-import the new code in a subprocess BEFORE she is
       restarted on it — the same check her self-edit gate runs
       (core/selfedit.py's _smoke_import_changed, mirrored here
       because this file may not import hers): every changed watched
       .py imported with parent packages stubbed; a failure that is
       solely a missing third-party dependency is an environment
       gap, not broken code, and does not block.
    4. ANNOUNCE-FIRST (Alek's pick): if she is awake with a live
       session (memory/app_presence.json, refreshed by the app),
       the announcement is dropped for her to speak — "Update
       ready: …. Restarting in 60 seconds — say wait to hold." —
       and the countdown honours memory/update_hold: if the hold
       file appears, the update defers and re-announces later. If
       she is asleep, down, or unreachable, there is nobody to
       announce to and the update proceeds.
    5. Restart her: terminate her process (the pid from HER OWN
       stamp/presence files — files she wrote into this checkout's
       memory/, which is the directory match; the command line is
       verified where the platform allows it, and a mismatch aborts
       the kill, never guesses) and relaunch main.py detached with
       the interpreter she booted with.
    6. Health check: the new process must write a fresh boot stamp
       naming the new HEAD within the grace window. Healthy →
       last_known_good advances and she is queued a plain-English
       changelog from the pulled commits' subject lines. Unhealthy
       → roll back (git reset --hard to last_known_good — a commit,
       never a stash of anyone's work — and only ever after a pull
       this sidecar performed or a Case-B restart it triggered),
       restart again, and announce what happened.

    When she is DOWN and an update is pending, steps 3–6 double as
    the recovery: pull, verify, launch. That is all — the in-process
    watchdog owns wedges of a RUNNING app; this is not a general
    supervisor and it never relaunches a down app that has no
    update waiting.

THE RECORD
    memory/updater_state.json (atomic writes): the last remote head
    seen, last_known_good, the boot head at sidecar start, any
    in-flight/announced update, and a capped history of what it did
    and why. update_status (actions/update_status.py) reads it back
    to the model. Announcements travel through
    memory/announce_drop.jsonl, which the app drains into the same
    pending-announce queue the scheduler speaks from
    (core/updater_link.py is the app side of that seam).
"""

from __future__ import annotations

import atexit
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

def no_window_kwargs(os_name=None):
    """Subprocess kwargs that keep a console child INVISIBLE on
    Windows. This sidecar runs detached — it has no console — so
    every console child it spawns (git on every poll, python for a
    smoke import, powershell, taskkill) would get a brand-new
    console window allocated by Windows: a black box flashing on
    the desktop for the length of the call, every poll. CREATE_NO_
    WINDOW suppresses the allocation. Empty dict elsewhere."""
    if (os_name or os.name) == "nt":
        return {"creationflags":
                getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)}
    return {}


POLL_S = 60.0
COUNTDOWN_S = 60.0
HEALTH_GRACE_S = 120.0
PRESENCE_FRESH_S = 45.0
TERM_WAIT_S = 20.0
GIT_TIMEOUT_S = 120
SMOKE_TIMEOUT_S = 90
HISTORY_CAP = 20
CHANGELOG_CAP = 5

# The watched set, mirrored from core/selfedit.py (which this file
# may not import): root scripts + these package dirs are "her code".
PACKAGE_DIRS = ("core", "actions", "memory", "providers", "dashboard",
                "plugins")
ROOT_SCRIPTS = ("main.py", "ui.py")
_OWN_TOP_LEVEL = set(PACKAGE_DIRS) | {"main", "ui"}


def base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


# ── Small file helpers (the stores' idiom: temp + os.replace) ─────────────

def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_json_atomic(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    os.replace(tmp, path)


# ── Process primitives (module-level so tests can stub the seams) ─────────

def pid_alive(pid) -> bool:
    """Cross-platform liveness, stdlib only."""
    if not pid or int(pid) <= 0:
        return False
    pid = int(pid)
    if os.name == "nt":
        try:
            import ctypes
            handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
            if handle:
                ctypes.windll.kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def pid_matches_app(pid, root: Path, runner=subprocess.run) -> bool:
    """Verify `pid` is THIS checkout's main.py — on POSITIVE
    evidence only — before anything is allowed to stop it.

    The stamp/presence files prove what the app WAS when it wrote
    them; pids get reused by the OS, and a force-kill aimed at a
    reused pid can land on a process Windows cannot live without.
    That is the 2026-10-22 incident: the old check treated an
    unanswerable query as trust, the update restart killed the
    wrong process, and the whole computer went down. A check that
    cannot answer is a NO — callers then leave her running and
    say so, which is always recoverable. A wrongful kill is not.
    """
    if os.name == "nt":
        try:
            r = runner(
                ["powershell", "-NoProfile", "-Command",
                 f"$p = Get-CimInstance Win32_Process -Filter "
                 f"\"ProcessId={int(pid)}\"; $p.ExecutablePath; "
                 f"$p.CommandLine"],
                capture_output=True, text=True, timeout=15,
                **no_window_kwargs())
            lines = [ln.strip() for ln in
                     (r.stdout or "").splitlines() if ln.strip()]
            if len(lines) < 2:
                return False
            exe_name = lines[0].replace("\\", "/").rsplit("/", 1)[-1]
            if exe_name.lower() not in ("python.exe", "pythonw.exe"):
                return False
            return "main.py" in " ".join(lines[1:])
        except Exception:
            return False
    try:
        cmdline = Path(f"/proc/{int(pid)}/cmdline").read_bytes()
        text = cmdline.replace(b"\0", b" ").decode("utf-8", "replace")
        if "main.py" not in text:
            return False
        cwd = os.readlink(f"/proc/{int(pid)}/cwd")
        return os.path.realpath(cwd) == os.path.realpath(str(root))
    except Exception:
        return False


def terminate_process(pid, root: Path, alive=pid_alive,
                      sleep=time.sleep, now=time.time) -> bool:
    """Stop `pid` — gracefully first, force only after the wait.
    Returns whether the process is gone."""
    pid = int(pid)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T"],
                           capture_output=True, timeout=30,
                           **no_window_kwargs())
        else:
            import signal
            os.kill(pid, signal.SIGTERM)
    except Exception:
        pass
    deadline = now() + TERM_WAIT_S
    while now() < deadline:
        if not alive(pid):
            return True
        sleep(1.0)
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=30,
                           **no_window_kwargs())
        else:
            import signal
            os.kill(pid, signal.SIGKILL)
    except Exception:
        pass
    deadline = now() + 10.0
    while now() < deadline:
        if not alive(pid):
            return True
        sleep(0.5)
    return not alive(pid)


def launch_app(root: Path, executable: str | None = None):
    """Relaunch main.py detached — the app's own restart spawns the
    replacement the same way (same interpreter, the script, this
    directory); detached so she outlives this sidecar and any
    terminal. Output appends to memory/updater_spawn.log, where a
    failed first boot explains itself. Returns the new pid."""
    exe = executable or sys.executable
    if os.name == "nt":
        # A GUI app relaunch wants the windowless interpreter when
        # there is one beside the recorded executable.
        cand = Path(exe).with_name("pythonw.exe")
        if cand.is_file():
            exe = str(cand)
    log_path = root / "memory" / "updater_spawn.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = open(log_path, "ab")
    kwargs = dict(cwd=str(root), stdin=subprocess.DEVNULL,
                  stdout=log, stderr=log, close_fds=True)
    if os.name == "nt":
        kwargs["creationflags"] = (subprocess.DETACHED_PROCESS
                                   | subprocess.CREATE_NEW_PROCESS_GROUP)
    else:
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([exe, str(root / "main.py")], **kwargs)
    return proc.pid


# ── The smoke import, mirrored from core/selfedit.py ──────────────────────
# Same subprocess shape as _smoke_import_changed: each module imported
# fresh with parent packages stubbed to empty __init__s with a real
# __path__, so the test exercises THE MODULE's imports — the same
# import startup performs. Keep the two in step.

_SMOKE_CODE = (
    "import sys, types, os, importlib\n"
    "mod = sys.argv[1]\n"
    "repo = os.getcwd()\n"
    "parts = mod.split('.')\n"
    "for i in range(1, len(parts)):\n"
    "    name = '.'.join(parts[:i])\n"
    "    pkg = types.ModuleType(name)\n"
    "    pkg.__path__ = [os.path.join(repo, *parts[:i])]\n"
    "    sys.modules[name] = pkg\n"
    "importlib.import_module(mod)\n"
)


def _missing_third_party_dep(stderr_tail: str) -> bool:
    m = re.search(r"ModuleNotFoundError: No module named '([^']+)'",
                  stderr_tail)
    if not m:
        return False
    return m.group(1).split(".")[0] not in _OWN_TOP_LEVEL


def is_watched_py(rel: str) -> bool:
    """The watched-set test for a repo-relative path (posix style)."""
    if "__pycache__" in rel.split("/"):
        return False
    if rel in ROOT_SCRIPTS:
        return True
    parts = rel.split("/")
    return (len(parts) >= 2 and parts[0] in PACKAGE_DIRS
            and rel.endswith(".py"))


def smoke_import(root: Path, rel_files, runner=subprocess.run):
    """Import each watched .py in a subprocess. Returns
    [(rel_path, error_tail)] for genuine failures; a failure caused
    solely by a missing third-party dependency is skipped (an
    environment gap, not broken code — her gate's rule)."""
    failed = []
    for rel in rel_files:
        if not rel.endswith(".py") or not is_watched_py(rel):
            continue
        if not (root / rel).is_file():
            continue
        mod = rel[:-3].replace("/", ".")
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        try:
            r = runner([sys.executable, "-c", _SMOKE_CODE, mod],
                       cwd=str(root), capture_output=True,
                       timeout=SMOKE_TIMEOUT_S, **no_window_kwargs())
            if r.returncode != 0:
                err = r.stderr.decode("utf-8", "replace").strip()
                lines = err.splitlines()
                tail = lines[-1][-300:] if lines else "import failed"
                if _missing_third_party_dep(tail):
                    continue
                failed.append((rel, tail))
        except Exception as e:
            failed.append((rel, str(e)[:200]))
    return failed


# ── The updater ────────────────────────────────────────────────────────────

class Updater:
    def __init__(self, root=None, *, now=time.time, sleep=time.sleep,
                 alive=None, terminate=None, launch=None, smoke=None,
                 matches=None, self_exec=None,
                 countdown_s=COUNTDOWN_S,
                 health_grace_s=HEALTH_GRACE_S, poll_s=POLL_S):
        self.root = Path(root) if root else base_dir()
        self._now = now
        self._sleep = sleep
        self._alive = alive or pid_alive
        self._matches = matches or pid_matches_app
        self._terminate = terminate or (
            lambda pid: terminate_process(pid, self.root,
                                          alive=self._alive,
                                          sleep=self._sleep,
                                          now=self._now))
        self._launch = launch or (
            lambda: launch_app(self.root,
                               self._recorded_executable()))
        self._smoke = smoke or (
            lambda rels: smoke_import(self.root, rels))
        self._self_exec = self_exec or self._exec_self
        self.countdown_s = countdown_s
        self.health_grace_s = health_grace_s
        self.poll_s = poll_s

        mem = self.root / "memory"
        self.state_path = mem / "updater_state.json"
        self.lock_path = mem / "updater.lock"
        self.stamp_path = mem / "boot_stamp.json"
        self.presence_path = mem / "app_presence.json"
        self.hold_path = mem / "update_hold"
        self.drop_path = mem / "announce_drop.jsonl"
        self.config_path = self.root / "config" / "api_keys.json"
        self.state = self._load_state()

    # ── state ──────────────────────────────────────────────────────────

    def _load_state(self) -> dict:
        data = _read_json(self.state_path) or {}
        data.setdefault("last_seen_remote", None)
        data.setdefault("last_known_good", None)
        data.setdefault("boot_head_at_start", None)
        data.setdefault("pending", None)
        data.setdefault("pending_announce", None)
        data.setdefault("announced_key", None)
        data.setdefault("held_for", None)
        data.setdefault("history", [])
        return data

    def _save_state(self) -> None:
        try:
            _write_json_atomic(self.state_path, self.state)
        except Exception:
            pass

    def _history(self, event: str, detail: str = "") -> None:
        hist = self.state.setdefault("history", [])
        hist.append({"ts": self._now(), "event": event,
                     "detail": detail})
        del hist[:-HISTORY_CAP]
        self._save_state()

    # ── config (read directly; this process imports no app code) ─────

    def enabled(self) -> bool:
        """auto_update_enabled — anything but an explicit false
        means on (the app's flag idiom)."""
        data = _read_json(self.config_path)
        if data is None:
            return True
        return bool(data.get("auto_update_enabled", True))

    # ── git ────────────────────────────────────────────────────────────

    def _git(self, args, timeout=GIT_TIMEOUT_S):
        try:
            r = subprocess.run(["git", "-C", str(self.root), *args],
                               capture_output=True, text=True,
                               encoding="utf-8", errors="replace",
                               timeout=timeout, **no_window_kwargs())
            return r.returncode, (r.stdout or "").strip(), \
                (r.stderr or "").strip()
        except Exception as e:
            return 1, "", str(e)

    def is_repo(self) -> bool:
        rc, _o, _e = self._git(["rev-parse", "--git-dir"])
        return rc == 0

    def head(self):
        rc, out, _e = self._git(["rev-parse", "HEAD"])
        return out if rc == 0 and out else None

    def tree_of(self, rev):
        rc, out, _e = self._git(["rev-parse", f"{rev}^{{tree}}"])
        return out if rc == 0 and out else None

    def fetch_remote(self):
        """Fetch origin's main and return its head sha (FETCH_HEAD),
        or None when the fetch failed. Read-only toward the working
        tree; the merge step of a pull happens only on the update
        path (merge --ff-only FETCH_HEAD — the second half of
        `git pull --ff-only`, immune to branch-name drift)."""
        rc, _o, _e = self._git(["fetch", "origin", "main"])
        if rc != 0:
            return None
        rc, out, _e = self._git(["rev-parse", "FETCH_HEAD"])
        return out if rc == 0 and out else None

    def is_ancestor(self, a, b) -> bool:
        rc, _o, _e = self._git(["merge-base", "--is-ancestor", a, b])
        return rc == 0

    def tracked_dirty(self) -> bool:
        """Any tracked file modified (staged or not). Untracked
        files — runtime state, her notes — never block an update."""
        rc, out, _e = self._git(["status", "--porcelain"])
        if rc != 0:
            return True   # cannot prove clean → treat as dirty
        for line in out.splitlines():
            if line and not line.startswith("??"):
                return True
        return False

    def subjects_between(self, a, b):
        rc, out, _e = self._git(
            ["log", "--reverse", "--format=%s", f"{a}..{b}"])
        if rc != 0:
            rc, out, _e = self._git(
                ["log", "-5", "--format=%s", b or "HEAD"])
        return [ln for ln in out.splitlines() if ln.strip()]

    def changed_py_between(self, a, b):
        rc, out, _e = self._git(
            ["diff", "--name-only", f"{a}..{b}"])
        if rc != 0:
            return []
        return [ln.strip() for ln in out.splitlines()
                if ln.strip().endswith(".py")
                and is_watched_py(ln.strip())]

    # ── her process, via her own files ─────────────────────────────────

    def _stamp(self):
        return _read_json(self.stamp_path)

    def _presence(self):
        return _read_json(self.presence_path)

    def _recorded_executable(self):
        for source in (self._presence(), self._stamp()):
            if source and source.get("executable"):
                return source["executable"]
        return None

    def app_pid(self):
        """Her pid, from the presence file (fresher) or the boot
        stamp — and only when the process is alive AND positively
        verified as this checkout's main.py. An unverifiable pid
        counts as not-her: the kill gate fails closed."""
        for source in (self._presence(), self._stamp()):
            if not source:
                continue
            pid = source.get("pid")
            if pid and self._alive(pid):
                if self._matches(pid, self.root):
                    return int(pid)
                self._history(
                    "pid_mismatch",
                    f"pid {pid} from her files could not be verified "
                    f"as this checkout's main.py — not touching it")
                return None
        return None

    def _recorded_pid_alive(self) -> bool:
        """Any pid her files name is still a live process —
        distinguishes "she is not running" (launch is the fix)
        from "she is running but unverifiable" (launching would
        start a second copy; the restart must abort instead)."""
        for source in (self._presence(), self._stamp()):
            if source:
                pid = source.get("pid")
                if pid and self._alive(pid):
                    return True
        return False

    def hes_around(self) -> bool:
        """Awake, with a live session, recently — the announce-first
        signal. The presence file is the app side's word on it
        (core/updater_link.py refreshes it every few seconds)."""
        p = self._presence()
        if not p:
            return False
        if self._now() - float(p.get("ts") or 0) > PRESENCE_FRESH_S:
            return False
        return bool(p.get("awake")) and bool(p.get("session"))

    # ── announcements ──────────────────────────────────────────────────

    def announce(self, text: str) -> None:
        """Queue a line for her to speak: the app drains
        announce_drop.jsonl into the scheduler's pending-announce
        queue. Also the pending_announce record in state."""
        try:
            self.drop_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.drop_path, "a", encoding="utf-8") as f:
                f.write(json.dumps({"ts": self._now(), "text": text})
                        + "\n")
        except Exception:
            pass
        self.state["pending_announce"] = {
            "text": text, "ts": self._now()}
        self._save_state()

    def _announce_once(self, key: str, text: str) -> None:
        if self.state.get("announced_key") != key:
            self.announce(text)
            self.state["announced_key"] = key
            self._save_state()

    def _hold_active(self) -> bool:
        return self.hold_path.exists()

    def _countdown(self) -> bool:
        """Wait out the announce-first countdown. True = held."""
        end = self._now() + self.countdown_s
        while self._now() < end:
            if self._hold_active():
                return True
            self._sleep(min(2.0, max(0.0, end - self._now())))
        return self._hold_active()

    # ── restart + health ───────────────────────────────────────────────

    def _restart_and_verify(self, expected_head) -> str:
        """"healthy" | "unhealthy" | "aborted". Terminate her (if
        running), relaunch, and require a fresh boot stamp naming
        expected_head within the grace window."""
        pid = self.app_pid()
        if pid is None and self._recorded_pid_alive():
            # She IS running — her files name a live pid — but it
            # could not be positively verified as her. Killing on
            # a guess is how the 2026-10-22 restart took the whole
            # computer down, and relaunching would run two of her.
            # Abort: the new code is on disk; a human restart picks
            # it up. Announced, recorded, recoverable.
            self._history(
                "restart_aborted_unverified",
                "recorded pid is alive but unverifiable as her "
                "main.py — restart left to a human")
            self.announce(
                "I pulled the update and the code is on disk, but "
                "I couldn't verify the running process is really "
                "her, so I left her running rather than risk "
                "stopping the wrong thing. Restart her yourself "
                "when you're ready and she'll come up on the new "
                "code.")
            return "aborted"
        if pid is not None:
            if not self._terminate(pid):
                self._history("terminate_failed",
                              f"pid {pid} would not stop")
                return "aborted"
        restart_ts = self._now()
        try:
            self._launch()
        except Exception as e:
            self._history("launch_failed", str(e)[:200])
            return "unhealthy"
        deadline = restart_ts + self.health_grace_s
        while self._now() < deadline:
            stamp = self._stamp()
            if stamp and stamp.get("head") == expected_head \
                    and float(stamp.get("ts") or 0) >= restart_ts - 2:
                return "healthy"
            self._sleep(2.0)
        return "unhealthy"

    def _rollback(self, reason: str, fallback) -> str:
        """Reset the disk to last_known_good and restart her on it.
        Only ever called after a pull this sidecar performed or a
        Case-B restart it triggered (see _update_flow)."""
        lkg = (self.state.get("last_known_good") or {}).get("commit") \
            or fallback
        self._git(["reset", "--hard", lkg])
        self._history("rollback", f"{reason}; reset to {lkg[:12]}")
        self.announce(
            "That update didn't come up clean, so I rolled back to "
            "the last version that booted healthy and restarted her "
            "on it.")
        verdict = self._restart_and_verify(lkg)
        if verdict == "healthy":
            self.state["boot_head_at_start"] = lkg
        return verdict

    # ── the update flow ────────────────────────────────────────────────

    def _update_flow(self, kind: str, from_head: str, to_head: str,
                     changed_py) -> str:
        subjects = self.subjects_between(from_head, to_head)
        first_line = subjects[0] if subjects else "new code"

        if kind == "disk":
            # The code is already on disk: smoke-check BEFORE
            # restarting her onto it. A failure leaves her running
            # on the old code in memory — today's behaviour, but now
            # with a record and a word to Alek.
            failures = self._smoke(changed_py)
            if failures:
                detail = "; ".join(f"{p}: {e}" for p, e in failures[:3])
                self._history("smoke_failed_disk", detail[:400])
                self._announce_once(
                    f"smoke:{to_head}",
                    "The code that was pulled in doesn't import "
                    "cleanly, so I left her running on the old code. "
                    f"First problem: {failures[0][0]}.")
                return "smoke_failed"

        self.state["pending"] = {
            "kind": kind, "from": from_head, "to": to_head,
            "since": self._now()}
        self._save_state()

        # Announce-first (pick 3a).
        if self.state.get("held_for") == to_head \
                and self._hold_active():
            self.state["pending"] = None
            self._save_state()
            return "held"
        if self.hes_around():
            self.announce(
                f"Update ready: {first_line}. Restarting in "
                f"{int(self.countdown_s)} seconds — say wait to "
                f"hold.")
            if self._countdown():
                self.state["held_for"] = to_head
                self.state["pending"] = None
                self._history("held", f"update to {to_head[:12]} "
                                       f"held by request")
                self.announce("Okay — update held. I'll bring it "
                              "back later.")
                self._save_state()
                return "held"
        elif self._hold_active():
            # Nobody to announce to, but a hold is a hold.
            self.state["held_for"] = to_head
            self.state["pending"] = None
            self._save_state()
            return "held"

        if kind == "remote":
            rc, _o, err = self._git(["merge", "--ff-only",
                                     "FETCH_HEAD"])
            if rc != 0 or self.head() != to_head:
                self._history("pull_failed", err[:300])
                self.state["pending"] = None
                self._save_state()
                return "pull_failed"
            self._history("pulled", f"{from_head[:12]} -> "
                                     f"{to_head[:12]}")
            failures = self._smoke(changed_py)
            if failures:
                detail = "; ".join(
                    f"{p}: {e}" for p, e in failures[:3])
                self._history("smoke_failed_pull", detail[:400])
                # Undo ONLY the pull this sidecar just performed —
                # the tree was verified clean before it.
                self._git(["reset", "--hard", from_head])
                self.announce(
                    "I pulled an update, but the new code doesn't "
                    "import cleanly, so I put the previous code back. "
                    f"First problem: {failures[0][0]}. She is still "
                    "running the version she had.")
                self.state["pending"] = None
                self._save_state()
                return "smoke_failed"

        verdict = self._restart_and_verify(to_head)
        if verdict == "aborted":
            self._history("restart_aborted",
                          "could not stop her process safely; code "
                          "is in place, restart left to a human")
            self.state["pending"] = None
            self._save_state()
            return "restart_aborted"
        if verdict != "healthy":
            result = self._rollback(
                f"new code did not boot healthy ({kind})", from_head)
            self.state["pending"] = None
            self._save_state()
            return "rolled_back" if result == "healthy" \
                else "rollback_unhealthy"

        self.state["last_known_good"] = {
            "commit": to_head, "tree": self.tree_of(to_head)}
        self.state["boot_head_at_start"] = to_head
        self.state["held_for"] = None
        self.state["announced_key"] = None
        self.state["pending"] = None
        self._history("updated",
                      f"{kind}: {from_head[:12]} -> {to_head[:12]}")
        if subjects:
            lines = subjects[:CHANGELOG_CAP]
            text = "Updated:\n" + "\n".join(f"- {s}" for s in lines)
            if len(subjects) > CHANGELOG_CAP:
                text += f"\n- (and {len(subjects) - CHANGELOG_CAP} " \
                        f"more)"
            self.announce(text)
        self._save_state()
        if self._self_changed(from_head, to_head):
            self._self_exec()
        return "updated"

    def _self_changed(self, from_head, to_head) -> bool:
        """updater.py is not in the watched set (root scripts are
        main.py/ui.py only), so the self-change check diffs by name."""
        rc, out, _e = self._git(
            ["diff", "--name-only", f"{from_head}..{to_head}"])
        return rc == 0 and "updater.py" in out.splitlines()

    # ── one poll ───────────────────────────────────────────────────────

    def poll_once(self) -> str:
        if not self.enabled():
            return "disabled"
        if not self.is_repo():
            return "not_a_repo"
        disk = self.head()
        if disk is None:
            return "no_head"
        stamp = self._stamp()
        boot = stamp.get("head") if stamp else None
        if self.state.get("boot_head_at_start") is None and boot:
            self.state["boot_head_at_start"] = boot
        if self.state.get("last_known_good") is None:
            # Whatever she booted with when this sidecar first
            # looked is, by evidence, a version that runs.
            base = boot if boot and boot != "unknown" else disk
            self.state["last_known_good"] = {
                "commit": base, "tree": self.tree_of(base)}
        self._save_state()

        remote = self.fetch_remote()
        if remote is None:
            self._history("fetch_failed", "origin unreachable")
            return "fetch_failed"
        self.state["last_seen_remote"] = remote
        self._save_state()

        if remote != disk:
            if self.is_ancestor(disk, remote):
                # Case A — the remote is ahead of the disk.
                if self.tracked_dirty():
                    self._announce_once(
                        f"dirty:{remote}",
                        "An update is ready, but the app folder "
                        "has uncommitted changes, so I won't pull "
                        "over them. Commit or set them aside and "
                        "I'll take it from there.")
                    self._history("blocked_dirty",
                                  f"remote {remote[:12]} waiting")
                    return "blocked_dirty"
                changed = self.changed_py_between(disk, remote)
                return self._update_flow("remote", disk, remote,
                                          changed)
            if not self.is_ancestor(remote, disk):
                self._announce_once(
                    f"diverged:{remote}",
                    "The local code and GitHub have diverged — "
                    "different commits on both sides. I won't touch "
                    "that; it needs a human merge.")
                self._history("diverged",
                              f"disk {disk[:12]} vs remote "
                              f"{remote[:12]}")
                return "diverged"
            # Remote is behind the disk: the disk-ahead case below
            # decides (Case B), keyed on the boot stamp.

        # Case B — the disk is ahead of the code she booted with.
        if boot and boot != "unknown" and disk != boot:
            changed = self.changed_py_between(boot, disk)
            return self._update_flow("disk", boot, disk, changed)
        return "current"

    # ── lock + main loop ───────────────────────────────────────────────

    def acquire_lock(self) -> bool:
        try:
            if self.lock_path.is_file():
                data = _read_json(self.lock_path) or {}
                other = data.get("pid")
                if other and int(other) != os.getpid() \
                        and self._alive(other):
                    return False
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json_atomic(self.lock_path,
                               {"pid": os.getpid(), "ts": self._now()})
            return True
        except Exception:
            return False

    def release_lock(self) -> None:
        try:
            data = _read_json(self.lock_path) or {}
            if int(data.get("pid") or 0) == os.getpid():
                self.lock_path.unlink(missing_ok=True)
        except Exception:
            pass

    def _exec_self(self) -> None:
        """Re-exec after an update changed updater.py itself: the
        running sidecar is the old code until it does this. Same
        pid, so the lock stays valid."""
        try:
            os.execv(sys.executable,
                     [sys.executable,
                      str(Path(__file__).resolve())])
        except Exception:
            pass

    def run_forever(self) -> None:
        while True:
            try:
                self.poll_once()
            except Exception as e:
                try:
                    self._history("poll_error", str(e)[:300])
                except Exception:
                    pass
            self._sleep(self.poll_s)


def main() -> int:
    updater = Updater()
    if not updater.acquire_lock():
        # Another sidecar owns this checkout. Exit quietly — the
        # app spawns one per launch by design.
        return 0
    atexit.register(updater.release_lock)
    print(f"[Updater] watching {updater.root} "
          f"(poll {int(updater.poll_s)}s)")
    updater.run_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
