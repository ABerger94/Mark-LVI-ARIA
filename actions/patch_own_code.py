"""
actions/patch_own_code.py — replace one exact passage in one of her
own source files.

The surgical half of the Phase D self-edit port (blueprint P4); see
actions/edit_own_code.py for the design and core/selfedit.py for the
watcher that validates every change after the turn. The refusal rules
are ARIA's, verbatim in spirit: old_text must match EXACTLY ONE place.
Absent or ambiguous matches change nothing — a partial or ambiguous
self-patch is how a working file becomes a broken one.
"""

from __future__ import annotations

from core import outcome, selfedit


def patch_own_code(parameters: dict = None, response=None, player=None,
                   session_memory=None, speak=None) -> str:
    params = parameters or {}
    path = (params.get("path") or "").strip()
    old_text = params.get("old_text")
    new_text = params.get("new_text")
    if not path:
        return outcome.failed("patch_own_code", "no path was given",
                              wanted="patch")
    if not old_text:
        return outcome.failed("patch_own_code",
                              "old_text is empty — nothing to match",
                              wanted="patch")
    if new_text is None:
        return outcome.failed("patch_own_code", "no new_text was given",
                              wanted="patch")
    ok, message = selfedit.patch_own_code(path, old_text, new_text)
    if not ok:
        return outcome.failed("patch_own_code", message, wanted="patch",
                              path=path)
    return outcome.done("patch_own_code", wanted="patch", path=path,
                        note=selfedit.RESTART_NOTE)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "patch_own_code",
    "description": (
        "Replace one exact passage in one of YOUR OWN source files — the "
        "small-change counterpart to edit_own_code. Same editable set: "
        "main.py, ui.py, any .py under core/, actions/, memory/, "
        "providers/, dashboard/ or plugins/, and core/prompt.txt. "
        "old_text must match exactly one place in the file; if it "
        "matches none or several, nothing is changed and you are told "
        "why. The change is validated after this turn (compile + import "
        "smoke test): good changes are announced and loaded by restart, "
        "broken ones are restored from git and reported."
    ),
    "brief": (
        "Patch one exact passage in one of your own files ('path', "
        "'old_text', 'new_text' — old_text must match exactly once)."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "File to patch, relative to your root (e.g. core/prompt.txt) or absolute inside your tree",
            },
            "old_text": {
                "type": "STRING",
                "description": "The exact text to replace — must occur exactly once in the file",
            },
            "new_text": {
                "type": "STRING",
                "description": "The replacement text",
            },
        },
        "required": ["path", "old_text", "new_text"],
    },
    "handler": patch_own_code,
}
