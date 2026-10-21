"""
actions/file_duplicates.py — find duplicate files by content (ARIA's,
ported).

Report-only, exactly as ARIA shipped it: files are grouped by size,
quick-hashed, then full SHA-256 hashed only where both collide, and
the groups are reported. Nothing is deleted — deleting one copy of a
"duplicate" is a decision for the user, with file_controller's gated
delete as the tool for it once they have chosen.
"""

from __future__ import annotations

from core import filetools, outcome
from actions.file_controller import _resolve_path


def file_duplicates(parameters: dict = None, response=None, player=None,
                    session_memory=None) -> str:
    params = parameters or {}
    raw = (params.get("directory") or "").strip()
    if not raw:
        return outcome.failed(
            "file_duplicates", "no directory was given to scan")
    directory = _resolve_path(raw)
    if not directory.is_dir():
        return outcome.failed(
            "file_duplicates", f"'{raw}' is not a directory",
            directory=str(directory))
    try:
        scanned, groups = filetools.find_duplicates(directory)
    except Exception as e:
        return outcome.failed(
            "file_duplicates", f"the scan failed: {e}",
            directory=str(directory))
    report = filetools.render_duplicates(directory, scanned, groups)
    return outcome.done("file_duplicates", directory=str(directory),
                        scanned=scanned, groups=len(groups)) + "\n" + \
        outcome.untrusted("files", report)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "file_duplicates",
    "description": (
        "Find duplicate files under a folder by content (SHA-256), "
        "recursively. Report-only: it lists groups of identical files "
        "and never deletes anything. 'directory' accepts an absolute "
        "path or the file_controller shortcuts (desktop, downloads, "
        "documents, home, ...)."
    ),
    "brief": "Find duplicate files in a folder (report only, deletes nothing).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "directory": {
                "type": "STRING",
                "description": "Folder to scan, absolute path or shortcut (desktop, downloads, home, ...)",
            },
        },
        "required": ["directory"],
    },
    "handler": file_duplicates,
}
