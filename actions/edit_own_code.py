"""
actions/edit_own_code.py — rewrite one of her own source files in full.

The writer half of the Phase D self-edit port (blueprint P4); the
validator half is core/selfedit.py's hash watcher, and the split is the
design: this tool validates nothing and restarts nothing. It writes —
atomically, and only to the allowlist, which is exactly the watcher's
watched set (main.py, ui.py, .py files under core/ actions/ memory/
providers/ dashboard/ plugins/, and core/prompt.txt, her persona
file). After the turn, the watcher compiles, smoke-imports, announces
and restarts — or restores the file from git and says so.

For a small change prefer patch_own_code: a full rewrite makes the
model re-emit every line it wants to keep, and every re-emitted line
is a chance to lose one.
"""

from __future__ import annotations

from core import outcome, selfedit


def edit_own_code(parameters: dict = None, response=None, player=None,
                  session_memory=None, speak=None) -> str:
    params = parameters or {}
    path = (params.get("path") or "").strip()
    content = params.get("content")
    if not path:
        return outcome.failed("edit_own_code", "no path was given",
                              wanted="edit")
    if content is None:
        return outcome.failed("edit_own_code", "no content was given",
                              wanted="edit")
    ok, message = selfedit.edit_own_code(path, content)
    if not ok:
        return outcome.failed("edit_own_code", message, wanted="edit",
                              path=path)
    return outcome.done("edit_own_code", wanted="edit", path=path,
                        note=selfedit.RESTART_NOTE)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "edit_own_code",
    "description": (
        "Rewrite one of YOUR OWN source files in full, to improve or fix "
        "yourself. Editable files only: main.py, ui.py, any .py file under "
        "core/, actions/, memory/, providers/, dashboard/ or plugins/, and "
        "core/prompt.txt (your persona). Anything else is refused. The "
        "write is checked after this turn: if the new code compiles and "
        "imports, you announce the change and restart to load it; if it "
        "is broken, the last committed version is restored from git and "
        "you say so. Never tell the user to restart you, and never claim "
        "the change is live before the restart. For a small fix use "
        "patch_own_code instead of re-emitting a whole file."
    ),
    "brief": (
        "Rewrite one of your own source files in full ('path', "
        "'content'). Your code is validated and you restart to load it."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "path": {
                "type": "STRING",
                "description": "File to rewrite, relative to your root (e.g. actions/spotify.py, core/prompt.txt) or absolute inside your tree",
            },
            "content": {
                "type": "STRING",
                "description": "The complete new file content",
            },
        },
        "required": ["path", "content"],
    },
    "handler": edit_own_code,
}
