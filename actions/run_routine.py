"""
actions/run_routine.py — replay a saved routine.

Thin wrapper over core/routines.run_routine: the facts come back as a
ReplayReport and are formatted here, because the outcome line is this
layer's job. A routine that ran every step earns outcome.done; one that
stopped before any step ran is outcome.failed — nothing happened; one
that stopped partway is reported as exactly that, in plain text: some
steps DID happen, and outcome.failed's "nothing succeeded" would be as
false as calling it done.
"""

from __future__ import annotations

import json

from core import outcome, routines


def run_routine(parameters: dict = None, response=None, player=None,
                session_memory=None, speak=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip()
    if not name:
        return outcome.failed("run_routine", "no routine name was given")
    try:
        values = json.loads(params.get("params_json") or "{}")
    except Exception as e:
        return outcome.failed("run_routine",
                              f"params_json is not valid JSON: {e}",
                              routine=name)
    if not isinstance(values, dict):
        return outcome.failed(
            "run_routine",
            "params_json must be a JSON object, "
            "e.g. '{\"who\": \"alek\"}'",
            routine=name)

    report = routines.run_routine(name, values)
    steps_text = "\n".join(report.lines)
    if report.halted is None:
        head = outcome.done("run_routine", routine=report.name,
                            steps=report.total,
                            trusted="yes" if report.trusted else "no")
        return head + ("\n" + steps_text if steps_text else "")
    if report.completed == 0 and not report.awaiting_confirmation:
        return outcome.failed("run_routine", report.halted,
                              routine=report.name or name,
                              steps=report.total)
    head = (f"Routine '{report.name}' stopped: {report.halted}. "
            f"{report.completed} of {report.total} step(s) completed.")
    if report.awaiting_confirmation:
        head += (" If the user confirms on screen, the waiting step "
                 "will run — but the rest of the routine did not, so "
                 "offer to run the routine again, or to trust_routine "
                 "it so it can run unattended.")
    return head + ("\n" + steps_text if steps_text else "")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "run_routine",
    "description": (
        "Replay a saved routine by name, in order. Arguments recorded "
        "with {{param}} placeholders are filled from params_json (a JSON "
        "object as a string, e.g. '{\"who\": \"alek\"}'); every "
        "placeholder must be supplied or the replay refuses to start. A "
        "failed step stops the replay and reports which step and why. A "
        "destructive step in an UNTRUSTED routine waits for on-screen "
        "confirmation and stops the replay there; a TRUSTED routine's "
        "steps all run without asking. Inspect a routine first with "
        "describe_routine."
    ),
    "brief": (
        "Replay a saved routine ('name', optional 'params_json' object "
        "string filling its {{parameters}})."
    ),
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {
                "type": "STRING",
                "description": "The routine to replay, e.g. 'morning_setup'",
            },
            "params_json": {
                "type": "STRING",
                "description": "JSON object string of {{parameter}} values, e.g. '{\"who\": \"alek\"}'. Default '{}'.",
            },
        },
        "required": ["name"],
    },
    "handler": run_routine,
}
