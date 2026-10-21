"""Phase G1 — semantic recall (memory/embeddings.py + hybrid search).

The lexical scorer only finds a fact by the words it was stored
with. These suites pin the semantic leg and, just as hard, its
degradation contract:

  * a paraphrase with NO shared keywords finds the fact, labelled
    [similar match], ranked after keyword hits;
  * a changed value invalidates its stale vector by hash — the old
    meaning stops matching immediately, before any re-embed;
  * no key / embedder down / embedder raising → lexical-only answers,
    never a failed recall;
  * non-fact categories (playlist, sessions) are never embedded;
  * a store written before this module existed backfills on kick.
"""
import json
import re

import pytest

from core import incidents
from memory import embeddings, memory_manager

# Deterministic concept embedder: words vote for concept dimensions,
# synonyms share one. "automobile" and "Fiesta" land on the same axis
# without sharing a single keyword — the paraphrase condition.
CONCEPTS = [
    {"car", "fiesta", "automobile", "vehicle", "drive", "drives"},
    {"service", "repair", "tyres", "tires", "mechanic"},
    {"cake", "baking", "oven", "flour", "dessert"},
]


def _concept_embed(texts, task_type):
    out = []
    for t in texts:
        words = set(re.split(r"[^\w]+", t.lower()))
        out.append([float(len(words & c)) for c in CONCEPTS])
    return out


class Recorder:
    def __init__(self):
        self.texts = []

    def __call__(self, texts, task_type):
        self.texts.extend(texts)
        return _concept_embed(texts, task_type)


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "long_term.json"
    monkeypatch.setattr(memory_manager, "MEMORY_PATH", path)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    monkeypatch.setattr(memory_manager, "_recovery_note", None)
    # Determinism: the daemon worker never runs in these suites —
    # flush_pending() drains the queue in the test thread.
    monkeypatch.setattr(embeddings, "_ensure_worker", lambda: None)
    with embeddings._cv:
        embeddings._pending.clear()
    yield path
    embeddings.set_embedder(None)


def _sidecar(path):
    f = path.parent / "embeddings.json"
    return json.loads(f.read_text(encoding="utf-8")) if f.exists() else \
        {"entries": {}}


# ── The paraphrase case ─────────────────────────────────────────────────

def test_paraphrase_with_no_shared_keywords_matches(store):
    embeddings.set_embedder(_concept_embed)
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST, due a service in March"}}})
    embeddings.flush_pending()
    out = memory_manager.search_memory("what automobile does Alek drive")
    assert "Fiesta ST" in out
    assert "[similar match]" in out


def test_keyword_hit_is_not_relabelled_or_duplicated(store):
    embeddings.set_embedder(_concept_embed)
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST, due a service in March"}}})
    embeddings.flush_pending()
    out = memory_manager.search_memory("Fiesta service")
    assert out.count("Fiesta ST") == 1
    assert "[similar match]" not in out


def test_semantic_hits_rank_after_keyword_hits(store):
    embeddings.set_embedder(_concept_embed)
    memory_manager.update_memory({"notes": {
        "car": {"value": "Fiesta ST, due a service in March"},
        "backup_ride": {"value": "kept in the garage, drives fine"},
    }})
    embeddings.flush_pending()
    out = memory_manager.search_memory("vehicle service")
    lines = out.splitlines()[1:]
    assert lines[0].startswith("notes/car")          # keyword hit first
    # backup_ride shares no word with the query — it can only arrive
    # via the semantic leg ("drives" ~ "vehicle"), labelled as such.
    assert any(l.startswith("notes/backup ride") and "[similar match]" in l
               for l in lines[1:])


def test_unrelated_fact_stays_below_threshold(store):
    embeddings.set_embedder(_concept_embed)
    memory_manager.update_memory({"notes": {
        "car": {"value": "Fiesta ST, due a service in March"},
        "dessert": {"value": "Lemon cake, baking on Sunday"},
    }})
    embeddings.flush_pending()
    out = memory_manager.search_memory("what automobile does Alek drive")
    assert "Lemon cake" not in out


# ── Hash invalidation ───────────────────────────────────────────────────

def test_changed_value_invalidates_stale_vector_immediately(store):
    embeddings.set_embedder(_concept_embed)
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST, due a service in March"}}})
    embeddings.flush_pending()
    assert "Fiesta" in memory_manager.search_memory(
        "what automobile does Alek drive")
    # Change the fact; do NOT drain the refresh queue. The sidecar
    # still holds the old vector, but its hash no longer matches the
    # stored value, so the old meaning must already be unfindable.
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Sold it last spring"}}})
    out = memory_manager.search_memory("what automobile does Alek drive")
    assert "Sold it" not in out and "[similar match]" not in out
    embeddings.flush_pending()
    entry = _sidecar(store)["entries"]["notes/car"]
    assert entry["hash"] == embeddings._value_hash("Sold it last spring")


# ── Degradation: recall never depends on the network ────────────────────

def test_embedder_unavailable_is_lexical_only(store):
    embeddings.set_embedder(lambda texts, task_type: None)
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST, due a service in March"}}})
    embeddings.flush_pending()
    assert embeddings.semantic_matches(
        "automobile", memory_manager.load_memory()) == []
    out = memory_manager.search_memory("Fiesta")
    assert "Fiesta ST" in out and "[similar match]" not in out


def test_no_key_default_embedder_answers_nothing(store, monkeypatch):
    embeddings.set_embedder(None)                    # the Gemini path
    monkeypatch.setattr(embeddings, "_gemini_embed",
                        lambda texts, task_type: None)
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST, due a service in March"}}})
    out = memory_manager.search_memory("what automobile does Alek drive")
    assert "Nothing stored" in out                   # lexical floor held


def test_embedder_raising_never_breaks_recall(store):
    def boom(texts, task_type):
        raise RuntimeError("network is down")

    embeddings.set_embedder(boom)
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST, due a service in March"}}})
    embeddings.flush_pending()
    out = memory_manager.search_memory("Fiesta")
    assert "Fiesta ST" in out


# ── What never gets embedded ────────────────────────────────────────────

def test_playlist_and_sessions_are_never_embedded(store):
    rec = Recorder()
    embeddings.set_embedder(rec)
    memory_manager.remember("gym mix", "spotify:playlist:GYM1", "playlist")
    memory_manager.save_session_summary("we talked about the Fiesta")
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST"}}})
    embeddings.flush_pending()
    embeddings.kick_backfill()
    embeddings.flush_pending()
    keys = set(_sidecar(store)["entries"])
    assert keys == {"notes/car"}
    assert not any("spotify" in t or "GYM1" in t for t in rec.texts)


def test_deleted_fact_is_pruned_from_sidecar(store):
    embeddings.set_embedder(_concept_embed)
    memory_manager.update_memory(
        {"notes": {"car": {"value": "Fiesta ST"}}})
    embeddings.flush_pending()
    assert "notes/car" in _sidecar(store)["entries"]
    memory_manager.forget_by_keyword("Fiesta")
    assert "notes/car" not in _sidecar(store)["entries"]


# ── Backfill for a pre-existing store ───────────────────────────────────

def test_backfill_covers_a_store_written_before_embeddings(store):
    store.write_text(json.dumps({"notes": {
        "car": {"value": "Fiesta ST, due a service in March",
                "updated": "2026-01-01"}}}), encoding="utf-8")
    embeddings.set_embedder(_concept_embed)
    embeddings.kick_backfill()
    embeddings.flush_pending()
    assert "notes/car" in _sidecar(store)["entries"]
    out = memory_manager.search_memory("what automobile does Alek drive")
    assert "Fiesta ST" in out and "[similar match]" in out
