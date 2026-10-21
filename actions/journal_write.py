"""
actions/journal_write.py — write a journal entry (ARIA's, ported).

Appends the entry to today's dated journal file (memory/journal/
YYYY-MM-DD.md). A journal entry is a record, not a note: it is not
returned by read_notes (which reads note-prefixed entries only),
exactly as ARIA's journal and note stores were separate reads over
one habit of writing things down.
"""

from __future__ import annotations

from core import journal, outcome


def journal_write(parameters: dict = None, response=None, player=None,
                  session_memory=None) -> str:
    params = parameters or {}
    entry = (params.get("entry") or "").strip()
    if not entry:
        return outcome.failed("journal_write", "the entry was empty")
    try:
        path = journal.write_entry(entry)
    except Exception as e:
        return outcome.failed(
            "journal_write", f"the entry could not be written: {e}")
    return outcome.done("journal_write", saved_to=path.name) + \
        f"\nJournal entry saved to {path.name}"


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "journal_write",
    "description": (
        "Write an entry in today's journal (a dated record of the "
        "day — what happened, what was decided, what to remember "
        "about today). For a short note-to-self the user may ask "
        "about later, take_note is the better fit."
    ),
    "brief": "Write an entry in today's journal.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "entry": {
                "type": "STRING",
                "description": "The journal entry text",
            },
        },
        "required": ["entry"],
    },
    "handler": journal_write,
}
