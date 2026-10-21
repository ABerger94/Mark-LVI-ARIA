"""
actions/file_find_advanced.py — filtered recursive find (ARIA's,
ported).

file_controller's find matches a name and an extension; ARIA's find
also filtered by size, by age, and by a substring inside file
contents. Those filters are this tool. All filters are optional and
combine (AND); results cap at 200 with the true total reported.
"""

from __future__ import annotations

from core import filetools, outcome
from actions.file_controller import _resolve_path


def _num(params: dict, key: str) -> float:
    try:
        return float(params.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def file_find_advanced(parameters: dict = None, response=None, player=None,
                       session_memory=None) -> str:
    params = parameters or {}
    raw = (params.get("directory") or "").strip()
    if not raw:
        return outcome.failed(
            "file_find_advanced", "no directory was given to search")
    directory = _resolve_path(raw)
    if not directory.is_dir():
        return outcome.failed(
            "file_find_advanced", f"'{raw}' is not a directory",
            directory=str(directory))
    pattern = (params.get("pattern") or "").strip()
    needle = (params.get("content_contains") or "").strip()
    min_size_mb = _num(params, "min_size_mb")
    max_age_days = _num(params, "max_age_days")
    try:
        total, hits, truncated = filetools.find_advanced(
            directory, pattern=pattern, min_size_mb=min_size_mb,
            max_age_days=max_age_days, content_contains=needle)
    except Exception as e:
        return outcome.failed(
            "file_find_advanced", f"the search failed: {e}",
            directory=str(directory))
    report = filetools.render_find(directory, total, hits, truncated)
    return outcome.done("file_find_advanced", directory=str(directory),
                        matches=total, shown=len(hits)) + "\n" + \
        outcome.untrusted("files", report)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "file_find_advanced",
    "description": (
        "Recursively find files under a folder with combined filters: "
        "'pattern' (fnmatch on the file name, e.g. '*.log'), "
        "'min_size_mb' (at least this many MB), 'max_age_days' "
        "(modified within this many days), 'content_contains' (a "
        "substring that must appear inside the file's text). Every "
        "filter is optional; omit them all to list everything. For a "
        "simple name search file_controller's find is enough — reach "
        "for this when size, age or content matter."
    ),
    "brief": "Find files by name pattern, size, age, or content.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "directory": {
                "type": "STRING",
                "description": "Folder to search, absolute path or shortcut (desktop, downloads, home, ...)",
            },
            "pattern": {
                "type": "STRING",
                "description": "fnmatch pattern on the file name, e.g. '*.pdf' (optional)",
            },
            "min_size_mb": {
                "type": "NUMBER",
                "description": "Only files at least this many MB (optional)",
            },
            "max_age_days": {
                "type": "NUMBER",
                "description": "Only files modified within this many days (optional)",
            },
            "content_contains": {
                "type": "STRING",
                "description": "Substring that must appear in the file's text content (optional)",
            },
        },
        "required": ["directory"],
    },
    "handler": file_find_advanced,
}
