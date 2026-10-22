"""Phase R3 — the off-machine backup mirror (core/backup_mirror.py).

What is being pinned shut:

  * a fresh snapshot in either store's rotation is copied to the
    mirror folder, byte for byte, under the store's own filename;
  * with no mirror set, nothing changes anywhere — the rotations
    behave exactly as they did before the mirror existed;
  * an unwritable mirror never fails the save or the backup: the
    write lands, the on-machine rotation lands, ONE incident is
    recorded per process, and later failures stay silent;
  * the tool: setting a folder mirrors the current rotations
    immediately and the setting verifies by re-read; an unwritable
    first copy answers unverified, not done; clearing verifies;
  * the config accessors round-trip and default to unset.
"""
import json

import pytest

from actions import set_backup_mirror as sbm
from core import backup_mirror, incidents
from memory import config_manager as cm
from memory import memory_manager as mm


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "config" / "api_keys.json"
    mem = tmp_path / "memory" / "long_term.json"
    cfg.parent.mkdir(parents=True)
    mem.parent.mkdir(parents=True)
    monkeypatch.setattr(cm, "CONFIG_FILE", cfg)
    monkeypatch.setattr(mm, "MEMORY_PATH", mem)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    monkeypatch.setattr(cm, "_recovery_note", None)
    monkeypatch.setattr(mm, "_recovery_note", None)
    backup_mirror.reset_latch_for_tests()
    return {"cfg": cfg, "mem": mem, "mirror": tmp_path / "mirror",
            "inc": tmp_path / "incidents.jsonl", "root": tmp_path}


def _kinds(path):
    if not path.exists():
        return []
    return [json.loads(ln)["kind"]
            for ln in path.read_text(encoding="utf-8").splitlines()
            if ln]


def _write_memory_twice():
    store = mm._empty_memory()
    mm._write_store(store)
    store["identity"]["x"] = {"value": "y"}
    mm._write_store(store)      # this write snapshots the first


def _write_config_twice():
    cm.save_api_keys("key-one-1234567890")
    cm.save_api_keys("key-two-1234567890")


# ── accessors ───────────────────────────────────────────────────────────

def test_accessors_default_unset_and_round_trip(env):
    assert cm.get_backup_mirror_dir() == ""
    cm.save_backup_mirror_dir("  /somewhere/backups  ")
    assert cm.get_backup_mirror_dir() == "/somewhere/backups"
    cm.save_backup_mirror_dir("")
    assert cm.get_backup_mirror_dir() == ""


# ── mirroring on snapshot ───────────────────────────────────────────────

def test_memory_snapshot_is_mirrored(env):
    cm.save_backup_mirror_dir(str(env["mirror"]))
    _write_memory_twice()
    slot = mm._backup_slots()[0]
    assert slot.exists()
    mirrored = env["mirror"] / slot.name
    assert mirrored.exists()
    assert mirrored.read_bytes() == slot.read_bytes()


def test_config_snapshot_is_mirrored(env):
    cm.save_backup_mirror_dir(str(env["mirror"]))
    _write_config_twice()
    slot = cm._backup_slots()[0]
    assert slot.exists()
    mirrored = env["mirror"] / slot.name
    assert mirrored.exists()
    assert mirrored.read_bytes() == slot.read_bytes()


def test_unset_mirror_changes_nothing(env):
    _write_memory_twice()
    _write_config_twice()
    assert mm._backup_slots()[0].exists()
    assert cm._backup_slots()[0].exists()
    assert not env["mirror"].exists()      # never created


# ── failure discipline ──────────────────────────────────────────────────

def test_unwritable_mirror_never_fails_the_save(env):
    blocker = env["root"] / "a-file-not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    cm.save_backup_mirror_dir(str(blocker / "mirror"))
    _write_memory_twice()                  # must not raise
    _write_config_twice()                  # must not raise
    # the on-machine rotations are intact
    assert mm._backup_slots()[0].exists()
    assert cm._backup_slots()[0].exists()
    # exactly one incident across all those failures
    assert _kinds(env["inc"]).count("backup_mirror_failed") == 1


# ── the tool ────────────────────────────────────────────────────────────

def test_tool_sets_mirrors_now_and_verifies(env):
    _write_memory_twice()
    out = sbm.set_backup_mirror({"path": str(env["mirror"])})
    assert out.startswith("status=done")
    assert cm.get_backup_mirror_dir() == str(env["mirror"])
    slot = mm._backup_slots()[0]
    assert (env["mirror"] / slot.name).read_bytes() == \
        slot.read_bytes()


def test_tool_with_no_backups_yet_says_so(env):
    out = sbm.set_backup_mirror({"path": str(env["mirror"])})
    assert out.startswith("status=done")
    assert "No backups exist yet" in out


def test_tool_unwritable_first_copy_is_unverified(env):
    blocker = env["root"] / "a-file-not-a-dir"
    blocker.write_text("occupied", encoding="utf-8")
    _write_memory_twice()                  # a backup exists to copy
    out = sbm.set_backup_mirror({"path": str(blocker / "mirror")})
    assert out.startswith("status=unverified")
    # the setting itself was saved — only the protection is unproven
    assert cm.get_backup_mirror_dir() == str(blocker / "mirror")


def test_tool_clear_verifies(env):
    cm.save_backup_mirror_dir(str(env["mirror"]))
    out = sbm.set_backup_mirror({"clear": True})
    assert out.startswith("status=done")
    assert cm.get_backup_mirror_dir() == ""


def test_tool_reports_current_when_asked_empty(env):
    out = sbm.set_backup_mirror({})
    assert out.startswith("status=failed")  # nothing set, nothing given
    cm.save_backup_mirror_dir(str(env["mirror"]))
    out = sbm.set_backup_mirror({})
    assert out.startswith("status=done")
    assert str(env["mirror"]) in out
