"""
actions/self_heal.py — "self heal": diagnostics' bounded repair.

The pass that FINDS problems is core/diagnostics.py (read-only);
the repair is core/selfheal.py, and this action is only the
delivery: run a fresh diagnostics pass, heal what it found (the
two stores' loader recoveries — nothing else has a designed
remedy; rejected tool files and every other failure are reported,
never touched), put the heal report in the content panel and the
activity log, and announce the one-line summary.

Like run_diagnostics, the tool declares scheduling SILENT: it has
already announced itself. The returned text still carries the
summary and the full report, so the record of what was broken and
what was restored is in the conversation even when no session was
live to announce into.
"""

from __future__ import annotations

from core import diagnostics as diag
from core import outcome
from core import selfheal

_ANNOUNCE = ("Announce this to the user now, in one short sentence, "
             "in their own language: ")


def self_heal(parameters: dict = None, player=None,
              session_memory=None) -> str:
    results = diag.run_all()
    entries = selfheal.heal(results)
    if player is not None:
        say = getattr(player, "request_say", None)
        announce = (lambda text: say(_ANNOUNCE + text)) \
            if callable(say) else None
        selfheal.deliver(
            entries,
            show_content=getattr(player, "show_content", None),
            write_log=getattr(player, "write_log", None),
            announce=announce)
    return (outcome.done("self_heal") + "\n"
            + selfheal.spoken_summary(entries) + "\n"
            + selfheal.render_report(entries))


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "self_heal",
    "description": (
        "Repair what a diagnostics pass finds broken — the bounded "
        "follow-up to run_diagnostics. Runs the diagnostics pass, "
        "then heals ONLY the failures that have a designed remedy: "
        "a corrupted configuration file or memory store is restored "
        "through its loader's own backup recovery and verified by "
        "re-reading the file. Everything else — tool files that "
        "failed to load, audio, dashboard, engine problems — is "
        "reported by name and NOT touched: self-heal never deletes "
        "or moves code, and never invents a remedy. Use when "
        "diagnostics reported a store failure, or the user asks "
        "her to fix what diagnostics found."
    ),
    "brief": "Heal what diagnostics found ('self heal').",
    "tier": "extra",
    "scheduling": "SILENT",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": self_heal,
}
