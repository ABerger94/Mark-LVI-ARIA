"""
core/journal.py — the dated journal and voice notes (ARIA's, ported).

ARIA kept a journal as dated markdown files — one per day, a
"# Tuesday, October 21, 2026" header on a new file, each entry under
a "## 02:35 PM" time heading — and her voice notes lived IN that
journal, prefixed "Note to self:", with a second copy in her memory
store so read_notes could find them by date.

The port keeps the files and the formats, and changes the storage
answer for the second copy: it does not go into long_term.json. That
file is a capped fact store with a trim discipline (380-char values,
oldest-first eviction) — a note is not a fact, and a journal silently
trimmed by the fact store's caps is a journal with holes. Following
the routines precedent (memory/routines/, beside the store, not in
it), the journal lives at memory/journal/YYYY-MM-DD.md, and notes
are read back out of the day's file — the sections are regular
enough (time heading + body) that the journal is its own index.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

NOTE_PREFIX = "Note to self:"


def _base_dir() -> Path:
    # Same rule as core/routines.py: beside the executable when
    # frozen, the repo root when running from source.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch this constant to a tmp dir (the routines pattern).
JOURNAL_DIR = _base_dir() / "memory" / "journal"


def _journal_dir() -> Path:
    return JOURNAL_DIR


def _day_path(day: datetime) -> Path:
    return _journal_dir() / (day.strftime("%Y-%m-%d") + ".md")


def write_entry(entry: str, now: datetime | None = None) -> Path:
    """Append one entry to today's journal file. Returns the path.

    Format is ARIA's: a new file opens with the "# Weekday, Month DD,
    YYYY" header; every entry sits under its own "## HH:MM AM" time
    heading, so the file reads as a journal, not a log dump."""
    day = now or datetime.now()
    _journal_dir().mkdir(parents=True, exist_ok=True)
    path = _day_path(day)
    new = not path.exists()
    with open(path, "a", encoding="utf-8") as f:
        if new:
            f.write(f"# {day.strftime('%A, %B %d, %Y')}\n\n")
        f.write(f"## {day.strftime('%I:%M %p')}\n{(entry or '').strip()}\n\n")
    return path


def take_note(text: str, now: datetime | None = None) -> Path:
    """A voice note: a journal entry carrying the note prefix, which
    is what read_notes looks for. ARIA also copied notes into her
    memory store; see the module docstring for why this does not."""
    return write_entry(f"{NOTE_PREFIX} {(text or '').strip()}", now=now)


def resolve_note_date(s: str, now: datetime | None = None) -> str:
    """ARIA's resolver, verbatim: 'today', 'yesterday', a weekday name
    (the most recent PAST occurrence — naming today gets last week's),
    or a YYYY-MM-DD date. Anything unparseable falls back to today."""
    s = (s or "").strip().lower()
    now = now or datetime.now()
    if s in ("today", ""):
        return now.strftime("%Y-%m-%d")
    if s == "yesterday":
        return (now - timedelta(days=1)).strftime("%Y-%m-%d")
    weekdays = ["monday", "tuesday", "wednesday", "thursday", "friday",
                "saturday", "sunday"]
    if s in weekdays:
        target = weekdays.index(s)
        diff = (now.weekday() - target) % 7
        if diff == 0:
            diff = 7
        return (now - timedelta(days=diff)).strftime("%Y-%m-%d")
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return s
    except Exception:
        return now.strftime("%Y-%m-%d")


def read_notes(date_str: str = "today",
               now: datetime | None = None) -> tuple[str, list[tuple[str, str]]]:
    """(resolved day, [(time, text), ...]) for the day's notes.

    Parses the day's journal file: a section is a note when its body
    starts with the note prefix. Journal entries without the prefix
    are journal, not notes, and are not returned."""
    day = resolve_note_date(date_str, now=now)
    path = _journal_dir() / (day + ".md")
    if not path.exists():
        return day, []
    notes: list[tuple[str, str]] = []
    current_time = ""
    body: list[str] = []

    def _flush():
        text = "\n".join(body).strip()
        if current_time and text.startswith(NOTE_PREFIX):
            notes.append((current_time, text[len(NOTE_PREFIX):].strip()))

    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            _flush()
            current_time = line[3:].strip()
            body = []
        elif line.startswith("# "):
            continue
        else:
            body.append(line)
    _flush()
    return day, notes
