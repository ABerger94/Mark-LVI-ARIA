"""Phase G1 — the memory store, hardened.

The store used to be written in place and loaded on trust: a torn
write or a corrupt byte loaded as an EMPTY memory, silently, and
every fact the user had ever told the assistant was simply gone.
These suites pin the new contract:

  * writes are atomic (temp file + os.replace) — a crash mid-write
    leaves the previous valid store;
  * every write first snapshots the outgoing store into a rotation of
    5 known-good backups (a file that does not parse is never
    snapshotted);
  * a present-but-unreadable store recovers from the newest readable
    backup, records an incident, and leaves a note for the startup
    briefing; total loss records memory_lost and heals the file;
  * a missing file is a fresh start — no incident, no note;
  * truncation at the 380-char cap is marked on the entry and said
    out loud by remember(), instead of silently shortening;
  * the fact-budget trim never spends playlist entries.
"""
import json

import pytest

from core import incidents
from memory import embeddings, memory_manager


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "long_term.json"
    monkeypatch.setattr(memory_manager, "MEMORY_PATH", path)
    inc = tmp_path / "incidents.jsonl"
    monkeypatch.setattr(incidents, "INCIDENTS_PATH", inc)
    monkeypatch.setattr(memory_manager, "_recovery_note", None)
    # The semantic leg is another suite's subject; here it must never
    # reach a network or a worker thread.
    monkeypatch.setattr(embeddings, "_ensure_worker", lambda: None)
    embeddings.set_embedder(lambda texts, task_type: None)
    with embeddings._cv:
        embeddings._pending.clear()
    yield path
    embeddings.set_embedder(None)


def _kinds(path):
    if not path.exists():
        return []
    return [json.loads(ln)["kind"]
            for ln in path.read_text(encoding="utf-8").splitlines() if ln]


def _save_fact(key, value, cat="notes"):
    memory_manager.update_memory({cat: {key: {"value": value}}})


# ── Atomic writes ───────────────────────────────────────────────────────

def test_failed_replace_leaves_previous_store(store, monkeypatch):
    _save_fact("car", "the Fiesta")
    before = store.read_text(encoding="utf-8")

    def boom(src, dst):
        raise OSError("simulated crash at replace")

    monkeypatch.setattr(memory_manager.os, "replace", boom)
    with pytest.raises(OSError):
        memory_manager.save_memory({"notes": {"car": {"value": "gone"}}})
    assert store.read_text(encoding="utf-8") == before
    assert memory_manager._parse_store(before) is not None
    assert not store.with_name("long_term.json.tmp").exists()


def test_unserialisable_save_leaves_previous_store(store):
    _save_fact("car", "the Fiesta")
    before = store.read_text(encoding="utf-8")
    with pytest.raises(TypeError):
        memory_manager.save_memory({"notes": {"bad": {"value": {1, 2}}}})
    assert store.read_text(encoding="utf-8") == before


# ── Backups ─────────────────────────────────────────────────────────────

def test_backups_rotate_and_cap_at_five(store):
    for i in range(1, 9):
        _save_fact("fact", f"value-{i}")
    backups = sorted((store.parent / "backups").glob("*.json"))
    assert len(backups) == memory_manager.BACKUP_KEEP == 5
    newest = json.loads(backups[0].read_text(encoding="utf-8"))
    assert memory_manager._entry_value(newest["notes"]["fact"]) == "value-7"
    oldest = json.loads(backups[-1].read_text(encoding="utf-8"))
    assert memory_manager._entry_value(oldest["notes"]["fact"]) == "value-3"


def test_corrupt_store_is_never_snapshotted(store):
    _save_fact("car", "the Fiesta")
    _save_fact("car", "the Fiesta, serviced")
    slot = store.parent / "backups" / "long_term.1.json"
    good_snapshot = slot.read_text(encoding="utf-8")
    store.write_text("{torn", encoding="utf-8")
    _save_fact("other", "written over the damage")
    assert slot.read_text(encoding="utf-8") == good_snapshot
    live = memory_manager._parse_store(store.read_text(encoding="utf-8"))
    assert memory_manager._entry_value(live["notes"]["other"]) == \
        "written over the damage"


# ── Load validation + recovery ──────────────────────────────────────────

def test_missing_file_is_a_fresh_start(store):
    memory = memory_manager.load_memory()
    assert memory == memory_manager._empty_memory()
    assert memory_manager.pop_recovery_note() is None
    assert _kinds(store.parent / "incidents.jsonl") == []


def test_corrupt_store_recovers_from_newest_backup(store):
    _save_fact("niece", "Ayse", cat="relationships")
    _save_fact("niece", "Ayse, visiting in June", cat="relationships")
    store.write_text("{torn write,,,", encoding="utf-8")

    memory = memory_manager.load_memory()
    # Backup 1 holds the state before the second save.
    assert memory_manager._entry_value(
        memory["relationships"]["niece"]) == "Ayse"
    assert _kinds(store.parent / "incidents.jsonl") == ["memory_recovered"]
    note = memory_manager.pop_recovery_note()
    assert note and "restored it from a backup" in note
    assert memory_manager.pop_recovery_note() is None     # spoken once
    # The live file is valid again — the next load is a clean load.
    assert memory_manager._parse_store(
        store.read_text(encoding="utf-8")) is not None


def test_recovery_walks_past_a_corrupt_backup(store):
    _save_fact("fact", "v0")
    _save_fact("fact", "v1")
    _save_fact("fact", "v2")
    backups = store.parent / "backups"
    (backups / "long_term.1.json").write_text("garbage", encoding="utf-8")
    store.write_text("garbage", encoding="utf-8")

    memory = memory_manager.load_memory()
    assert memory_manager._entry_value(memory["notes"]["fact"]) == "v0"
    recs = [json.loads(ln) for ln in
            (store.parent / "incidents.jsonl").read_text(
                encoding="utf-8").splitlines()]
    assert recs[0]["kind"] == "memory_recovered"
    assert "backup 2 of 2" in recs[0]["message"]


def test_wrong_shape_json_counts_as_corruption(store):
    _save_fact("fact", "kept")
    _save_fact("fact", "kept v2")
    store.write_text("[1, 2, 3]", encoding="utf-8")
    memory = memory_manager.load_memory()
    assert memory_manager._entry_value(memory["notes"]["fact"]) == "kept"


def test_total_loss_records_memory_lost_and_heals(store):
    _save_fact("fact", "v1")
    _save_fact("fact", "v2")
    for f in (store.parent / "backups").glob("*.json"):
        f.write_text("garbage", encoding="utf-8")
    store.write_text("garbage", encoding="utf-8")

    memory = memory_manager.load_memory()
    assert memory == memory_manager._empty_memory()
    assert _kinds(store.parent / "incidents.jsonl") == ["memory_lost"]
    assert "empty memory" in memory_manager.pop_recovery_note()
    # Healed: the damaged file was moved aside, a valid empty store is
    # live, and a second load does not record the loss again.
    assert list(store.parent.glob("long_term.json.corrupt-*"))
    assert memory_manager._parse_store(
        store.read_text(encoding="utf-8")) is not None
    memory_manager.load_memory()
    assert _kinds(store.parent / "incidents.jsonl") == ["memory_lost"]


def test_pop_last_session_pops_through_recovery(store):
    _save_fact("fact", "v1")
    memory_manager.save_session_summary("talked about the car")
    _save_fact("fact", "v2")
    store.write_text("garbage", encoding="utf-8")
    entry = memory_manager.pop_last_session()
    assert entry and "car" in entry["summary"]


# ── Truncation honesty ──────────────────────────────────────────────────

def test_long_value_is_marked_and_reported(store):
    msg = memory_manager.remember("essay", "y" * 500, "notes")
    assert "shortened to 380 characters" in msg
    data = json.loads(store.read_text(encoding="utf-8"))
    entry = data["notes"]["essay"]
    assert entry["truncated"] is True
    assert len(entry["value"]) == memory_manager.MAX_VALUE_LENGTH + 1


def test_short_value_is_unmarked_and_unreported(store):
    msg = memory_manager.remember("car", "the Fiesta", "notes")
    assert "shortened" not in msg
    data = json.loads(store.read_text(encoding="utf-8"))
    assert "truncated" not in data["notes"]["car"]


def test_update_memory_reports_cut_keys(store):
    cut = []
    memory_manager.update_memory(
        {"notes": {"long": {"value": "z" * 400}}}, cut)
    assert cut == ["long"]
    # _truncate_value keeps its plain-string contract for the importer.
    assert isinstance(memory_manager._truncate_value("z" * 400), str)


# ── Fact budgets never spend machinery ──────────────────────────────────

def test_trim_never_spends_playlist_entries(store):
    facts = {f"k{i}": {"value": "x" * 380, "updated": "2026-01-01"}
             for i in range(600)}
    memory = {"notes": facts,
              "playlist": {"gym": {"value": "spotify:playlist:GYM1"}}}
    assert len(json.dumps(memory)) > memory_manager.MEMORY_MAX_CHARS
    memory_manager.save_memory(memory)
    live = json.loads(store.read_text(encoding="utf-8"))
    assert live["playlist"]["gym"]["value"] == "spotify:playlist:GYM1"
    assert len(json.dumps(live)) <= memory_manager.MEMORY_MAX_CHARS
