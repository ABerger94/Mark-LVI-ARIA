"""
actions/diagnostics.py — "run diagnostics": her self-test, as a tool.

The pass itself lives in core/diagnostics.py (seven read-only
probes: COM audio, the dashboard bridge, Spotify, the memory and
config stores, the command registry, the engine). This action is
only the delivery: run the pass, put the checklist in the content
panel and the activity log, record an incident if anything failed,
and announce the one-line summary through the UI's say channel —
the same plugin_say seam the scheduler's timers and the engine
failover notice speak through, so the sentence arrives in her
voice, in English, with the facts fixed.

The tool declares scheduling SILENT: it has already announced
itself, so its result is recorded without prompting the model to
say it all again. The returned text still carries the summary and
the full checklist, so the record of what was found is in the
conversation even when no session was live to announce into.
"""

from __future__ import annotations

from core import diagnostics as diag
from core import outcome

_ANNOUNCE = ("Announce this to the user now, in one short sentence, "
             "in English: ")


def run_diagnostics(parameters: dict = None, player=None,
                    session_memory=None) -> str:
    results = diag.run_all()
    announce = None
    if player is not None:
        say = getattr(player, "request_say", None)
        if callable(say):
            announce = lambda text: say(_ANNOUNCE + text)
        diag.deliver(
            results,
            show_content=getattr(player, "show_content", None),
            write_log=getattr(player, "write_log", None),
            announce=announce)
    return (outcome.done("run_diagnostics") + "\n"
            + diag.spoken_summary(results) + "\n"
            + diag.render_report(results))


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "run_diagnostics",
    "description": (
        "Run a full self-diagnostics pass on this machine and report "
        "a pass/fail checklist: the COM audio round-trip (volume "
        "read-back), the phone dashboard bridge (HTTPS, certificate, "
        "pairing key, ports), Spotify's window telemetry, memory "
        "store and config file integrity (including their backups), "
        "the command registry, and engine readiness (Gemini Live's "
        "ladder or the providers pipeline's completeness). The "
        "checklist appears in the content panel; a one-line summary "
        "is announced aloud — either that everything checks out, or "
        "how many checks failed and which ones. Read-only: it "
        "changes nothing. Use when something seems off, after an "
        "update, or when the user asks whether everything is "
        "working."
    ),
    "brief": "Run the self-diagnostics pass ('run diagnostics').",
    "tier": "extra",
    "scheduling": "SILENT",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": run_diagnostics,
}
