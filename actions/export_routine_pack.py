"""
actions/export_routine_pack.py — bundle routines into a shareable pack.

A pack is a zip — manifest.json plus one JSON per routine, in ARIA's
format, so packs move between her build and this one. The zip mechanics
and validation live in core/routines.py; this wrapper only resolves
which routines ("all" when names_csv is empty, as ARIA's did) and
where the pack lands.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core import outcome, routines


def export_routine_pack(parameters: dict = None, response=None,
                        player=None, session_memory=None,
                        speak=None) -> str:
    params = parameters or {}
    names_csv = (params.get("names_csv") or "").strip()
    if names_csv:
        names = [n.strip() for n in names_csv.split(",") if n.strip()]
    else:
        names = routines.routine_names()
    dest_path = (params.get("dest_path") or "").strip()
    if not dest_path:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        dest_path = str(routines.ROUTINES_DIR.parent
                        / f"routines-{stamp}.zip")
    result = routines.export_pack(names, dest_path)
    if result.startswith("["):
        return outcome.failed("export_routine_pack", result.strip("[]"))
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "export_routine_pack",
    "description": (
        "Export routines to a shareable pack file (a zip with a "
        "manifest). names_csv says which routines, comma-separated; "
        "leave it empty to export all of them. dest_path chooses where "
        "the pack is written; by default it lands beside the routines "
        "folder with a dated name. The file can be carried to another "
        "machine and loaded there with import_routine_pack."
    ),
    "brief": (
        "Export routines to a pack file ('names_csv' — empty means "
        "all; optional 'dest_path')."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "names_csv": {
                "type": "STRING",
                "description": "Comma-separated routine names, e.g. 'morning_setup, shutdown_check'. Empty exports all routines.",
            },
            "dest_path": {
                "type": "STRING",
                "description": "Where to write the pack zip. Default: beside the routines folder.",
            },
        },
        "required": [],
    },
    "handler": export_routine_pack,
}
