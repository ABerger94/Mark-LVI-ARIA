"""
actions/notify_phone.py — put a notification on the user's phone.

WHY THIS EXISTS
    Everything else she says reaches the user one way: her voice, in
    the room, at that moment. This tool is for the thing that cannot
    wait for the room — the download that finished, the long job that
    needs a decision, the result he asked to be told about wherever
    he is. It goes through core/notify.py's one call, so it gets all
    three sinks (the live dashboard toast, Web Push to his phone,
    the missed queue) and the notify_enabled flag's silence.

NOT CONFIRM-GATED — RATE-LIMITED INSTEAD
    A notification is not an action on the machine; it is a tap on
    the shoulder, and gating it would make it useless for exactly
    the moments it exists for. What it gets instead is discipline:
    at most one model-initiated notification per 5 minutes, and at
    most 6 per rolling hour. The ledger is core/notify.py's persisted
    model_calls record, so a restart does not reset it. System
    sources (price crossings, scheduler firings) are not the model's
    calls and are never counted here.

    Delegated tasks never see the tool at all: it sits in main.py's
    _DELEGATED_EXCLUDE, so no background task can decide to tap the
    user's shoulder on its own initiative.
"""

from __future__ import annotations

from core import notify, outcome

MIN_GAP_S = 300.0        # one model notification per 5 minutes
HOURLY_CAP = 6           # and at most six per rolling hour
WINDOW_S = 3600.0


def _rate_refusal(now: float):
    """The refusal facts when the rate discipline bites, or None
    when this call may go out. Reads the persisted ledger; a call
    counts only once it has actually been delivered (recorded by
    the handler below), so a refused or deduped attempt does not
    spend the budget."""
    times = [t for t in notify.model_call_times() if now - t < WINDOW_S]
    if times and now - max(times) < MIN_GAP_S:
        wait = int(MIN_GAP_S - (now - max(times))) + 1
        return (f"the last phone notification went out less than "
                f"5 minutes ago — the next one may go in about "
                f"{wait} seconds")
    if len(times) >= HOURLY_CAP:
        return (f"{HOURLY_CAP} phone notifications already went out "
                f"in the last hour — that is the hourly cap")
    return None


def notify_phone(parameters: dict = None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    title = (params.get("title") or "").strip()
    body = (params.get("body") or "").strip()
    if not body:
        return outcome.failed("notify_phone",
                              "no message text was given")
    if not title:
        title = "ARIA"

    now = notify.clock()
    refusal = _rate_refusal(now)
    if refusal:
        return outcome.failed("notify_phone", refusal)

    result = notify.notify(title, body, source="aria", now=now)
    if not result.get("enabled", True):
        return outcome.failed(
            "notify_phone",
            "phone notifications are switched off (notify_enabled) "
            "— nothing was sent")
    if result.get("deduped"):
        return outcome.failed(
            "notify_phone",
            "the identical notification already went out within "
            "the last hour — it is not sent twice")
    if not result.get("ok"):
        return outcome.failed(
            "notify_phone",
            f"the notification could not be delivered "
            f"({result.get('reason', 'unknown')})")

    notify.record_model_call(now)
    return outcome.done(
        "notify_phone",
        title=title,
        pushed=result.get("pushed", 0),
        queued=bool(result.get("queued")),
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "notify_phone",
    "description": (
        "Send a notification to the user's phone (and any open "
        "dashboard) — for the thing that should reach them wherever "
        "they are: a long job finished, a result they asked to be "
        "told about, something that needs them soon. Keep it short: "
        "a title and one or two sentences. This is a tap on the "
        "shoulder, not a channel: at most one notification per "
        "5 minutes and six per hour are allowed, and a call past "
        "that is refused — so save it for what matters and batch "
        "news into one notification rather than sending several. "
        "Routine progress belongs in the conversation, not here. "
        "Delivery needs the user to have enabled notifications on "
        "the phone dashboard; if they have not, the notification is "
        "still recorded in the missed queue the dashboard shows."
    ),
    "brief": "Notify the user's phone ('title', 'body') — rate-limited, for what can't wait.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "title": {
                "type": "STRING",
                "description": "Short headline for the notification (optional; defaults to her name)",
            },
            "body": {
                "type": "STRING",
                "description": "The notification text — one or two sentences",
            },
        },
        "required": ["body"],
    },
    "handler": notify_phone,
}
