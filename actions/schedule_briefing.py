"""
actions/schedule_briefing.py — the daily briefing at a set time
(Phase G4).

"Brief me at 7:30 every morning": a scheduler task of kind
"briefing" (core/scheduler.set_daily_briefing) whose record
carries the time of day and an enabled flag. When it comes due
while she runs — and only when a session is up and she is awake —
she composes the fixed-facts briefing (the startup greeting's own
composition) and delivers it as a spoken turn. She only ever has
one daily briefing: setting a new time moves it, and this tool is
also how it is switched off or back on. The plain list/cancel
tools see it like any other task.
"""

from __future__ import annotations

from core import outcome, scheduler


def schedule_briefing(parameters: dict = None, response=None,
                      player=None, session_memory=None) -> str:
    params = parameters or {}
    time_text = (params.get("time") or "").strip()
    enabled = params.get("enabled")
    existing = scheduler.get_briefing_task()

    if time_text:
        tid, normalised = scheduler.set_daily_briefing(
            time_text, enabled=True if enabled is None
            else bool(enabled))
        if tid is None:
            return outcome.failed(
                "schedule_briefing",
                f"I couldn't read '{time_text}' as a time of day — "
                "try '7:30 AM' or '19:30'.")
        pretty = scheduler._fmt_daily_time(
            *scheduler.parse_daily_time(normalised))
        state = "" if (enabled is None or enabled) else \
            " It is switched off — say 'turn my daily briefing " \
            "back on' when you want it."
        return outcome.done("schedule_briefing", task_id=tid,
                            time=normalised) + (
            f"\nDaily briefing set (#{tid}): I'll brief you at "
            f"{pretty} every day while I'm running.{state}")

    if enabled is None:
        if existing is None:
            return outcome.failed(
                "schedule_briefing",
                "no time was given and no daily briefing is set "
                "yet — try 'brief me at 7:30 every morning'.")
        parsed = scheduler.parse_daily_time(existing.get("time")
                                            or "")
        pretty = (scheduler._fmt_daily_time(*parsed) if parsed
                  else existing.get("time"))
        state = ("on" if existing.get("enabled", True) else "off")
        return outcome.done("schedule_briefing") + (
            f"\nYour daily briefing is set for {pretty} and is "
            f"currently {state} (task #{existing['id']}).")

    if not scheduler.set_briefing_enabled(bool(enabled)):
        return outcome.failed(
            "schedule_briefing",
            "no daily briefing is set yet — give me a time first, "
            "like 'brief me at 7:30 every morning'.")
    return outcome.done("schedule_briefing") + (
        "\nDaily briefing switched on."
        if enabled else
        "\nDaily briefing switched off — the time is kept, and "
        "I'll stay quiet until you switch it back on.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "schedule_briefing",
    "description": (
        "Set, move, switch off or check the DAILY BRIEFING: at a "
        "fixed time of day, while the assistant is running, she "
        "composes the briefing (calendar, inbox, weather, news — "
        "the startup greeting's composition) and speaks it. "
        "'Brief me at 7:30 every morning' sets it (time); there is "
        "only one, so a new time moves it. 'Turn off my daily "
        "briefing' is enabled=false (the time is kept); 'turn it "
        "back on' is enabled=true. With neither parameter, reports "
        "the current setting. If she is off or asleep at the set "
        "time it fires once at the next awake session, not on a "
        "loop. Delivered only while she runs — for a briefing that "
        "must arrive with the app closed, this is not the tool."
    ),
    "brief": "The daily briefing at a set time ('time', 'enabled').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "time": {
                "type": "STRING",
                "description": "Time of day, e.g. '7:30 AM' or '19:30' (omit to just switch or check)",
            },
            "enabled": {
                "type": "BOOLEAN",
                "description": "Switch the daily briefing on or off without changing its time",
            },
        },
        "required": [],
    },
    "handler": schedule_briefing,
}
