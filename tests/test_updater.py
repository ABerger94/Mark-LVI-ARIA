"""Self-update sidecar (updater.py) + its app-side link.

The defect (Alek, 2026-10-22): she pulls new code — by hand at his
terminal, or herself through the shell tool — and nothing restarts
her, because the only restart hook lived on the self-edit path.
What is pinned here:

  * Case A (remote ahead): a clean tracked tree pulls fast-forward,
    the new code is smoke-imported first, she restarts, and a
    healthy boot stamp advances last_known_good. A dirty tracked
    tree blocks the pull (announced once per remote commit);
    untracked files never block.
  * A smoke failure after OUR pull resets the disk to the pre-pull
    commit and never restarts her. A health failure (no fresh boot
    stamp naming the new code) rolls back to last_known_good and
    restarts on that.
  * Case B (disk ahead of the boot stamp — the manual pull): she
    restarts onto the disk code, smoke-checked first; broken disk
    code leaves her running and is announced once.
  * Announce-first: awake + live session → the announcement is
    dropped for her to speak and the countdown honours the hold
    file; a held update defers and proceeds once the hold clears.
  * Diverged history is announced and left alone. The kill switch
    idles everything. The lockfile keeps one sidecar per checkout.
  * update_status is discovered, read-only in shape, and excluded
    from delegated tasks.

Git is real (temp repos, a file remote) — the pull/ancestry/reset
behaviour under test IS git's. Her process is not: pid/terminate/
launch are seams, and the launch stub writes the boot stamp a real
boot would write.
"""
import importlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
updater = importlib.import_module("updater")

GIT_ENV = {**os.environ, "GIT_CONFIG_NOSYSTEM": "1"}


def _git(cwd, *args, check=True):
    r = subprocess.run(
        ["git", "-C", str(cwd), "-c", "user.email=t@t",
         "-c", "user.name=t", *args],
        capture_output=True, text=True, env=GIT_ENV)
    if check and r.returncode != 0:
        raise AssertionError(f"git {args} failed: {r.stderr}")
    return r


def _commit_all(cwd, message):
    _git(cwd, "add", "-A")
    _git(cwd, "commit", "-q", "-m", message)
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


def _head(cwd):
    return _git(cwd, "rev-parse", "HEAD").stdout.strip()


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += float(s)


class World:
    pass


@pytest.fixture()
def world(tmp_path):
    w = World()
    w.clock = Clock()
    w.remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-b", "main",
                    str(w.remote)], check=True, capture_output=True)
    w.work = tmp_path / "work"
    subprocess.run(["git", "init", "-b", "main", str(w.work)],
                   check=True, capture_output=True)
    (w.work / "main.py").write_text("VALUE = 1\n")
    (w.work / "ui.py").write_text("UI = 1\n")
    (w.work / "core").mkdir()
    (w.work / "core" / "__init__.py").write_text("")
    (w.work / "core" / "mod.py").write_text("X = 1\n")
    (w.work / "memory").mkdir()
    (w.work / "memory" / "__init__.py").write_text("")
    (w.work / "config").mkdir()
    w.base = _commit_all(w.work, "base commit")
    _git(w.work, "remote", "add", "origin", str(w.remote))
    _git(w.work, "push", "-u", "origin", "main")
    w.write_stamp(w.base)
    return w


def _world_write_stamp(self, head, pid=999999):
    (self.work / "memory" / "boot_stamp.json").write_text(
        json.dumps({"head": head, "ts": self.clock.now(),
                    "pid": pid, "executable": sys.executable}))


World.write_stamp = _world_write_stamp


def advance_remote(w, changes, message):
    """Push a commit to the remote from a second clone."""
    pusher = w.work.parent / "pusher"
    if not pusher.exists():
        subprocess.run(["git", "clone", "-q", str(w.remote),
                        str(pusher)], check=True, capture_output=True)
    else:
        _git(pusher, "pull", "-q", "origin", "main")
    for rel, text in changes.items():
        p = pusher / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    sha = _commit_all(pusher, message)
    _git(pusher, "push", "-q", "origin", "main")
    return sha


def make_updater(w, *, launch_mode="healthy", alive=None, sleep=None,
                 **kw):
    launches = []
    terminated = []

    def launch():
        launches.append(w.clock.now())
        if launch_mode == "healthy":
            w.write_stamp(_head(w.work))
        elif launch_mode == "stale_first" and len(launches) > 1:
            w.write_stamp(_head(w.work))
        elif launch_mode == "stale_first":
            w.write_stamp(w.base)
        return 4242

    u = updater.Updater(
        w.work, now=w.clock.now, sleep=sleep or w.clock.sleep,
        alive=alive if alive is not None else (lambda pid: False),
        terminate=lambda pid: terminated.append(pid) or True,
        launch=launch,
        countdown_s=kw.pop("countdown_s", 30),
        health_grace_s=kw.pop("health_grace_s", 30), **kw)
    u.launches = launches
    u.terminated = terminated
    return u


def drop_texts(w):
    drop = w.work / "memory" / "announce_drop.jsonl"
    if not drop.is_file():
        return []
    return [json.loads(ln)["text"]
            for ln in drop.read_text().splitlines() if ln.strip()]


def write_presence(w, awake=True, session=True, pid=999999):
    (w.work / "memory" / "app_presence.json").write_text(json.dumps(
        {"awake": awake, "session": session, "pid": pid,
         "ts": w.clock.now()}))


# ── the kill switch ────────────────────────────────────────────────────

def test_kill_switch_off_is_a_noop(world):
    (world.work / "config" / "api_keys.json").write_text(
        json.dumps({"auto_update_enabled": False}))
    advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    u = make_updater(world)
    assert u.poll_once() == "disabled"
    assert _head(world.work) == world.base
    assert not (world.work / "memory" / "updater_state.json").exists()


# ── Case A: the remote is ahead ────────────────────────────────────────

def test_clean_pull_updates_and_advances_known_good(world):
    new = advance_remote(world, {"core/mod.py": "X = 2\n"},
                         "shine the mod")
    u = make_updater(world)
    assert u.poll_once() == "updated"
    assert _head(world.work) == new
    state = json.loads(
        (world.work / "memory" / "updater_state.json").read_text())
    assert state["last_known_good"]["commit"] == new
    assert state["last_seen_remote"] == new
    texts = drop_texts(world)
    assert any(t.startswith("Updated:") and "shine the mod" in t
               for t in texts)
    assert len(u.launches) == 1


def test_dirty_tracked_tree_blocks_the_pull(world):
    advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    (world.work / "main.py").write_text("VALUE = 'his edit'\n")
    u = make_updater(world)
    assert u.poll_once() == "blocked_dirty"
    assert _head(world.work) == world.base
    assert len(drop_texts(world)) == 1
    # Announced once per remote commit, not once per poll.
    assert u.poll_once() == "blocked_dirty"
    assert len(drop_texts(world)) == 1
    # Clean the tree and the same update proceeds.
    _git(world.work, "checkout", "--", "main.py")
    assert u.poll_once() == "updated"


def test_untracked_files_never_block(world):
    advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    (world.work / "scratch notes.txt").write_text("his, uncommitted")
    u = make_updater(world)
    assert u.poll_once() == "updated"


def test_smoke_failure_after_pull_restores_previous_code(world):
    advance_remote(world, {"main.py": "import core.does_not_exist\n"},
                   "break the import")
    u = make_updater(world)
    assert u.poll_once() == "smoke_failed"
    assert _head(world.work) == world.base
    assert u.launches == []          # she was never restarted
    assert any("put the previous code back" in t
               for t in drop_texts(world))


def test_health_failure_rolls_back_to_known_good(world):
    new = advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    u = make_updater(world, launch_mode="stale_first")
    assert u.poll_once() == "rolled_back"
    assert _head(world.work) == world.base
    assert len(u.launches) == 2     # the bad boot + the rollback boot
    state = json.loads(
        (world.work / "memory" / "updater_state.json").read_text())
    events = [h["event"] for h in state["history"]]
    assert "rollback" in events


def test_diverged_history_is_left_alone(world):
    advance_remote(world, {"core/mod.py": "X = 2\n"}, "remote work")
    (world.work / "ui.py").write_text("UI = 'local'\n")
    _commit_all(world.work, "local work")
    local = _head(world.work)
    u = make_updater(world)
    assert u.poll_once() == "diverged"
    assert _head(world.work) == local
    assert u.launches == []


def test_changelog_caps_at_five_subjects(world):
    pusher = world.work.parent / "pusher"
    subprocess.run(["git", "clone", "-q", str(world.remote),
                    str(pusher)], check=True, capture_output=True)
    for i in range(7):
        (pusher / "core" / "mod.py").write_text(f"X = {i + 2}\n")
        _commit_all(pusher, f"step {i + 1}")
    _git(pusher, "push", "-q", "origin", "main")
    u = make_updater(world)
    assert u.poll_once() == "updated"
    changelog = [t for t in drop_texts(world)
                 if t.startswith("Updated:")]
    assert len(changelog) == 1
    body = changelog[0]
    assert body.count("\n- ") == 6       # 5 subjects + the "and N more"
    assert "(and 2 more)" in body


# ── Case B: the disk is ahead of her boot ──────────────────────────────

def test_case_b_restarts_her_onto_the_disk_code(world):
    (world.work / "core" / "mod.py").write_text("X = 'pulled'\n")
    disk = _commit_all(world.work, "manual pull")
    u = make_updater(world)
    assert u.poll_once() == "updated"
    assert len(u.launches) == 1
    state = json.loads(
        (world.work / "memory" / "updater_state.json").read_text())
    assert state["last_known_good"]["commit"] == disk


def test_case_b_broken_disk_code_leaves_her_running(world):
    (world.work / "ui.py").write_text("import core.does_not_exist\n")
    _commit_all(world.work, "bad pull")
    u = make_updater(world)
    assert u.poll_once() == "smoke_failed"
    assert u.launches == []
    assert any("left her running on the old code" in t
               for t in drop_texts(world))


def test_no_case_b_when_boot_matches_disk(world):
    u = make_updater(world)
    assert u.poll_once() == "current"
    assert u.launches == []


# ── announce-first ─────────────────────────────────────────────────────

def test_announce_first_hold_defers_then_proceeds(world):
    new = advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    write_presence(world)
    made_hold = {"done": False}

    def sleep_then_hold(seconds):
        world.clock.sleep(seconds)
        if not made_hold["done"]:
            made_hold["done"] = True
            (world.work / "memory" / "update_hold").write_text("wait")

    u = make_updater(world, sleep=sleep_then_hold)
    assert u.poll_once() == "held"
    assert _head(world.work) == world.base
    texts = drop_texts(world)
    assert any(t.startswith("Update ready:") for t in texts)
    assert any("update held" in t for t in texts)

    (world.work / "memory" / "update_hold").unlink()
    assert u.poll_once() == "updated"
    assert _head(world.work) == new


def test_asleep_means_no_countdown_just_proceed(world):
    advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    write_presence(world, awake=False, session=True)
    u = make_updater(world)
    assert u.poll_once() == "updated"
    assert not any(t.startswith("Update ready:")
                   for t in drop_texts(world))


def test_hold_file_alone_defers_even_unannounced(world):
    advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    (world.work / "memory" / "update_hold").write_text("wait")
    u = make_updater(world)          # no presence: she's not around
    assert u.poll_once() == "held"
    assert _head(world.work) == world.base


# ── her process ────────────────────────────────────────────────────────

def test_restart_terminates_the_recorded_pid(world):
    advance_remote(world, {"core/mod.py": "X = 2\n"}, "new code")
    world.write_stamp(world.base, pid=987654)
    u = make_updater(world, alive=lambda pid: pid == 987654)
    assert u.poll_once() == "updated"
    assert u.terminated == [987654]


# ── the lock ───────────────────────────────────────────────────────────

def test_single_instance_lock(world):
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        lock = world.work / "memory" / "updater.lock"
        lock.write_text(json.dumps({"pid": sleeper.pid, "ts": 1}))
        u = updater.Updater(world.work)      # real liveness checks
        assert u.acquire_lock() is False
        sleeper.terminate()
        sleeper.wait(timeout=10)
        assert u.acquire_lock() is True      # a stale lock is taken
        u.release_lock()
        assert not lock.exists()
    finally:
        if sleeper.poll() is None:
            sleeper.kill()


# ── the smoke import's own rules ───────────────────────────────────────

def test_watched_set_membership():
    assert updater.is_watched_py("main.py")
    assert updater.is_watched_py("ui.py")
    assert updater.is_watched_py("core/selfedit.py")
    assert updater.is_watched_py("actions/run_shell.py")
    assert not updater.is_watched_py("updater.py")
    assert not updater.is_watched_py("tests/test_updater.py")
    assert not updater.is_watched_py("core/__pycache__/x.py")
    assert not updater.is_watched_py("readme.md")


def test_smoke_import_missing_third_party_is_not_broken(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("")
    (tmp_path / "core" / "mod.py").write_text(
        "import definitely_not_a_real_package_xyz\n")
    assert updater.smoke_import(tmp_path, ["core/mod.py"]) == []


def test_smoke_import_missing_own_module_is_broken(tmp_path):
    (tmp_path / "core").mkdir()
    (tmp_path / "core" / "__init__.py").write_text("")
    (tmp_path / "core" / "mod.py").write_text(
        "import core.not_here\n")
    failed = updater.smoke_import(tmp_path, ["core/mod.py"])
    assert len(failed) == 1 and failed[0][0] == "core/mod.py"


# ── the app-side link ──────────────────────────────────────────────────

def test_boot_stamp_names_the_boot(world):
    from core import updater_link
    stamp = updater_link.write_boot_stamp(world.work)
    assert stamp["head"] == world.base
    assert stamp["pid"] == os.getpid()
    on_disk = json.loads(
        (world.work / "memory" / "boot_stamp.json").read_text())
    assert on_disk["head"] == world.base


def test_announce_drain_respects_the_say_hook(world):
    from core import updater_link
    drop = world.work / "memory" / "announce_drop.jsonl"
    drop.write_text(
        json.dumps({"ts": 1, "text": "first"}) + "\n"
        + json.dumps({"ts": 2, "text": "second"}) + "\n")
    heard = []

    def say(text):
        heard.append(text)
        return len(heard) < 2        # accept one, refuse the second

    assert updater_link.drain_announcements(world.work, say) == 1
    assert heard == ["first", "second"]
    remaining = drop.read_text()
    assert "second" in remaining and "first" not in remaining
    assert updater_link.drain_announcements(
        world.work, lambda t: True) == 1
    assert not drop.exists()


# ── the model surface ──────────────────────────────────────────────────

def test_update_status_tool_shape_and_exclusion():
    import main
    import actions.update_status as us

    assert us.TOOL["name"] == "update_status"
    assert us.TOOL["handler"] is us.update_status
    assert "update_status" in main.JarvisLive._DELEGATED_EXCLUDE

    from core.action_loader import discover_actions
    reserved = {t["name"] for t in main.TOOL_DECLARATIONS}
    actions = discover_actions(ROOT / "actions",
                               reserved_names=reserved,
                               logger=lambda msg: None)
    assert actions.has("update_status")


def test_update_status_render_reads_the_record():
    import actions.update_status as us
    state = {
        "last_seen_remote": "a" * 40,
        "last_known_good": {"commit": "b" * 40},
        "pending": None,
        "history": [{"event": "updated", "detail": "remote: x -> y"}],
    }
    stamp = {"head": "c" * 40}
    text = us.render_status(state, stamp, "c" * 40, True)
    assert "on." in text
    assert "Last known-good" in text
    assert "Most recent updater event: updated" in text
    # A sidecar that never ran still gets a plain answer.
    text = us.render_status(None, None, None, False)
    assert "OFF" in text and "no record yet" in text
