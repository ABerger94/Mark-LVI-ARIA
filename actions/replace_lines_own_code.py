"""
actions/replace_lines_own_code.py — replace a numbered span of lines
in one of her own source files.

The line-addressed half of the self-edit relief package (Alek's
ruling, 2026-10-25); see actions/patch_own_code.py for the exact-text
counterpart and core/selfedit.py for the watcher that validates every
change after the turn. Where patch_own_code anchors on exact text,
this tool anchors on explicit line numbers — for edits where
reproducing the old text byte-exactly is the fragile part. The span
is capped at 400 lines so one call stays one reviewable change.
"""

from __future__ import annotations

from core import outcome, selfedit


def replace_lines_own_code(parameters: dict = None, response=None,
                           player=None, session_memory=None,
                           speak=None) -> str:
    params = parameters or {}
    path = (params.get("path") or "").strip()
    new_text = params.get("new_text")
    if not path:
        return outcome.failed("replace_lines_own_code",
                              "no path was given",
                              wanted="replace lines")
    try:
        start_line = int(params.get("start_line"))
        end_line = int(params.get("end_line"))
    except (TypeError, ValueError):
        return outcome.failed(
            "replace_lines_own_code",
            "start_line and end_line must be whole line numbers",
            wanted="replace lines")
    if new_text is None:
        return outcome.failed("replace_lines_own_code",
                              "no new_text was given",
                              wanted="replace lines")
    ok, message = selfedit.replace_lines_own_code(
        path, start_line, end_line, new_text)
    if not ok:
        return outcome.failed("replace_lines_own_code", message,
                              wanted="replace lines", path=path)
    return outcome.done("replace_lines_own_code",
                        wanted="replace lines", path=path,
                        note=selfedit.RESTART_NOTE)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "replace_lines_own_code",
    "description": (
        "Replace a numbered span of lines in one of YOUR OWN source "
        "files — the line-addressed counterpart to patch_own_code, "
        "for edits where reproducing the old text exactly is the "
        "fragile part. Same editable set: main.py, ui.py, any .py "
        "under core/, actions/, memory/, providers/, dashboard/ or "
        "plugins/, and core/prompt.txt. Lines start_line..end_line "
        "(1-based, inclusive) are replaced by new_text; an empty "
        "new_text deletes the span. One call may replace at most "
        "400 lines — split larger changes into smaller replaces. "
        "Read rule: only the region being changed (the span plus "
        "60 lines around it) must have been read with read_own_code "
        "at the file's current version first — not the whole file. "
        "The change is validated after this turn (compile + import "
        "smoke test): good changes are announced and loaded by "
        "restart, broken ones are restored from git and reported."
    ),
    "brief": (
        "Replace lines 'start_line'..'end_line' in one of your own "
        "files with 'new_text' (span capped at 400 lines; read the "
        "region first)."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "File to edit, relative to your root (e.g. core/selfedit.py) or absolute inside your tree",
            },
            "start_line": {
                "type": "INTEGER",
                "description": "First line to replace, 1-based",
            },
            "end_line": {
                "type": "INTEGER",
                "description": "Last line to replace, 1-based, inclusive",
            },
            "new_text": {
                "type": "STRING",
                "description": "Replacement text for the span (may be multi-line; empty deletes the span)",
            },
        },
        "required": ["path", "start_line", "end_line", "new_text"],
    },
    "handler": replace_lines_own_code,
}
