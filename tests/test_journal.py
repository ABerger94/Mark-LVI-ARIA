"""Phase F-REST group 2 — the notes trio (ARIA's, ported).

Pinned shut:
  * notes round-trip: take_note lands in today's dated journal file
    and read_notes finds it, with journal_write entries NOT leaking
    into the notes read (ARIA's stores were separate reads);
  * the date resolver keeps ARIA's exact semantics — a weekday name
    means the most recent PAST occurrence, never today itself;
  * nothing is written into long_term.json: the journal is its own
    store beside memory, not a tenant of the capped fact store.
"""
from datetime import datetime

import pytest

from core import journal
from actions import take_note as take_note_action
from actions import read_notes as read_notes_action
from actions import journal_write as journal_write_action


# 2026-10-21 is a Wednesday.
NOW = datetime(2026, 10, 21, 14, 32)


class _FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return NOW


@pytest.fixture(autouse=True)
def _journal_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(journal, "JOURNAL_DIR", tmp_path / "journal")
    monkeypatch.setattr(journal, "datetime", _FrozenDateTime)
    return tmp_path / "journal"


def test_note_round_trip():
    result = take_note_action.take_note({"text": "buy more mealworms"})
    assert "status=done" in result and "Noted." in result

    day, notes = journal.read_notes("today", now=NOW)
    assert day == "2026-10-21"
    assert notes == [("02:32 PM", "buy more mealworms")]


def test_read_notes_action_lists_the_day():
    take_note_action.take_note({"text": "first"})
    take_note_action.take_note({"text": "second"})
    result = read_notes_action.read_notes({})
    assert "count=2" in result
    assert "first" in result and "second" in result


def test_journal_entries_are_not_notes():
    journal_write_action.journal_write({"entry": "A long day of coding."})
    take_note_action.take_note({"text": "actual note"})
    day, notes = journal.read_notes("today", now=NOW)
    assert [t for _w, t in notes] == ["actual note"]
    # The journal entry is still in the file, under its own heading.
    text = (journal.JOURNAL_DIR / "2026-10-21.md").read_text(
        encoding="utf-8")
    assert "A long day of coding." in text
    assert text.startswith("# Wednesday, October 21, 2026")


def test_read_notes_empty_day():
    result = read_notes_action.read_notes({})
    assert "status=done" in result and "count=0" in result
    assert "No notes from" in result


def test_empty_note_fails_honestly():
    assert "status=failed" in take_note_action.take_note({"text": "  "})
    assert "status=failed" in journal_write_action.journal_write(
        {"entry": ""})


@pytest.mark.parametrize("given,expected", [
    ("today", "2026-10-21"),
    ("", "2026-10-21"),
    ("yesterday", "2026-10-20"),
    ("wednesday", "2026-10-14"),   # today IS Wednesday -> last week's
    ("tuesday", "2026-10-20"),
    ("thursday", "2026-10-15"),
    ("2026-09-30", "2026-09-30"),
    ("garbage", "2026-10-21"),     # unparseable -> today (ARIA's fallback)
])
def test_resolve_note_date_aria_semantics(given, expected):
    assert journal.resolve_note_date(given, now=NOW) == expected


def test_notes_stay_out_of_long_term_facts(_journal_dir):
    from memory import memory_manager
    before = memory_manager.load_memory()
    take_note_action.take_note({"text": "not a fact"})
    after = memory_manager.load_memory()
    assert before == after
