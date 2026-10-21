"""
actions/take_note.py — a voice note to self (ARIA's, ported).

Saves the note into today's journal (memory/journal/) where
read_notes can find it again by date. ARIA also copied notes into
her searchable memory; this port deliberately does not put them in
long_term.json — see core/journal.py for why the journal is its own
store.
"""

from __future__ import annotations

from core import journal, outcome


def take_note(parameters: dict = None, response=None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    text = (params.get("text") or "").strip()
    if not text:
        return outcome.failed("take_note", "the note was empty")
    try:
        path = journal.take_note(text)
    except Exception as e:
        return outcome.failed("take_note", f"the note could not be saved: {e}")
    return outcome.done("take_note", saved_to=path.name) + "\nNoted."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "take_note",
    "description": (
        "Save a quick note to self ('note that...', 'remind me in "
        "writing that...'). The note goes into today's journal and "
        "can be read back with read_notes. For a timed spoken reminder "
        "use reminder or set_timer instead — a note does not speak."
    ),
    "brief": "Save a note to today's journal.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {
                "type": "STRING",
                "description": "The note text, in the user's words",
            },
        },
        "required": ["text"],
    },
    "handler": take_note,
}
