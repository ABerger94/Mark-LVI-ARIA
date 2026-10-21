"""
actions/read_notes.py — read back a day's voice notes (ARIA's,
ported).

'date' resolves the way ARIA resolved it: 'today', 'yesterday', a
weekday name (the most recent past one), or an explicit YYYY-MM-DD.
The notes themselves come out of that day's journal file — core/
journal.py parses the note-prefixed sections.
"""

from __future__ import annotations

from core import journal, outcome


def read_notes(parameters: dict = None, response=None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    date_str = params.get("date") or "today"
    try:
        day, notes = journal.read_notes(date_str)
    except Exception as e:
        return outcome.failed("read_notes", f"the notes could not be read: {e}")
    if not notes:
        return outcome.done("read_notes", day=day, count=0) + \
            f"\nNo notes from {day}."
    lines = [f"Notes from {day}:"]
    for when, text in notes:
        lines.append(f"• {day} {when} — {text}")
    listing = "\n".join(lines)
    return outcome.done("read_notes", day=day, count=len(notes)) + "\n" + \
        outcome.untrusted("notes", listing)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "read_notes",
    "description": (
        "Read the voice notes saved with take_note for one day. "
        "'date' may be 'today', 'yesterday', a weekday name like "
        "'tuesday' (the most recent one), or YYYY-MM-DD. Journal "
        "entries written with journal_write are not notes and are "
        "not returned here."
    ),
    "brief": "Read a day's saved notes ('today', 'yesterday', a weekday).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "date": {
                "type": "STRING",
                "description": "today | yesterday | weekday name | YYYY-MM-DD (default today)",
            },
        },
        "required": [],
    },
    "handler": read_notes,
}
