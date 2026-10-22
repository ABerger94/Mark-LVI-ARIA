"""
actions/read_own_code.py — read one of her own source files, in line
ranges, completely.

The reader half of the read-before-edit rule (Alek, 2026-10-22;
enforcement lives in core/selfedit.py). The general read_file cannot
serve here: it truncates at a character cap (4,000 by default) with
no offset, so a large file — ui.py is ~7,700 lines — could never be
read in full through it, which is part of how she came to rewrite
ui.py blind. This tool reads explicit offset/limit line ranges,
reports the file's total line count and her running coverage, and
records that coverage: edit_own_code and patch_own_code refuse any
file whose current version has not been read in full.
"""

from __future__ import annotations

from core import outcome, selfedit


def read_own_code(parameters: dict = None, response=None, player=None,
                  session_memory=None, speak=None) -> str:
    params = parameters or {}
    path = (params.get("path") or "").strip()
    if not path:
        return outcome.failed("read_own_code", "no path was given",
                              wanted="read")
    try:
        offset = int(params.get("offset") or 1)
    except (TypeError, ValueError):
        offset = 1
    try:
        limit = int(params.get("limit") or 400)
    except (TypeError, ValueError):
        limit = 400
    ok, text = selfedit.read_own_code(path, offset=offset, limit=limit)
    if not ok:
        return outcome.failed("read_own_code", text, wanted="read",
                              path=path)
    return outcome.done("read_own_code", wanted="read", path=path) \
        + "\n" + text


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "read_own_code",
    "description": (
        "Read one of YOUR OWN source files in line ranges — the only "
        "way to read a large file of yours in full (the general file "
        "reader truncates and cannot page). Same editable set as "
        "edit_own_code: main.py, ui.py, any .py under core/, actions/, "
        "memory/, providers/, dashboard/ or plugins/, and "
        "core/prompt.txt. Each call returns 'offset'..'offset+limit-1' "
        "numbered lines plus the file's total line count and how much "
        "of it you have now read. RULE: before edit_own_code or "
        "patch_own_code will accept a change to a file, you must have "
        "read that file's CURRENT version in full with this tool — "
        "the edit tools refuse otherwise and tell you how many lines "
        "remain. Any change to the file resets the requirement."
    ),
    "brief": (
        "Read one of your own source files in line ranges ('path', "
        "'offset', 'limit'). Required in full before you may edit it."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "File to read, relative to your root (e.g. ui.py, core/selfedit.py) or absolute inside your tree",
            },
            "offset": {
                "type": "INTEGER",
                "description": "First line to return, 1-based (default 1)",
            },
            "limit": {
                "type": "INTEGER",
                "description": "How many lines to return, up to 2000 (default 400)",
            },
        },
        "required": ["path"],
    },
    "handler": read_own_code,
}
