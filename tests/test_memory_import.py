"""Phase D (D1) — ARIA's memories come across, and can be forgotten.

The importer's contract, pinned here against a synthetic store built
from ARIA's real schema (aria/memory.py: memory(id, category, key,
value, updated_at, embedding)):

  * her categories map onto this store's (her singular "preference"
    becomes "preferences"; journal/note/general land in notes);
  * her playlist rows keep their own category — the DJ data has to
    survive the move intact, or the Phase F DJ port has nothing to
    resolve names from;
  * internal entries (system category, "_"-prefixed keys) do not come;
  * this store's rules apply on arrival: the 380-char value cap is
    enforced by the store's own truncation, and her updated dates are
    kept rather than re-stamped;
  * running it twice is a no-op the second time.

forget_by_keyword is ARIA's forget semantics with this store under
it: key-or-value substring, case-insensitive, the count is the
answer, a no-match changes nothing.
"""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from memory import memory_manager

ROOT = Path(__file__).resolve().parent.parent


def _load_importer():
    spec = importlib.util.spec_from_file_location(
        "import_aria_memory", ROOT / "tools" / "import_aria_memory.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def aria_db(tmp_path: Path) -> Path:
    """A synthetic ARIA memory database, her real schema."""
    db = tmp_path / "aria_memory.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE memory ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, key TEXT, "
        "value TEXT, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, "
        "embedding BLOB)")
    rows = [
        ("identity", "name", "Alek", "2026-10-01 09:00:00"),
        ("preference", "favorite_food", "pizza", "2026-10-02 10:00:00"),
        ("projects", "deck_build", "Malcolm/Kediss deck tech",
         "2026-10-03 11:00:00"),
        ("playlist", "doja_playlist", "spotify:playlist:abc123",
         "2026-10-04 12:00:00"),
        ("journal", "journal 2026-10-05", "x" * 500, "2026-10-05 13:00:00"),
        ("general", "misc_fact", "the sky is blue", "2026-10-05 14:00:00"),
        ("note", "note 2026-10-06", "buy milk", "2026-10-06 09:00:00"),
        ("system", "_last_greeting", "hello there", "2026-10-06 10:00:00"),
        ("notes", "_internal_scratch", "not a fact", "2026-10-06 11:00:00"),
    ]
    conn.executemany(
        "INSERT INTO memory (category, key, value, updated_at) "
        "VALUES (?, ?, ?, ?)", rows)
    conn.commit()
    conn.close()
    return db


def _read_store(target: Path) -> dict:
    return json.loads(target.read_text(encoding="utf-8"))


def test_import_maps_categories_and_keeps_playlists(aria_db, tmp_path):
    imp = _load_importer()
    target = tmp_path / "long_term.json"
    report = imp.import_aria_memory(aria_db, target)

    assert report["read"] == 9
    assert report["skipped"] == 2            # system/_ row + "_" key row
    assert report["imported"] == 7
    assert report["by_category"] == {
        "identity": 1, "preferences": 1, "projects": 1,
        "playlist": 1, "notes": 3,
    }

    store = _read_store(target)
    assert store["identity"]["name"]["value"] == "Alek"
    # Her date is kept, not re-stamped as learned on import day.
    assert store["identity"]["name"]["updated"] == "2026-10-01"
    # Singular → plural.
    assert store["preferences"]["favorite_food"]["value"] == "pizza"
    # DJ data survives in its own category.
    assert store["playlist"]["doja_playlist"]["value"] == \
        "spotify:playlist:abc123"
    # journal + general + note all land in notes.
    assert "journal 2026-10-05" in store["notes"]
    assert "misc_fact" in store["notes"]
    assert "note 2026-10-06" in store["notes"]
    # Internal state did not come across, under any category.
    flat = {k for cat in store.values() if isinstance(cat, dict)
            for k in cat}
    assert "_last_greeting" not in flat
    assert "_internal_scratch" not in flat


def test_import_applies_the_stores_value_cap(aria_db, tmp_path):
    imp = _load_importer()
    target = tmp_path / "long_term.json"
    imp.import_aria_memory(aria_db, target)
    value = _read_store(target)["notes"]["journal 2026-10-05"]["value"]
    # The store's own rule: 380 chars + the ellipsis it appends.
    assert len(value) == memory_manager.MAX_VALUE_LENGTH + 1
    assert value.endswith("…")


def test_import_is_idempotent(aria_db, tmp_path):
    imp = _load_importer()
    target = tmp_path / "long_term.json"
    imp.import_aria_memory(aria_db, target)
    before = _read_store(target)
    report = imp.import_aria_memory(aria_db, target)
    assert report["changed"] == 0
    assert report["unchanged"] == 7
    assert _read_store(target) == before


def test_import_from_json_export(tmp_path):
    imp = _load_importer()
    export = tmp_path / "export.json"
    export.write_text(json.dumps([
        {"category": "preference", "key": "favorite_color",
         "value": "green", "updated": "2026-09-01 08:00:00"},
        {"category": "playlist", "key": "chill", "value": "spotify:x"},
    ]), encoding="utf-8")
    target = tmp_path / "long_term.json"
    report = imp.import_aria_memory(export, target)
    assert report["imported"] == 2
    store = _read_store(target)
    assert store["preferences"]["favorite_color"]["value"] == "green"
    assert store["playlist"]["chill"]["value"] == "spotify:x"


# ── forget_by_keyword ───────────────────────────────────────────────────────

@pytest.fixture()
def store(tmp_path, monkeypatch):
    target = tmp_path / "long_term.json"
    monkeypatch.setattr(memory_manager, "MEMORY_PATH", target)
    memory_manager.update_memory({
        "preferences": {"favorite_food": {"value": "pizza"}},
        "playlist": {"doja_playlist": {"value": "spotify:playlist:abc"}},
        "notes": {"shopping": {"value": "milk and eggs"}},
    })
    return target


def test_forget_matches_value_substring(store):
    n, gone = memory_manager.forget_by_keyword("piz")
    assert n == 1
    assert gone == ["preferences/favorite_food"]
    assert "favorite_food" not in memory_manager.load_memory()[
        "preferences"]


def test_forget_matches_key_case_insensitively(store):
    n, gone = memory_manager.forget_by_keyword("DOJA")
    assert n == 1
    assert gone == ["playlist/doja_playlist"]


def test_forget_no_match_is_a_clean_noop(store):
    before = memory_manager.load_memory()
    n, gone = memory_manager.forget_by_keyword("zzzz-not-there")
    assert (n, gone) == (0, [])
    assert memory_manager.load_memory() == before


def test_forget_empty_query_forgets_nothing(store):
    n, gone = memory_manager.forget_by_keyword("   ")
    assert (n, gone) == (0, [])
