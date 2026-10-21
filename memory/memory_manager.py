import json
import os
import re
import shutil
from datetime import datetime
from threading import RLock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
_lock            = RLock()
MAX_VALUE_LENGTH = 380
BACKUP_KEEP      = 5

# The categories that hold FACTS about the person. Two other things live
# in the same file and are not facts: 'sessions' (a list of visit
# summaries) and 'playlist' (DJ's shelf of Spotify URIs). Facts are what
# semantic recall embeds and what the store/prompt budgets ration; the
# machinery categories are never embedded, and their entries are never
# trimmed to make room for facts.
FACT_CATEGORIES = ("identity", "preferences", "projects",
                   "relationships", "wishes", "notes")

# ── Why there are two very different numbers here ────────────────────────────
#
# There used to be one: MEMORY_MAX_CHARS = 2200, applied to the whole store. It
# was a *storage* limit, and it existed only because the entire memory was
# pasted into the system prompt on every connect — so growing the memory grew
# every single request. When it filled, _trim_to_limit() deleted the oldest
# entries and printed one line to a console nobody reads. A memory described as
# "deeply remembers projects, preferences and personal context" was in practice
# two pages long, and quietly forgot your sister's name after a few weeks.
#
# Storage and prompt budget are now separate concerns:
#
#   MEMORY_MAX_CHARS  — a runaway guard, not a feature limit. Nothing normal
#                       reaches it; a bug writing in a loop does.
#   PROMPT_CORE_CHARS — what actually rides in the system prompt every session.
#                       Smaller than the old whole-memory dump, so sessions
#                       start *faster* than before, not slower.
#
# Everything above the core stays on disk and is fetched on demand by the
# recall_memory tool — see search_memory() and format_memory_for_prompt().
MEMORY_MAX_CHARS  = 200_000
PROMPT_CORE_CHARS = 900
PROMPT_INDEX_CHARS = 420
# Most entries any one category may contribute to the core block, so a person
# with forty stored preferences still gets their sister into the prompt.
PROMPT_MAX_PER_CATEGORY = 6

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }

def _parse_store(text: str) -> dict | None:
    """Parse store text; the dict, or None when the text is not a usable
    store (bad JSON, or JSON that isn't an object)."""
    try:
        data = json.loads(text)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _with_defaults(data: dict) -> dict:
    base = _empty_memory()
    for key in base:
        if key not in data:
            data[key] = {}
    return data


def _backup_slots() -> list[Path]:
    """Backup file paths, newest first (slot 1 is the most recent)."""
    d = MEMORY_PATH.parent / "backups"
    return [d / f"long_term.{i}.json" for i in range(1, BACKUP_KEEP + 1)]


def _snapshot_backup() -> None:
    """Copy the CURRENT live store into the backup rotation — but only
    if it still parses. A backup of a corrupt file is worse than no
    backup: it would occupy a slot the recovery walk trusts. Never
    raises; a failed backup costs a snapshot, not a save."""
    try:
        if not MEMORY_PATH.exists():
            return
        if _parse_store(MEMORY_PATH.read_text(encoding="utf-8")) is None:
            return
        slots = _backup_slots()
        slots[0].parent.mkdir(parents=True, exist_ok=True)
        for i in range(len(slots) - 1, 0, -1):
            if slots[i - 1].exists():
                os.replace(slots[i - 1], slots[i])
        shutil.copyfile(MEMORY_PATH, slots[0])
    except Exception as e:
        print(f"[Memory] ⚠️ Backup failed: {e}")


def _write_store(memory: dict) -> None:
    """The one way the store reaches disk. Two properties, both earned:

      * ATOMIC — serialised to a temp file in the same directory and
        os.replace()d over the live file, so a crash mid-write (or a
        full disk, or a kill) leaves the PREVIOUS valid store in
        place, never a torn half-file. The old code wrote the live
        file in place; a torn write there was indistinguishable from
        amnesia at the next load.
      * BACKED UP — before the replace, the outgoing live file is
        snapshotted into memory/backups/ (the last BACKUP_KEEP
        known-good copies), which is what load-time recovery walks.
    """
    text = json.dumps(memory, indent=2, ensure_ascii=False)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _snapshot_backup()
        tmp = MEMORY_PATH.with_name(MEMORY_PATH.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, MEMORY_PATH)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


# Set when a load had to recover (or give up), cleared when the startup
# briefing speaks it once — see pop_recovery_note().
_recovery_note: str | None = None


def pop_recovery_note() -> str | None:
    """The one-line note from the most recent load-time recovery, or
    None when recent loads were clean. Consumed by reading: a recovery
    is announced once, in the startup briefing, not repeated on every
    load — the incident record is the durable copy."""
    global _recovery_note
    note, _recovery_note = _recovery_note, None
    return note


def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        # A missing file is a fresh start, not damage: no store has
        # ever been written here. (Backups are only consulted when a
        # store EXISTS but cannot be read — see _recover_from_backups.)
        return _empty_memory()
    with _lock:
        data = None
        try:
            data = _parse_store(MEMORY_PATH.read_text(encoding="utf-8"))
        except Exception as e:
            print(f"[Memory] ⚠️ Load error: {e}")
        if data is not None:
            return _with_defaults(data)
        return _recover_from_backups()


def _recover_from_backups() -> dict:
    """The live store exists but cannot be read. Walk the backups
    newest → oldest; the first that parses becomes the store again.

    This replaces the old behaviour — print one line, return an empty
    memory — under which a single torn write silently cost the user
    every fact they had ever told the assistant, with no record
    anywhere that it had happened. Recovery (and total loss) are both
    recorded as incidents, and both leave a note for the startup
    briefing: the user hears about it from the assistant, once, in
    plain words — they do not find out by being forgotten."""
    global _recovery_note
    from core import incidents
    slots = [s for s in _backup_slots() if s.exists()]
    for depth, slot in enumerate(slots, start=1):
        try:
            data = _parse_store(slot.read_text(encoding="utf-8"))
        except Exception:
            data = None
        if data is None:
            continue
        restored = _with_defaults(data)
        try:
            _write_store(restored)
        except Exception as e:
            print(f"[Memory] ⚠️ Restore write failed: {e}")
        msg = (f"long_term.json unreadable; restored from backup "
               f"{depth} of {len(slots)} ({slot.name})")
        print(f"[Memory] 🛟 Recovered: {msg}")
        incidents.record("memory", "memory_recovered", msg)
        _recovery_note = (
            "A note about my memory: its file was damaged, so I "
            "restored it from a backup — anything saved very recently "
            "may be missing.")
        return restored
    print("[Memory] 🛟 No readable backup — starting with an empty memory")
    incidents.record("memory", "memory_lost",
                     "long_term.json unreadable and no backup parsed; "
                     "memory started empty")
    _recovery_note = (
        "A note about my memory: its file was damaged and no backup "
        "could be read, so I have started with an empty memory — I "
        "may be missing things you told me before.")
    # Heal the file: move the unreadable store aside (kept, not
    # deleted — it is evidence, and a human may yet read something
    # out of it) and write a valid empty store, so every later load
    # does not re-walk the backups and re-record the loss.
    try:
        aside = MEMORY_PATH.with_name(
            f"{MEMORY_PATH.name}.corrupt-{datetime.now():%Y%m%d%H%M%S}")
        os.replace(MEMORY_PATH, aside)
        _write_store(_empty_memory())
    except Exception as e:
        print(f"[Memory] ⚠️ Could not reset the damaged store: {e}")
    return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


# Set by main.py so a trim can reach the activity log. Deleting something a
# person told you and mentioning it only on stdout is how a memory loses trust.
_trim_notifier = None


def set_trim_notifier(fn) -> None:
    """Register a callable(str) that surfaces trims to the user."""
    global _trim_notifier
    _trim_notifier = fn


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    # Only facts are trimmed: the cap rations the fact store, and a
    # playlist on DJ's shelf is not kindling for it.
    entries = [t for t in _all_entries(memory) if t[0] in FACT_CATEGORIES]
    entries.sort(key=lambda t: t[2].get("updated", "0000-00-00"))
    dropped = []
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        dropped.append(f"{cat}/{key}")
        print(f"[Memory] 🗑️  Trimmed {cat}/{key}")
    if dropped and _trim_notifier:
        try:
            _trim_notifier(
                f"SYS: Memory full — forgot {len(dropped)} oldest entries "
                f"({', '.join(dropped[:3])}{'…' if len(dropped) > 3 else ''})"
            )
        except Exception:
            pass
    return memory

def _embeddings_module():
    """The semantic-recall sidecar (memory/embeddings.py), imported
    lazily and never allowed to break a save or a recall: it pulls the
    Gemini SDK, and memory_manager is imported by nearly everything.
    None means "no semantic leg today" — callers degrade to lexical."""
    try:
        from memory import embeddings
        return embeddings
    except Exception:
        return None


def save_memory(memory: dict) -> None:
    if not isinstance(memory, dict):
        return
    memory = _trim_to_limit(memory)
    _write_store(memory)
    emb = _embeddings_module()
    if emb is not None:
        emb.note_saved(memory)


def _truncate_value(val: str) -> str:
    return _truncate_value_flagged(val)[0]


def _truncate_value_flagged(val) -> tuple[str, bool]:
    """_truncate_value, plus whether it cut. The cap stays — it is the
    prompt budget, not an oversight — but the cut stops being silent:
    the stored entry is marked (see _recursive_update) and save results
    can say it happened. A memory that quietly shortens what it was
    told teaches the assistant to misquote the user with confidence."""
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…", True
    return val, False


def _recursive_update(target: dict, updates: dict,
                      truncated_out: list | None = None) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value, truncated_out):
                changed = True
        else:
            new_val, was_cut = _truncate_value_flagged(
                str(value["value"] if isinstance(value, dict) else value))
            entry = {"value": new_val,
                     "updated": datetime.now().strftime("%Y-%m-%d")}
            if was_cut:
                entry["truncated"] = True
                if truncated_out is not None:
                    truncated_out.append(key)
            existing = target.get(key, {})
            if not isinstance(existing, dict) or existing.get("value") != new_val:
                target[key] = entry
                changed = True
    return changed


def update_memory(memory_update: dict,
                  truncated_out: list | None = None) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    cut: list[str] = []
    if _recursive_update(memory, memory_update, cut):
        save_memory(memory)
        note = f" (shortened to fit: {', '.join(cut)})" if cut else ""
        print(f"[Memory] 💾 Saved: {list(memory_update.keys())}{note}")
    if truncated_out is not None:
        truncated_out.extend(cut)
    return memory

def _entry_value(entry) -> str:
    """Accept both the {'value': ..., 'updated': ...} shape and a bare string,
    because early versions of the store wrote plain strings."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _pretty(key: str) -> str:
    return key.replace("_", " ").strip()


# Identity is always in the prompt; these categories compete for the remaining
# budget by recency.
_CATEGORY_LABELS = {
    "preferences":   "Preferences",
    "projects":      "Active projects / goals",
    "relationships": "People in their life",
    "wishes":        "Wishes / plans",
    "notes":         "Notes",
}

_IDENTITY_FIELDS = ["name", "age", "birthday", "city", "job",
                    "language", "school", "nationality"]


def format_memory_for_prompt(memory: dict | None) -> str:
    """Build the memory block that goes into the system prompt.

    This used to dump everything. It now sends three things:

      1. IDENTITY  - always, in full. It is small, and it is wrong for the
         assistant to have to look up your name.
      2. RECENT    - the most recently updated entries from every other
         category, up to PROMPT_CORE_CHARS. Recency is the cheapest useful
         relevance signal available without embeddings.
      3. AN INDEX  - the *keys* of everything else, values omitted.

    Point 3 is what makes recall work at all. A model cannot decide to look
    something up if it does not know the thing exists: with only points 1 and 2,
    "who is Ayse?" would get "I don't know" while ayse_sister sat on disk
    unread. The index costs a few hundred characters and turns recall from a
    gamble into a lookup.

    Net effect on latency: this block is SMALLER than the old full dump, so
    every session connects with fewer tokens. Occasionally the model spends one
    extra round trip on recall_memory - covered by the acknowledgment it
    already speaks before any slow step."""
    if not memory:
        return ""

    core_lines: list[str] = []

    # 1. Identity - always, in full
    identity = memory.get("identity", {}) or {}
    for field in _IDENTITY_FIELDS:
        val = _entry_value(identity.get(field))
        if not val:
            continue
        if field == "language":
            # Labelled as an observation, not a setting. A bare "Language:
            # English" line written months ago reads like a standing order and
            # was one of the reasons a Turkish question came back in English.
            core_lines.append(
                f"Has spoken to you in: {val} (an observation about the past — "
                f"always answer in the language of their CURRENT message)")
        else:
            core_lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in _IDENTITY_FIELDS:
            continue
        val = _entry_value(entry)
        if val:
            core_lines.append(f"{_pretty(key).title()}: {val}")

    # 2. Everything else, most recently updated first
    rest: list[tuple[str, str, str, str]] = []   # (updated, cat, key, value)
    for cat in _CATEGORY_LABELS:
        for key, entry in (memory.get(cat, {}) or {}).items():
            val = _entry_value(entry)
            if not val:
                continue
            updated = (entry.get("updated", "") if isinstance(entry, dict) else "") or "0000-00-00"
            rest.append((updated, cat, key, val))
    rest.sort(key=lambda t: t[0], reverse=True)

    used    = sum(len(l) + 1 for l in core_lines)
    shown: dict[str, list[str]] = {}
    overflow: dict[str, list[str]] = {}

    # Recency decides order, but no single category may take the whole budget.
    # Without the cap, someone with forty stored preferences gets a prompt that
    # is forty preferences and not one person's name — the categories that
    # matter most in conversation are also the ones that change least often, so
    # pure recency systematically buries them.
    per_cat_used: dict[str, int] = {}
    for _updated, cat, key, val in rest:
        line = f"  - {_pretty(key).title()}: {val}"
        if (per_cat_used.get(cat, 0) < PROMPT_MAX_PER_CATEGORY
                and used + len(line) + 1 <= PROMPT_CORE_CHARS):
            shown.setdefault(cat, []).append(line)
            per_cat_used[cat] = per_cat_used.get(cat, 0) + 1
            used += len(line) + 1
        else:
            overflow.setdefault(cat, []).append(_pretty(key))

    # The index is a table of contents, so it is interleaved across categories
    # rather than continuing in recency order. Sorted by recency it would list
    # twenty-four preferences before the first relationship, and the one entry
    # the index exists for — the old fact the model has no other way to know
    # about — would fall off the end.
    indexed: list[str] = []
    if overflow:
        cats  = [c for c in _CATEGORY_LABELS if overflow.get(c)]
        cursor = {c: 0 for c in cats}
        while cats:
            for cat in list(cats):
                i = cursor[cat]
                if i >= len(overflow[cat]):
                    cats.remove(cat)
                    continue
                indexed.append(overflow[cat][i])
                cursor[cat] = i + 1

    for cat, label in _CATEGORY_LABELS.items():
        if shown.get(cat):
            core_lines.append("")
            core_lines.append(f"{label}:")
            core_lines.extend(shown[cat])

    if not core_lines and not indexed:
        return ""

    out = [
        "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]",
        *core_lines,
    ]

    # 3. The index of what is on disk but not in this prompt
    if indexed:
        budget, names = PROMPT_INDEX_CHARS, []
        for n in indexed:
            if budget - len(n) - 2 < 0:
                break
            names.append(n)
            budget -= len(n) + 2
        if names:
            out.append("")
            out.append(
                "[ALSO REMEMBERED — values not shown here. Call recall_memory "
                "with a keyword to read any of these before saying you do not know]"
            )
            out.append(", ".join(names)
                       + (f" (+{len(indexed) - len(names)} more)"
                          if len(indexed) > len(names) else ""))

    return "\n".join(out) + "\n"


# ── Recall ────────────────────────────────────────────────────────────────────

def _score(query_words: list[str], cat: str, key: str, value: str) -> int:
    """Cheap lexical relevance. No embeddings, no network, no model call - this
    runs in well under a millisecond, which is the entire point: recall must
    cost one model round trip, never two."""
    hay_key = _pretty(key).lower()
    hay_val = value.lower()
    score   = 0
    for w in query_words:
        if not w:
            continue
        if w == hay_key:
            score += 10
        elif w in hay_key:
            score += 6
        if w in hay_val:
            score += 3
        if w in cat:
            score += 1
    return score


def search_memory(query: str, limit: int = 8) -> str:
    """Find stored facts matching `query`. Backs the recall_memory tool.

    An empty query is treated as "show me everything you know", capped - the
    model asks that when the user says "what do you remember about me?"."""
    memory = load_memory()
    words  = [w for w in re.split(r"[^\w]+", (query or "").lower()) if len(w) > 1]

    rows: list[tuple[int, str, str, str]] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # skip 'sessions', which is a list
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            s = _score(words, cat, key, val) if words else 1
            if s > 0:
                rows.append((s, cat, key, val))

    limit = max(1, limit)
    rows.sort(key=lambda r: (-r[0], r[2]))
    lines = [f"{cat}/{_pretty(key)}: {val}"
             for _s, cat, key, val in rows[:limit]]
    total = len(rows)

    # The semantic leg (Phase G1): facts whose MEANING matches, found
    # by embedding similarity rather than shared words, labelled as
    # similar matches and ranked after the keyword hits — a shared
    # word is evidence, a shared direction is a suggestion. Consulted
    # even when the keyword leg found nothing (that is the case it
    # exists for), and degrading to nothing, by design, whenever
    # embeddings are unavailable: keyword recall never depends on a
    # network.
    if words and len(lines) < limit:
        seen = {(cat, key) for _s, cat, key, _v in rows}
        emb = _embeddings_module()
        if emb is not None:
            for _sim, cat, key, val in emb.semantic_matches(
                    query, memory, limit=limit):
                if (cat, key) in seen:
                    continue
                seen.add((cat, key))
                lines.append(f"{cat}/{_pretty(key)}: {val} [similar match]")
                total += 1
                if len(lines) >= limit:
                    break

    if not lines:
        return (f"Nothing stored about '{query}'." if query
                else "I have not stored anything about this person yet.")

    head  = (f"Stored facts matching '{query}':" if query
             else "Everything currently stored:")
    more  = (f"\n(+{total - len(lines)} more — search with a narrower keyword)"
             if total > len(lines) else "")
    return head + "\n" + "\n".join(lines) + more


def all_entries_for_ui() -> list[dict]:
    """Flat list for the memory panel: what JARVIS knows, and when it learned it.
    Sorted newest first so the panel opens on what changed most recently."""
    memory = load_memory()
    rows = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            val = _entry_value(entry)
            if not val:
                continue
            rows.append({
                "category": cat,
                "key":      key,
                "value":    val,
                "updated":  (entry.get("updated", "") if isinstance(entry, dict) else ""),
            })
    rows.sort(key=lambda r: (r["updated"] or "0000-00-00"), reverse=True)
    return rows

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes", "playlist"}
    if category not in valid:
        category = "notes"
    cut: list[str] = []
    update_memory({category: {key: {"value": value}}}, cut)
    msg = f"Remembered: {category}/{key} = {value}"
    if key in cut:
        msg += (f" — shortened to {MAX_VALUE_LENGTH} characters to fit "
                f"the memory limit; the stored entry is marked as cut")
    return msg


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


def forget_by_keyword(query: str) -> tuple[int, list[str]]:
    """Delete every stored fact whose key or value contains `query`
    (case-insensitive) — the tool face of forgetting, mirroring ARIA's
    memory_forget_entries. The exact-key forget() above is the UI's
    per-entry button; this is the spoken form ("forget my old
    playlist"), so it matches the way a person names a memory, not
    the way the store keys it. Returns (count, ["category/key", ...])
    so the caller can say exactly what went. An empty query forgets
    nothing: a keyword-less delete-everything is not something a
    misheard word should be able to do."""
    q = (query or "").strip().lower()
    if not q:
        return 0, []
    memory = load_memory()
    gone: list[str] = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue                     # 'sessions' is a list, not facts
        for key in list(items.keys()):
            val = _entry_value(items[key])
            if q in key.lower() or q in val.lower():
                del items[key]
                gone.append(f"{cat}/{key}")
    if gone:
        save_memory(memory)
    return len(gone), gone


# ── Session memory ─────────────────────────────────────────────────────────────

_SESSION_MAX = 3   # safety cap — in practice 0-1 entries after pop


def save_session_summary(summary: str, language: str = "") -> None:
    """Append a 1-2 sentence session summary to long_term.json['sessions']."""
    summary = (summary or "").strip()
    if not summary:
        return
    memory   = load_memory()
    sessions = memory.get("sessions", [])
    if not isinstance(sessions, list):
        sessions = []
    entry: dict = {
        "date":    datetime.now().strftime("%Y-%m-%d"),
        "summary": summary[:280],
    }
    if language:
        entry["language"] = language
    sessions.append(entry)
    memory["sessions"] = sessions[-_SESSION_MAX:]
    _write_store(memory)
    print(f"[Memory] 📝 Session saved ({entry['date']}): {summary[:60]}…")


def pop_last_session() -> dict | None:
    """
    Return AND remove the most recent session entry.
    Calling this consumes the entry so it is never repeated in future briefings.
    """
    with _lock:
        if not MEMORY_PATH.exists():
            return None
        try:
            memory   = load_memory()
            sessions = memory.get("sessions", [])
            if not isinstance(sessions, list) or not sessions:
                return None
            entry = sessions.pop()          # remove the last entry
            memory["sessions"] = sessions
            _write_store(memory)
            return entry
        except Exception as e:
            print(f"[Memory] ⚠️ pop_last_session error: {e}")
            return None