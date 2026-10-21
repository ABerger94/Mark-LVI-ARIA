"""
actions/routine_record_start.py — begin capturing a routine.

The recording half of the routines system (core/routines.py): from this
call until routine_record_stop, every tool call the assistant makes is
captured as a step — the capture hook sits in main.py's _execute_tool,
the one funnel both session engines feed. This tool itself is never
captured (nor are any of the routine tools, or save_memory).
"""

from __future__ import annotations

from core import outcome, routines


def routine_record_start(parameters: dict = None, response=None,
                         player=None, session_memory=None,
                         speak=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed("routine_record_start",
                              "no routine name was given")
    result = routines.start_recording(name)
    if result.startswith("["):
        return outcome.failed("routine_record_start", result.strip("[]"),
                              routine=routines.sanitize_name(name))
    return result


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "routine_record_start",
    "description": (
        "Start recording a routine: every tool call made from now until "
        "routine_record_stop is captured as a step of a named, replayable "
        "routine. Use when the user wants to teach a repeated task once "
        "('watch me do this and save it', 'record a routine'). The name "
        "is cleaned to lowercase letters, numbers, _ and -. Refused if a "
        "recording is already open or a routine with that name exists."
    ),
    "brief": (
        "Start capturing tool calls into a new routine ('name'). Ends "
        "at routine_record_stop."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "What to call the routine, e.g. 'morning_setup'",
            },
        },
        "required": ["name"],
    },
    "handler": routine_record_start,
}
