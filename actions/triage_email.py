"""
actions/triage_email.py — classify the inbox (ARIA's triage, ported).

ARIA's shape kept: fetch the newest summaries from the same reader
read_email uses, hand them to a one-shot text call under her exact
classification brief, and return its verdict. The brief is conservative
by design — when in doubt, err toward IMPORTANT — because the cost of
a buried bill or a missed interview reply is higher than the cost of
a promo the user glances at.

The classifier seam (`_classify`) is injectable for the tests: the
default walks core/gemini.py's ladder, which is this build's
provider_text. An empty or failed classification is reported honestly
as a failure, never dressed up as a verdict.
"""

from __future__ import annotations

from core import gmail, outcome

_TRIAGE_SYSTEM = (
    "You are triaging the user's email inbox summaries. Classify each "
    "message into exactly one bucket. Be CONSERVATIVE — when in "
    "doubt, err toward important.\n\n"
    "IMPORTANT (act now): bills or payment reminders, job applications "
    "/ interview responses, security or account alerts (logins, "
    "password resets, fraud), real humans waiting on a reply, "
    "delivery problems, appointments being confirmed or changed.\n"
    "FYI (worth a glance, no rush): receipts and confirmations, "
    "shipping updates, newsletters from sources the user actually "
    "reads, personal updates from friends/family.\n"
    "NOISE (ignore): promotions, sales, marketing newsletters, "
    "spam-adjacent mailing lists, social-media notification digests, "
    "anything asking to buy something.\n\n"
    "Respond ONLY in this exact format — one message per line, then "
    "the noise count:\n"
    "IMPORTANT (act now): <one-line summary of the message>\n"
    "FYI: <one-line summary of the message>\n"
    "NOISE (ignored): <count of noise messages>"
)

# Injectable classifier seam — tests replace this; production leaves
# it None and the Gemini ladder answers.
_classify = None


def _classify_default(system: str, user_text: str) -> str:
    from core import gemini
    return gemini.text(f"{system}\n\n{user_text}", tier=gemini.FAST)


def triage_email(parameters: dict = None, player=None,
                 session_memory=None) -> str:
    params = parameters or {}
    entries, err = gmail.list_summaries(
        limit=params.get("limit", 20), action="triage_email")
    if err:
        return err
    if not entries:
        return outcome.done("triage_email", count=0) + \
            "\nInbox is empty — nothing to triage."

    user_text = ("Triage these email summaries (newest first):\n\n"
                 + gmail.summaries_text(entries))
    classify = _classify or _classify_default
    try:
        answer = classify(_TRIAGE_SYSTEM, user_text)
    except Exception as e:
        return outcome.failed("triage_email",
                              f"the classifier failed: {e}",
                              count=len(entries))
    if not answer or not str(answer).strip():
        return outcome.failed(
            "triage_email",
            "the classifier did not answer, so nothing was classified",
            count=len(entries))
    return outcome.done("triage_email", count=len(entries)) + "\n" + \
        str(answer).strip()


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "triage_email",
    "description": (
        "Triage the Gmail inbox: classify the newest messages into "
        "IMPORTANT / FYI / NOISE under a conservative brief (bills, "
        "job responses, security alerts and real humans are IMPORTANT; "
        "promotions and marketing are NOISE). Needs gmail_setup first. "
        "limit caps how many messages are classified (default 20, "
        "max 50)."
    ),
    "brief": "Triage the inbox into IMPORTANT / FYI / NOISE ('limit').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "limit": {
                "type": "INTEGER",
                "description": "How many newest messages to classify (1-50, default 20)",
            },
        },
        "required": [],
    },
    "handler": triage_email,
}
