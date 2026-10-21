"""
memory/embeddings.py — semantic recall for the long-term store.

WHY THIS EXISTS
    search_memory() used to be lexical only: a fact was findable only
    by the words it happened to be stored with. "What car does he
    drive?" scored nothing against the stored note "the Fiesta needs
    new tyres" — no shared word worth counting. ARIA (the assistant
    this build succeeds) had embedding recall; the migration to this
    store dropped it as an accepted delta, and this module closes it.

WHAT IT IS
    A sidecar, memory/embeddings.json, mapping "category/key" to
    {"hash": ..., "vector": [...]} for FACT categories only (see
    memory_manager.FACT_CATEGORIES — playlists and session summaries
    are machinery, not facts, and are never embedded and never counted
    against the fact budgets). The hash is of the stored VALUE: when a
    fact changes, its hash stops matching and the stale vector is
    ignored until the refresh worker re-embeds it. Invalidation needs
    no bookkeeping at the write sites — every path that changes a
    value (the tools, the importer, the UI's per-entry forget) is
    covered by the same check.

    Vectors come from the Gemini API with the key the app already has:
    model gemini-embedding-001 at 768 dimensions (the model's smaller
    Matryoshka size — a personal store does not need 3072 dimensions
    to tell a car from a cake, and the sidecar stays a quarter of the
    size). Entries are embedded as documents, queries as queries.

THE DEGRADATION CONTRACT
    Recall must never depend on a network. No key, offline, quota,
    SDK trouble — every failure in here lands as "no semantic results"
    plus a debug line (and one incident per process), and
    search_memory() answers lexically exactly as it always did.
    Nothing in this module raises into a caller.

REFRESH
    memory_manager.save_memory() calls note_saved(), which diffs the
    store against the sidecar and queues changed facts; a daemon
    worker embeds them off the save path. kick_backfill() queues every
    fact lacking a fresh vector — main.py calls it once at startup,
    and semantic_matches() kicks it lazily when a recall finds gaps,
    so a store that predates this module fills in without ceremony.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading

from memory import memory_manager

EMBED_MODEL = "gemini-embedding-001"
EMBED_DIMS = 768
SIMILARITY_THRESHOLD = 0.55
_BATCH = 32

_TASK_DOCUMENT = "RETRIEVAL_DOCUMENT"
_TASK_QUERY = "RETRIEVAL_QUERY"

# The embedder seam: fn(texts, task_type) -> list of vectors, or None
# when embedding is unavailable. Tests inject a deterministic fake;
# production uses the Gemini API via core/gemini.
_embedder = None

# Pending refreshes: "cat/key" -> (cat, key). Values are re-read from
# the store at embed time, so a queued entry can never embed stale
# text — it embeds whatever the fact says when its turn comes.
_pending: dict[str, tuple[str, str]] = {}
_cv = threading.Condition()
_worker_started = False
_failure_reported = False   # one incident per process — see contract

_sidecar_lock = threading.Lock()


def set_embedder(fn) -> None:
    """Install a custom embedder (tests); None restores the Gemini one."""
    global _embedder
    _embedder = fn


# ── The embedder ────────────────────────────────────────────────────────

def _embed(texts: list[str], task_type: str):
    """Embed a batch; a list of vectors, or None when unavailable.
    Never raises — unavailability is a normal state here."""
    try:
        if _embedder is not None:
            return _embedder(texts, task_type)
        return _gemini_embed(texts, task_type)
    except Exception as e:
        print(f"[Memory] 🔎 Embedding failed: {e}")
        return None


def _gemini_embed(texts: list[str], task_type: str):
    from google.genai import types as gtypes

    from core import gemini
    key = gemini.api_key()
    if not key:
        return None
    client = gemini.client(key=key)
    resp = client.models.embed_content(
        model=EMBED_MODEL,
        contents=texts,
        config=gtypes.EmbedContentConfig(
            task_type=task_type, output_dimensionality=EMBED_DIMS),
    )
    vectors = [[float(x) for x in e.values] for e in resp.embeddings]
    return vectors if len(vectors) == len(texts) else None


def _report_failure() -> None:
    """One incident per process when embedding proves unavailable.
    The debug line prints every time; the ledger is for patterns, and
    'offline all evening' is one fact, not four hundred records."""
    global _failure_reported
    print("[Memory] 🔎 Semantic recall unavailable — lexical only")
    if _failure_reported:
        return
    _failure_reported = True
    try:
        from core import incidents
        incidents.record("memory.embeddings", "embedding_unavailable",
                         "embedding refresh/recall failed; recall is "
                         "lexical-only until the API answers again")
    except Exception:
        pass


# ── The sidecar ─────────────────────────────────────────────────────────

def _sidecar_path():
    # Beside the store, wherever the store is — tests redirect
    # MEMORY_PATH and the sidecar follows it.
    return memory_manager.MEMORY_PATH.parent / "embeddings.json"


def _load_sidecar() -> dict:
    try:
        data = json.loads(_sidecar_path().read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("entries"), dict):
            return data
    except Exception:
        pass
    return {"model": EMBED_MODEL, "entries": {}}


def _write_sidecar(data: dict) -> None:
    """Atomic, like the store itself; a torn sidecar costs a rebuild,
    never a fact. Never raises."""
    try:
        path = _sidecar_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, path)
    except Exception as e:
        print(f"[Memory] 🔎 Sidecar write failed: {e}")


def _value_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _fact_entries(memory: dict):
    """(cat, key, value) for every fact in the store. Non-fact
    categories (playlist, sessions) are skipped by construction."""
    for cat in memory_manager.FACT_CATEGORIES:
        items = memory.get(cat)
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = memory_manager._entry_value(entry)
            if val:
                yield cat, key, val


def _embed_text(cat: str, key: str, value: str) -> str:
    return f"{key.replace('_', ' ').strip()}: {value}"


# ── Refresh: diff, queue, worker ────────────────────────────────────────

def note_saved(memory: dict) -> None:
    """Called by memory_manager.save_memory() after every write. Diffs
    the store against the sidecar: facts whose hash moved are queued
    for re-embedding, sidecar entries for deleted facts are pruned.
    Cheap and synchronous; the embedding itself is the worker's."""
    try:
        sidecar = _load_sidecar()
        entries = sidecar["entries"]
        live = set()
        stale = []
        for cat, key, val in _fact_entries(memory):
            live.add(f"{cat}/{key}")
            cur = entries.get(f"{cat}/{key}")
            if not isinstance(cur, dict) or cur.get("hash") != _value_hash(val):
                stale.append((cat, key))
        pruned = [k for k in entries if k not in live]
        if pruned:
            with _sidecar_lock:
                fresh = _load_sidecar()
                for k in pruned:
                    fresh["entries"].pop(k, None)
                _write_sidecar(fresh)
        if stale:
            _queue(stale)
    except Exception as e:
        print(f"[Memory] 🔎 note_saved failed: {e}")


def kick_backfill(memory: dict | None = None) -> None:
    """Queue every fact that lacks a fresh vector. The startup call —
    and the lazy one from semantic_matches() when a recall finds the
    sidecar behind the store."""
    try:
        if memory is None:
            memory = memory_manager.load_memory()
        sidecar = _load_sidecar()
        stale = [(cat, key) for cat, key, val in _fact_entries(memory)
                 if (sidecar["entries"].get(f"{cat}/{key}") or {})
                 .get("hash") != _value_hash(val)]
        if stale:
            _queue(stale)
    except Exception as e:
        print(f"[Memory] 🔎 Backfill failed: {e}")


def _queue(pairs) -> None:
    with _cv:
        for cat, key in pairs:
            _pending[f"{cat}/{key}"] = (cat, key)
        _cv.notify_all()
    _ensure_worker()


def _ensure_worker() -> None:
    global _worker_started
    if _worker_started:
        return
    with _cv:
        if _worker_started:
            return
        _worker_started = True
    threading.Thread(target=_worker_loop, name="memory-embeddings",
                     daemon=True).start()


def _worker_loop() -> None:
    while True:
        with _cv:
            while not _pending:
                _cv.wait()
        _drain_once()


def _drain_once() -> bool:
    """Embed one batch of pending facts. True when a batch was taken
    (whatever its outcome), False when the queue was empty. A failed
    batch is DROPPED, not retried in a loop: the hashes stay stale, so
    the next save or backfill re-queues it — retrying here against a
    down network would just spin."""
    with _cv:
        batch = []
        for name in list(_pending)[:_BATCH]:
            batch.append(_pending.pop(name))
    if not batch:
        return False
    memory = memory_manager.load_memory()
    texts, targets = [], []
    for cat, key in batch:
        items = memory.get(cat)
        if not isinstance(items, dict):
            continue
        val = memory_manager._entry_value(items.get(key))
        if not val:
            continue                     # deleted since it was queued
        texts.append(_embed_text(cat, key, val))
        targets.append((cat, key, val))
    if not targets:
        return True
    vectors = _embed(texts, _TASK_DOCUMENT)
    if vectors is None:
        _report_failure()
        return True
    with _sidecar_lock:
        sidecar = _load_sidecar()
        for (cat, key, val), vec in zip(targets, vectors):
            sidecar["entries"][f"{cat}/{key}"] = {
                "hash": _value_hash(val), "vector": vec}
        sidecar["model"] = EMBED_MODEL
        _write_sidecar(sidecar)
    return True


def flush_pending(max_batches: int = 100) -> None:
    """Drain the queue in the calling thread (tests, and any future
    'index now' surface). Bounded so a pathological store cannot hang
    a caller; the worker keeps draining past the bound anyway."""
    for _ in range(max_batches):
        if not _drain_once():
            break


# ── Recall ──────────────────────────────────────────────────────────────

def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def semantic_matches(query: str, memory: dict, limit: int = 8) -> list:
    """Facts whose meaning matches `query`: [(similarity, cat, key,
    value)], best first, above SIMILARITY_THRESHOLD. Only entries with
    a FRESH vector (hash matches the current value) can match — a
    stale vector describes a fact that no longer exists. Empty list
    whenever embeddings are unavailable; never raises."""
    try:
        return _semantic_matches(query, memory, limit)
    except Exception as e:
        print(f"[Memory] 🔎 Semantic recall failed: {e}")
        return []


def _semantic_matches(query: str, memory: dict, limit: int) -> list:
    sidecar = _load_sidecar()
    entries = sidecar["entries"]
    candidates = []
    gaps = False
    for cat, key, val in _fact_entries(memory):
        cur = entries.get(f"{cat}/{key}")
        if isinstance(cur, dict) and cur.get("hash") == _value_hash(val) \
                and isinstance(cur.get("vector"), list):
            candidates.append((cat, key, val, cur["vector"]))
        else:
            gaps = True
    if gaps:
        kick_backfill(memory)            # results improve next recall
    if not candidates:
        return []
    qv = _embed([query], _TASK_QUERY)
    if not qv:
        _report_failure()
        return []
    scored = []
    for cat, key, val, vec in candidates:
        sim = _cosine(qv[0], vec)
        if sim >= SIMILARITY_THRESHOLD:
            scored.append((sim, cat, key, val))
    scored.sort(key=lambda r: -r[0])
    return scored[:max(1, limit)]
