"""
actions/read_email.py — read Gmail (ARIA's read_email, ported).

Two shapes, exactly ARIA's:

  * Without `uid`: the newest matching messages as one-line summaries
    (date | sender | "subject"), each carrying the uid it can be
    opened with. `query` is Gmail's own search syntax (from:boss
    newer_than:7d) via X-GM-RAW; `unread_only` restricts to unseen.
  * With `uid`: that one message in full — headers plus its plain-text
    body, truncated at 4000 characters as ARIA truncated it.

Reading never marks anything read (readonly mailbox, BODY.PEEK — see
core/gmail.py). The message text is wrapped as untrusted content: it
was written by whoever sent the mail, not by the user.
"""

from __future__ import annotations

from core import gmail, outcome


def read_email(parameters: dict = None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    uid = str(params.get("uid") or "").strip()
    if uid:
        msg, err = gmail.read_body(uid, action="read_email")
        if err:
            return err
        head = outcome.done("read_email", uid=uid,
                            subject=msg["subject"])
        headers = (f"From: {msg['from']}\nDate: {msg['date']}\n"
                   f"Subject: {msg['subject']}\n\n")
        body = msg["body"] or "[no text body]"
        return head + "\n" + headers + outcome.untrusted("gmail", body)

    entries, err = gmail.list_summaries(
        query=str(params.get("query") or ""),
        limit=params.get("limit", 10),
        unread_only=bool(params.get("unread_only", False)),
        action="read_email")
    if err:
        return err
    if not entries:
        return outcome.done("read_email", count=0) + \
            "\nNo matching emails."
    return outcome.done("read_email", count=len(entries)) + "\n" + \
        outcome.untrusted("gmail", gmail.summaries_text(entries))


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "read_email",
    "description": (
        "Read the user's Gmail over IMAP (connect it first with "
        "gmail_setup). Without uid: list the newest matching messages "
        "as one-line summaries with uids — query takes Gmail search "
        "syntax (e.g. 'from:boss newer_than:7d'), unread_only lists "
        "only unseen mail, limit caps the count (default 10, max 50). "
        "With uid: return that message's full plain-text body. Reading "
        "never marks mail as read."
    ),
    "brief": "Read Gmail ('query', 'limit', 'unread_only', or one 'uid').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "query": {
                "type": "STRING",
                "description": "Gmail search syntax, e.g. 'from:boss newer_than:7d'. Default '' = latest mail.",
            },
            "limit": {
                "type": "INTEGER",
                "description": "How many summaries to list (1-50, default 10)",
            },
            "unread_only": {
                "type": "BOOLEAN",
                "description": "Only unseen messages (ignored when query is given)",
            },
            "uid": {
                "type": "STRING",
                "description": "Open this one message in full instead of listing",
            },
        },
        "required": [],
    },
    "handler": read_email,
}
