"""
actions/import_routine_pack.py — load routines from a pack file.

The safety properties live in core/routines.import_pack and are the
point of the tool: a pack is validated (ARIA format, per-routine
shape), it NEVER overwrites a routine that already exists under the
same name, and every imported routine lands UNTRUSTED however it was
flagged where it came from — trust is this machine's user's decision.
"""

from __future__ import annotations

from core import outcome, routines


def import_routine_pack(parameters: dict = None, response=None,
                        player=None, session_memory=None,
                        speak=None) -> str:
    params = parameters or {}
    pack_path = (params.get("pack_path") or "").strip()
    if not pack_path:
        return outcome.failed("import_routine_pack",
                              "no pack file path was given")
    result = routines.import_pack(pack_path)
    if result.startswith("["):
        return outcome.failed("import_routine_pack", result.strip("[]"),
                              path=pack_path)
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "import_routine_pack",
    "description": (
        "Import routines from a pack file made by "
        "export_routine_pack. Imported routines are always UNTRUSTED, "
        "however they were flagged in the pack, and an import never "
        "overwrites a routine that already exists under the same "
        "name — existing ones are skipped and reported, along with "
        "anything in the pack that was rejected."
    ),
    "brief": "Import a routine pack ('pack_path') — lands untrusted, never overwrites.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "pack_path": {
                "type": "STRING",
                "description": "Path to the pack zip, e.g. 'C:\\Users\\Allen\\Downloads\\routines-20261021-120000.zip'",
            },
        },
        "required": ["pack_path"],
    },
    "handler": import_routine_pack,
}
