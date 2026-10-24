"""
core/consolidation.py — the nightly memory consolidation pass.

THE PREMISE
    ARIA's memory is a store, not a process. Facts land in
    memory/long_term.json, session summaries pile up beside them,
    the journal keeps the days — and until now nothing ever
    reconciled any of it. Duplicates accumulated, stale values sat
    beside fresh ones, and the prompt's small core block is filled
    by recency, so clutter competed directly with what matters.

    This module is the editor. Once a night it reads everything
    new since the last run (journal days, session summaries) plus
    the current store, asks the one-shot model for a CHANGESET —
    promote / revise / retire / contradict, never free-form
    writes — validates the changeset hard, and applies it through
    the store's own atomic write path. What it retires is moved to
    memory/retired.json, never deleted. What genuinely conflicts
    is queued as a question for Alek in the morning briefing,
    never resolved by the model. And every successful run rewrites
    memory/resume_card.md, so the next boot starts from a thread
    instead of fragments.

HONESTY RULES (the load-bearing ones)
    * Changeset-only. The model proposes; this module disposes.
      A reply that is not schema-clean JSON is rejected WHOLE —
      no partial application of a malformed answer.
    * Never hard-delete. Retire = move to retired.json with the
      old value, the date and the reason. The store's pre-write
      backup rotation makes a whole run reversible besides.
    * Protected categories. Identity and relationships entries may
      be ADDED by a changeset, but a conflicting revise there is
      converted into a contradiction for Alek — the model does
      not get to decide who he is or who his people are.
    * Watermarks are evidence. journal_through and the session
      hashes advance only on success, and only over material that
      was actually sent to the model. A failed night retries the
      same window tomorrow; a window that fails twice is parked
      and reported, not retried forever.
    * Unbound = refuse. No model seam, no pass, no writes — the
      delegation pattern. A skipped night costs nothing.

DRIVING — two drivers, one gate. scheduler.tick() kicks the pass
(kick(), the module-owned-pass pattern pricecheck/screenwatch
use), and main.py runs a process-lifetime watch that calls
run_if_due() directly. The second driver exists because tick only
turns while a Live session is connected: at 03:00 with the engine
down there would be no tick, and the night's run would wait for
the next session. run_if_due() is the single gate both drivers
pass through; a module lock makes double-driving a no-op.

FILES (all under memory/, all git-ignored)
    consolidation.json    this pass's state: watermarks, the
                          contradiction queue, the queued briefing
                          note, the last 14 run records.
    retired.json          what retire moved out, restorable.
    resume_card.md        the thread for the next boot.
    consolidation_log.md  append-only audit of every change.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path


# ── Paths ────────────────────────────────────────────────────────────────────

def _base_dir() -> Path:
    # Same rule as core/journal.py: beside the executable when
    # frozen, the repo root when running from source.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


MEMORY_DIR   = _base_dir() / "memory"
STATE_PATH   = MEMORY_DIR / "consolidation.json"
RETIRED_PATH = MEMORY_DIR / "retired.json"
CARD_PATH    = MEMORY_DIR / "resume_card.md"
LOG_PATH     = MEMORY_DIR / "consolidation_log.md"


# ── Bounds ───────────────────────────────────────────────────────────────────

INPUT_CAP        = 14_000   # total chars of material per model call
STORE_CAP        = 6_000    # of that, what the store rendering may take
MAX_WRITES       = 25       # promotes + revises per run
MAX_RETIRES      = 10
MAX_CONTRADICTS  = 10
MAX_THREADS      = 5
THREAD_CHARS     = 120
CARD_CAP         = 1_200
CARD_STALE_H     = 48       # resume card older than this is not read
RUNS_KEPT        = 14
SKIP_RETRY_H     = 1        # a skipped pass retries no sooner than this

# Categories a conflicting revise may never touch by model say-so:
# additions are fine; conflicts become questions for Alek.
PROTECTED = ("identity", "relationships")


# ── The model seam (bound by main.py, the delegation pattern) ────────────────

_propose = None        # (system: str, user: str) -> str


def bind(propose=None) -> None:
    """Wire the one-shot model seam. Unbound, the pass refuses:
    no model, no edits — a memory editor that cannot read the
    material has no business rewriting it."""
    global _propose
    _propose = propose


# ── State ────────────────────────────────────────────────────────────────────

_run_lock   = threading.Lock()    # one pass at a time, across drivers
_state_lock = threading.Lock()    # state-file read-modify-write


def _empty_state() -> dict:
    return {
        "last_run_at":     None,
        "last_success_at": None,
        "last_status":     None,
        "journal_through": None,    # "YYYY-MM-DD" of the last sent day
        "session_hashes":  [],
        "contradictions":  [],      # {category, key, existing, proposed,
                                    #  question, asked_on}
        "pending_note":    None,    # queued one-line briefing summary
        "open_threads":    [],      # last threads the editor reported
        "window_key":      None,    # material signature of the failed window
        "window_failures": 0,
        "runs":            [],      # last RUNS_KEPT run records
    }


def _load_state() -> dict:
    """State is rebuildable — a corrupt or missing file is a fresh
    state, never an exception into a driver."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return _empty_state()
    if not isinstance(data, dict):
        return _empty_state()
    state = _empty_state()
    state.update({k: v for k, v in data.items() if k in state})
    return state


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _save_state(state: dict) -> None:
    with _state_lock:
        _atomic_write(STATE_PATH,
                      json.dumps(state, indent=2, ensure_ascii=False))


def _record_run(state: dict, now: datetime, status: str,
                counts: dict | None = None, note: str = "") -> None:
    counts = counts or {}
    state["last_run_at"] = _fmt_dt(now)
    state["last_status"] = status
    if status == "ok":
        state["last_success_at"] = _fmt_dt(now)
    rec = {"at": _fmt_dt(now), "status": status}
    for k in ("promoted", "revised", "retired", "contradictions",
              "days", "sessions"):
        rec[k] = int(counts.get(k, 0))
    if note:
        rec["note"] = note
    state["runs"] = (state.get("runs") or [])[-RUNS_KEPT + 1:] + [rec]


# ── Small helpers ────────────────────────────────────────────────────────────

def _fmt_dt(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse_dt(text) -> datetime | None:
    if not text or not isinstance(text, str):
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _slot_start(now: datetime, hour: int) -> datetime:
    """The most recent daily consolidation slot at or before now."""
    slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if now < slot:
        slot -= timedelta(days=1)
    return slot


_KEY_JUNK = re.compile(r"[^a-z0-9_]+")


def _normalize_key(key) -> str:
    """Store keys are snake_case (memory_manager._pretty renders
    them by turning underscores back into spaces). Normalise the
    way a spoken 'tea order' lands as tea_order: lower, words
    joined by underscores, nothing else kept."""
    text = str(key or "").strip().lower()
    text = re.sub(r"[\s\-]+", "_", text)
    text = _KEY_JUNK.sub("", text)
    text = re.sub(r"_{2,}", "_", text).strip("_")
    return text[:60]


def _entry_value(entry) -> str:
    """Both store shapes — {'value': ...} dicts and the bare
    strings early versions wrote (memory_manager._entry_value)."""
    if isinstance(entry, dict):
        return str(entry.get("value", "") or "").strip()
    return str(entry or "").strip()


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ── Collection ───────────────────────────────────────────────────────────────

class _Window:
    """The material one pass works on: what was gathered, what of
    it fit the budget and was actually sent, and its signature."""

    def __init__(self) -> None:
        self.user_text: str = ""
        self.sent_days: list[str] = []        # journal dates sent
        self.sent_hashes: list[str] = []      # session hashes sent
        self.all_days: list[str] = []         # every new journal date
        self.all_hashes: list[str] = []       # every new session hash
        self.key: str = ""

    @property
    def has_material(self) -> bool:
        return bool(self.all_days or self.all_hashes)


_DAY_FILE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def _journal_days(through: str | None) -> list[tuple[str, str]]:
    """(date, text) for every journal day after the watermark,
    oldest first. Reads the day files directly — journal.read_notes
    returns only the 'Note to self:' entries, and consolidation
    edits from the whole day, not just its notes."""
    from core import journal
    days: list[tuple[str, str]] = []
    try:
        files = sorted(journal.JOURNAL_DIR.iterdir())
    except Exception:
        return days
    for path in files:
        m = _DAY_FILE.match(path.name)
        if not m:
            continue
        day = m.group(1)
        if through and day <= through:
            continue
        try:
            days.append((day, path.read_text(encoding="utf-8",
                                             errors="replace")))
        except Exception:
            continue
    return days


def _new_sessions(store: dict, known: list[str]) -> list[tuple[str, dict]]:
    """(hash, entry) for session summaries not yet consolidated."""
    out: list[tuple[str, dict]] = []
    sessions = store.get("sessions")
    if not isinstance(sessions, list):
        return out
    known_set = set(known or [])
    for entry in sessions:
        if not isinstance(entry, dict):
            continue
        sig = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        h = _hash(sig)
        if h not in known_set:
            out.append((h, entry))
    return out


def _store_section(store: dict) -> str:
    """The current memory as the editor needs it: full entries,
    most recently updated first, until the store budget runs out —
    then the remaining keys as an index, so she never re-promotes
    a fact she already holds just because its value was cut."""
    from memory import memory_manager as mm
    entries: list[tuple[str, str, str, str]] = []
    for cat in mm.FACT_CATEGORIES:
        items = store.get(cat)
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            updated = (entry.get("updated", "")
                       if isinstance(entry, dict) else "")
            entries.append((updated or "", cat, key,
                            _entry_value(entry)))
    entries.sort(key=lambda t: t[0], reverse=True)
    lines = ["CURRENT MEMORY (what she already knows):"]
    used = len(lines[0]) + 1
    overflow: list[str] = []
    for _upd, cat, key, val in entries:
        line = f"{cat}/{key} = {val}"
        if used + len(line) + 1 <= STORE_CAP:
            lines.append(line)
            used += len(line) + 1
        else:
            overflow.append(f"{cat}/{key}")
    if overflow:
        lines.append("Also stored (values omitted for space): "
                     + ", ".join(overflow))
    return "\n".join(lines)


def _gather(store: dict, state: dict) -> _Window:
    """Collect the new material and fit it to the input budget.
    The watermark may only ever advance over what lands in
    sent_days / sent_hashes — the budget decides, oldest first,
    and whatever does not fit carries to the next run."""
    win = _Window()
    days = _journal_days(state.get("journal_through"))
    sessions = _new_sessions(store, state.get("session_hashes") or [])
    win.all_days = [d for d, _t in days]
    win.all_hashes = [h for h, _e in sessions]

    store_text = _store_section(store)
    session_lines = [f"[SESSION {e.get('date', '')}] "
                     f"{e.get('summary', '')}" for _h, e in sessions]
    session_text = ("NEW SESSION SUMMARIES:\n" + "\n".join(session_lines)
                    if session_lines else "")

    parts = [store_text]
    used = len(store_text)
    if session_text:
        parts.append(session_text)
        used += len(session_text) + 2
        win.sent_hashes = list(win.all_hashes)
    budget = INPUT_CAP - used - 60        # footer + join slack
    journal_parts: list[str] = []
    for day, text in days:
        block = f"[JOURNAL {day}]\n{text.strip()}"
        if len(block) <= budget:
            journal_parts.append(block)
            budget -= len(block) + 2
            win.sent_days.append(day)
        elif not journal_parts:
            # One day larger than the whole journal budget would
            # stall the watermark forever if it were carried whole;
            # send it truncated and say so in the block itself.
            cut = text.strip()[:max(0, budget - 40)]
            journal_parts.append(
                f"[JOURNAL {day} — truncated to fit]\n{cut}")
            win.sent_days.append(day)
            budget = 0
        else:
            break
    if journal_parts:
        parts.append("JOURNAL (new days):\n" + "\n\n".join(journal_parts))
    parts.append("Return the changeset JSON only.")
    win.user_text = "\n\n".join(parts)

    store_sig = _hash(json.dumps(
        {c: store.get(c) for c in
         ("identity", "preferences", "projects", "relationships",
          "wishes", "notes")},
        sort_keys=True, ensure_ascii=False, default=str))
    win.key = _hash("|".join([
        state.get("journal_through") or "-",
        ",".join(win.all_days),
        ",".join(sorted(win.all_hashes)),
        store_sig,
    ]))
    return win


# ── The changeset: proposal contract ─────────────────────────────────────────

_SYSTEM = """You are ARIA's memory editor. Once a night you tidy what she knows about the person she serves, working ONLY from the material given: the current memory, and everything new since the last tidy (journal days, session summaries).

Return a single JSON object and nothing else — no prose, no code fences. Schema:
{
  "promote":       [{"category": "...", "key": "...", "value": "...", "reason": "..."}],
  "revise":        [{"category": "...", "key": "...", "value": "...", "reason": "..."}],
  "retire":        [{"category": "...", "key": "...", "reason": "...", "superseded_by": "..."}],
  "contradictions": [{"category": "...", "key": "...", "existing": "...", "proposed": "...", "question": "..."}],
  "threads":       ["short open thread or promise from the journal"]
}

Categories are exactly: identity, preferences, projects, relationships, wishes, notes. Keys are snake_case. Values at most 380 characters.

Rules:
- Prefer revising an existing key over promoting a near-duplicate of it.
- A fact the person stated himself outranks anything inferred; inferred facts say so in their reason.
- When two values for the same key genuinely conflict, do NOT choose — file a contradiction with a short question he can answer.
- Retire only what the material shows is stale, superseded or finished — never merely old.
- Empty lists are a fine answer. Most nights change little; inventing changes is worse than none.
- threads: at most 5 short lines naming open loops the journal leaves hanging."""


class _Invalid(Exception):
    """The reply is not a usable changeset; reject it whole."""


def _check_write(item, where: str) -> dict:
    from memory import memory_manager as mm
    if not isinstance(item, dict):
        raise _Invalid(f"{where}: item is not an object")
    cat = str(item.get("category") or "").strip()
    if cat not in mm.FACT_CATEGORIES:
        raise _Invalid(f"{where}: unknown category {cat!r}")
    key = _normalize_key(item.get("key"))
    if not key:
        raise _Invalid(f"{where}: key normalises to nothing")
    value = item.get("value")
    if not isinstance(value, str) or not value.strip():
        raise _Invalid(f"{where}: value missing or empty")
    if len(value) > mm.MAX_VALUE_LENGTH:
        raise _Invalid(f"{where}: value over {mm.MAX_VALUE_LENGTH} chars")
    reason = item.get("reason", "")
    if not isinstance(reason, str):
        raise _Invalid(f"{where}: reason is not text")
    return {"category": cat, "key": key, "value": value.strip(),
            "reason": reason.strip()}


def _check_retire(item) -> dict:
    from memory import memory_manager as mm
    if not isinstance(item, dict):
        raise _Invalid("retire: item is not an object")
    cat = str(item.get("category") or "").strip()
    if cat not in mm.FACT_CATEGORIES:
        raise _Invalid(f"retire: unknown category {cat!r}")
    key = _normalize_key(item.get("key"))
    if not key:
        raise _Invalid("retire: key normalises to nothing")
    reason = item.get("reason", "")
    superseded = item.get("superseded_by", "")
    if not isinstance(reason, str) or not isinstance(superseded, str):
        raise _Invalid("retire: reason/superseded_by is not text")
    return {"category": cat, "key": key, "reason": reason.strip(),
            "superseded_by": superseded.strip()}


def _check_contradiction(item) -> dict:
    from memory import memory_manager as mm
    if not isinstance(item, dict):
        raise _Invalid("contradictions: item is not an object")
    cat = str(item.get("category") or "").strip()
    if cat not in mm.FACT_CATEGORIES:
        raise _Invalid(f"contradictions: unknown category {cat!r}")
    key = _normalize_key(item.get("key"))
    if not key:
        raise _Invalid("contradictions: key normalises to nothing")
    existing = item.get("existing")
    proposed = item.get("proposed")
    for field, val in (("existing", existing), ("proposed", proposed)):
        if not isinstance(val, str) or not val.strip():
            raise _Invalid(f"contradictions: {field} missing or empty")
        if len(val) > mm.MAX_VALUE_LENGTH:
            raise _Invalid("contradictions: value over the cap")
    if existing.strip() == proposed.strip():
        raise _Invalid("contradictions: existing and proposed agree")
    question = item.get("question", "")
    if not isinstance(question, str):
        raise _Invalid("contradictions: question is not text")
    return {"category": cat, "key": key, "existing": existing.strip(),
            "proposed": proposed.strip(), "question": question.strip(),
            "asked_on": None}


def validate_changeset(text: str) -> dict:
    """Parse and hard-validate a model reply into a changeset:
    {writes, retires, contradictions, threads}. ANY violation
    raises _Invalid and the whole reply is rejected — a memory
    editor's malformed answer never lands in pieces."""
    if not text or not text.strip():
        raise _Invalid("empty reply")
    body = text.strip()
    if body.startswith("```"):
        body = body.strip("`")
        if body.lower().startswith("json"):
            body = body[4:]
        body = body.strip()
    try:
        data = json.loads(body)
    except Exception as e:
        raise _Invalid(f"not JSON: {e}")
    if not isinstance(data, dict):
        raise _Invalid("top level is not an object")
    unknown = set(data) - {"promote", "revise", "retire",
                           "contradictions", "threads"}
    if unknown:
        raise _Invalid(f"unknown sections: {sorted(unknown)}")
    for name in ("promote", "revise", "retire", "contradictions",
                 "threads"):
        if name in data and not isinstance(data[name], list):
            raise _Invalid(f"{name} is not a list")

    writes = [_check_write(it, "promote")
              for it in data.get("promote", [])]
    writes += [_check_write(it, "revise")
               for it in data.get("revise", [])]
    if len(writes) > MAX_WRITES:
        raise _Invalid(f"too many writes: {len(writes)} > {MAX_WRITES}")
    retires = [_check_retire(it) for it in data.get("retire", [])]
    if len(retires) > MAX_RETIRES:
        raise _Invalid(f"too many retires: {len(retires)} > {MAX_RETIRES}")
    contras = [_check_contradiction(it)
               for it in data.get("contradictions", [])]
    if len(contras) > MAX_CONTRADICTS:
        raise _Invalid(f"too many contradictions: {len(contras)}")

    seen: set[tuple[str, str]] = set()
    for item in writes:
        slot = (item["category"], item["key"])
        if slot in seen:
            raise _Invalid(f"duplicate write for {slot[0]}/{slot[1]}")
        seen.add(slot)
    retired_slots: set[tuple[str, str]] = set()
    for item in retires:
        slot = (item["category"], item["key"])
        if slot in seen:
            raise _Invalid(f"{slot[0]}/{slot[1]} is both written "
                           "and retired")
        if slot in retired_slots:
            raise _Invalid(f"duplicate retire for {slot[0]}/{slot[1]}")
        retired_slots.add(slot)

    threads: list[str] = []
    for raw in data.get("threads", []):
        if isinstance(raw, str) and raw.strip():
            threads.append(raw.strip()[:THREAD_CHARS])
        if len(threads) >= MAX_THREADS:
            break
    return {"writes": writes, "retires": retires,
            "contradictions": contras, "threads": threads}


# ── Classification & application ───────────────────────────────────────────────

def _classify(changeset: dict, store: dict):
    """Ground truth decides what a write IS — the model's own
    promote/revise labels are a claim, the store is the record.
    A conflicting revise in a protected category is not applied
    at all: it becomes a contradiction for Alek."""
    adds, revises, contras = [], [], list(changeset["contradictions"])
    for item in changeset["writes"]:
        cat, key = item["category"], item["key"]
        bucket = store.get(cat)
        current = bucket.get(key) if isinstance(bucket, dict) else None
        if current is None:
            adds.append(item)
            continue
        if _entry_value(current) == item["value"]:
            continue                      # already known — no-op
        if cat in PROTECTED:
            contras.append({
                "category": cat, "key": key,
                "existing": _entry_value(current),
                "proposed": item["value"], "question": "",
                "asked_on": None})
        else:
            revises.append(item)
    return adds, revises, contras


def _apply(store: dict, adds, revises, retires, today: str):
    """Mutate the loaded store dict. Returns (changed, retired
    records). Idempotent by construction: re-applying the same
    changeset converges — values are set, not appended, and a
    retire of an absent key is a no-op."""
    changed = False
    records: list[dict] = []
    for item in adds + revises:
        bucket = store.setdefault(item["category"], {})
        entry = {"value": item["value"], "updated": today}
        if bucket.get(item["key"]) != entry:
            bucket[item["key"]] = entry
            changed = True
    for item in retires:
        bucket = store.get(item["category"])
        if not isinstance(bucket, dict) or item["key"] not in bucket:
            continue
        records.append({
            "category": item["category"], "key": item["key"],
            "value": _entry_value(bucket[item["key"]]),
            "retired_at": today, "reason": item["reason"],
            "superseded_by": item["superseded_by"]})
        del bucket[item["key"]]
        changed = True
    return changed, records


def _append_retired(records: list[dict]) -> None:
    if not records:
        return
    try:
        existing = json.loads(RETIRED_PATH.read_text(encoding="utf-8"))
        if not isinstance(existing, list):
            existing = []
    except Exception:
        existing = []
    existing.extend(records)
    _atomic_write(RETIRED_PATH,
                  json.dumps(existing, indent=2, ensure_ascii=False))


def _append_log(now: datetime, adds, revises, records, contras) -> None:
    if not (adds or revises or records or contras):
        return
    lines = [f"\n## {now.strftime('%Y-%m-%d %H:%M')} — consolidation"]
    for it in adds:
        lines.append(f"- new: {it['category']}/{it['key']} = "
                     f"{it['value']!r} ({it['reason'] or 'no reason given'})")
    for it in revises:
        lines.append(f"- updated: {it['category']}/{it['key']} = "
                     f"{it['value']!r} ({it['reason'] or 'no reason given'})")
    for rec in records:
        lines.append(f"- retired: {rec['category']}/{rec['key']} "
                     f"(was {rec['value']!r}; {rec['reason'] or 'no reason'}"
                     + (f"; superseded by {rec['superseded_by']}"
                        if rec["superseded_by"] else "") + ")")
    for c in contras:
        lines.append(f"- question queued: {c['category']}/{c['key']} — "
                     f"existing {c['existing']!r} vs proposed "
                     f"{c['proposed']!r}")
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception:
        pass          # the audit log is a courtesy, never a blocker


# ── Contradiction queue ──────────────────────────────────────────────────────

def _prune_contradictions(state: dict, store: dict) -> bool:
    """Drop queued questions the store shows are settled: if the
    current value IS the proposed one, Alek has answered (his
    answer lands through the ordinary remember path) and the
    question must not be asked again. Returns True if it pruned."""
    queue = state.get("contradictions") or []
    kept = []
    pruned = False
    for c in queue:
        bucket = store.get(c.get("category"))
        current = (bucket.get(c.get("key"))
                   if isinstance(bucket, dict) else None)
        if current is not None and _entry_value(current) == c.get("proposed"):
            pruned = True
            continue
        kept.append(c)
    if pruned:
        state["contradictions"] = kept
    return pruned


def _queue_contradictions(state: dict, contras) -> list:
    """Append new contradictions, skipping exact duplicates of
    ones already waiting. Returns the items actually queued."""
    queue = state.setdefault("contradictions", [])
    have = {(c["category"], c["key"], c["proposed"]) for c in queue}
    added = []
    for c in contras:
        sig = (c["category"], c["key"], c["proposed"])
        if sig in have:
            continue
        have.add(sig)
        queue.append(c)
        added.append(c)
    return added


def _default_question(c: dict) -> str:
    pretty = str(c.get("key", "")).replace("_", " ")
    return (f"you told me {c.get('proposed')} for {pretty}, but I "
            f"have {c.get('existing')} noted — which is right?")


def briefing_note(now: datetime | None = None) -> str:
    """The morning briefing's consolidation line: the queued
    one-line summary (consumed by being read, like the stores'
    recovery notes) plus at most ONE waiting memory question.
    A question is asked at most once per day and stays queued
    until the store shows it settled. "" when there is nothing
    to say. Never raises — a broken state must not cost a briefing."""
    try:
        now = now or datetime.now()
        state = _load_state()
        changed = False
        parts: list[str] = []
        if state.get("pending_note"):
            parts.append(state["pending_note"])
            state["pending_note"] = None
            changed = True
        if state.get("contradictions"):
            from memory import memory_manager as mm
            if _prune_contradictions(state, mm.load_memory()):
                changed = True
            today = now.date().isoformat()
            for c in state.get("contradictions") or []:
                if c.get("asked_on") != today:
                    q = c.get("question") or _default_question(c)
                    parts.append(f"Quick memory check: {q}")
                    c["asked_on"] = today
                    changed = True
                    break
        if changed:
            _save_state(state)
        return " ".join(parts)
    except Exception:
        return ""


# ── Resume card ──────────────────────────────────────────────────────────────

def _render_card(now: datetime, store: dict, state: dict,
                 counts: dict) -> str:
    lines = [
        "# Resume card",
        f"Last consolidated: {now.strftime('%Y-%m-%d')}.",
        (f"Memory: {counts.get('promoted', 0)} new, "
         f"{counts.get('revised', 0)} updated, "
         f"{counts.get('retired', 0)} retired this run. "
         f"Questions waiting for Alek: "
         f"{len(state.get('contradictions') or [])}."),
        "",
        "Active projects (most recently touched):",
    ]
    projects = store.get("projects")
    rows = []
    if isinstance(projects, dict):
        for key, entry in projects.items():
            updated = (entry.get("updated", "")
                       if isinstance(entry, dict) else "")
            rows.append((updated or "", key, _entry_value(entry)))
    rows.sort(reverse=True)
    if rows:
        for _u, key, val in rows[:4]:
            lines.append(f"- {key.replace('_', ' ')}: {val}"[:160])
    else:
        lines.append("- none recorded")
    lines += ["", "Open threads:"]
    threads = state.get("open_threads") or []
    if threads:
        lines += [f"- {t}" for t in threads[:MAX_THREADS]]
    else:
        lines.append("- none")
    text = "\n".join(lines) + "\n"
    if len(text) > CARD_CAP:
        cut = text[:CARD_CAP - 1]
        text = cut[:cut.rfind("\n")] + "…\n"
    return text


def resume_card_text(now: datetime | None = None) -> str | None:
    """The card for the session context, or None when there is no
    card or it has gone stale (>48h) — a stale thread is worse
    than none, because she would speak from it as if it were
    current."""
    try:
        if not CARD_PATH.exists():
            return None
        now = now or datetime.now()
        mtime = datetime.fromtimestamp(CARD_PATH.stat().st_mtime)
        if now - mtime > timedelta(hours=CARD_STALE_H):
            return None
        return CARD_PATH.read_text(encoding="utf-8") or None
    except Exception:
        return None


# ── The pass ─────────────────────────────────────────────────────────────────

def _note_line(adds: int, revises: int, retired: int,
               questions: int) -> str | None:
    changed = adds + revises + retired
    if not changed and not questions:
        return None
    if changed:
        note = (f"Overnight I tidied my memory: {adds} new "
                f"fact{'s' if adds != 1 else ''}, {revises} updated, "
                f"{retired} retired")
    else:
        note = "Overnight I tidied my memory — nothing needed changing"
    if questions:
        note += (f", and {questions} thing{'s' if questions != 1 else ''} "
                 f"to check with you")
    return note + "."


def _execute(win: _Window, store: dict, state: dict,
             now: datetime) -> list[str]:
    """Run the pass over a gathered window. The caller (run_if_due)
    owns the due decision and the run lock; this owns the work."""
    from memory import memory_manager as mm

    if _propose is None:
        _record_run(state, now, "skipped", note="model not bound")
        _save_state(state)
        return ["consolidation skipped — model not bound"]

    if not win.has_material:
        counts = {"days": 0, "sessions": 0}
        _record_run(state, now, "ok", counts)
        card = _render_card(now, store, state,
                            {"promoted": 0, "revised": 0, "retired": 0})
        _atomic_write(CARD_PATH, card)
        _save_state(state)
        return ["consolidation: nothing new to consolidate"]

    try:
        reply = _propose(_SYSTEM, win.user_text)
    except Exception as e:
        _record_run(state, now, "skipped", note=f"model error: {e}"[:120])
        _save_state(state)
        return [f"consolidation skipped — model error: {e}"]
    if not reply or not str(reply).strip():
        _record_run(state, now, "skipped", note="empty reply")
        _save_state(state)
        return ["consolidation skipped — empty model reply"]

    try:
        changeset = validate_changeset(str(reply))
    except _Invalid as e:
        if state.get("window_key") == win.key:
            state["window_failures"] = int(
                state.get("window_failures") or 0) + 1
        else:
            state["window_key"] = win.key
            state["window_failures"] = 1
        parked = state["window_failures"] >= 2
        if parked:
            state["pending_note"] = (
                "A memory tidy-up keeps failing on the same "
                "material, so I've parked it until something new "
                "arrives — the details are in my consolidation log.")
            try:
                LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
                with open(LOG_PATH, "a", encoding="utf-8") as f:
                    f.write(f"\n## {now.strftime('%Y-%m-%d %H:%M')} — "
                            f"window parked after repeated failures "
                            f"({e})\n")
            except Exception:
                pass
        _record_run(state, now, "failed", note=str(e)[:120])
        _save_state(state)
        return [f"consolidation failed — reply rejected ({e}); "
                "the same window retries tomorrow"]

    today = now.date().isoformat()
    adds, revises, contras = _classify(changeset, store)
    changed, records = _apply(store, adds, revises,
                               changeset["retires"], today)
    if changed:
        mm.save_memory(store)
    _append_retired(records)
    _prune_contradictions(state, store)
    new_contras = _queue_contradictions(state, contras)
    queued = len(new_contras)
    if changeset["threads"]:
        state["open_threads"] = changeset["threads"]
    _append_log(now, adds, revises, records, new_contras)

    if win.sent_days:
        state["journal_through"] = win.sent_days[-1]
    known = list(state.get("session_hashes") or [])
    for h in win.sent_hashes:
        if h not in known:
            known.append(h)
    state["session_hashes"] = known[-64:]
    state["window_key"] = None
    state["window_failures"] = 0

    counts = {"promoted": len(adds), "revised": len(revises),
              "retired": len(records), "contradictions": queued,
              "days": len(win.sent_days),
              "sessions": len(win.sent_hashes)}
    note = _note_line(len(adds), len(revises), len(records), queued)
    if note:
        state["pending_note"] = note
    _record_run(state, now, "ok", counts)
    _atomic_write(CARD_PATH, _render_card(now, store, state, counts))
    _save_state(state)
    return [f"consolidation ran: {len(adds)} new, {len(revises)} "
            f"updated, {len(records)} retired, {queued} "
            f"question{'s' if queued != 1 else ''} queued"]


def _is_due(state: dict, settings: dict, now: datetime,
            startup: bool, win: _Window) -> bool:
    """Tonight's run is due when the daily slot has passed without
    a success — or, at startup only, when the last success is more
    than 20h old (the missed-night catch-up). A junk-reply failure
    retries at the NEXT slot, never the same one; a skip retries
    after an hour; a twice-failed window is parked until its
    material changes."""
    if not settings.get("consolidation_enabled", True):
        return False
    hour = int(settings.get("consolidation_hour", 3))
    slot = _slot_start(now, hour)
    last_success = _parse_dt(state.get("last_success_at"))
    last_attempt = _parse_dt(state.get("last_run_at"))
    slot_due = last_success is None or last_success < slot
    catchup = bool(startup and (last_success is None or
                                now - last_success >
                                timedelta(hours=20)))
    if not (slot_due or catchup):
        return False
    if (state.get("window_failures") or 0) >= 2 \
            and state.get("window_key") == win.key:
        return False                      # parked window, unchanged
    if state.get("last_status") == "failed" \
            and last_attempt is not None and last_attempt >= slot:
        return False                      # junk retries tomorrow
    if state.get("last_status") == "skipped" \
            and last_attempt is not None \
            and now - last_attempt < timedelta(hours=SKIP_RETRY_H):
        return False                      # don't hammer a down model
    return True


def run_if_due(now: datetime | None = None, *,
               startup: bool = False) -> list[str]:
    """The one gate both drivers pass through. Returns log lines —
    [] when tonight's run is not due (the ordinary case, many
    times a day). Never raises into a driver."""
    if not _run_lock.acquire(blocking=False):
        return []
    try:
        now = now or datetime.now()
        try:
            from memory import config_manager, memory_manager as mm
            settings = config_manager.get_consolidation_settings()
            if not settings.get("consolidation_enabled", True):
                return []
            state = _load_state()
            store = mm.load_memory()
            win = _gather(store, state)
            if not _is_due(state, settings, now, startup, win):
                return []
            return _execute(win, store, state, now)
        except Exception as e:
            return [f"consolidation error: {e}"]
    finally:
        _run_lock.release()


def kick(now: datetime | None = None) -> bool:
    """Start the pass on a daemon thread if one is not already
    running — the scheduler tick's way in, so a slow model call
    can never stall the tick loop. True when a thread started."""
    if _run_lock.locked():
        return False
    thread = threading.Thread(target=run_if_due, args=(now,),
                              name="consolidation", daemon=True)
    thread.start()
    return True
