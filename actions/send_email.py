"""
actions/send_email.py — send Gmail (ARIA's send_email, behind the gate).

ARIA sent immediately; this build does not. Sending is one of the
Phase C gate classes (blueprint D3) — a sent email cannot be taken
back — so the tool call parks the send behind core/confirm.py's
on-screen token, pressed by a human, exactly like send_message. The
model cannot mint the token and cannot confirm its own send.

The send itself, when confirmed, is ARIA's: SMTP over STARTTLS with
the stored app password (core/gmail.py), an authentication failure
reported as final, a delivery refusal reported as unverified rather
than dressed up as success.
"""

from __future__ import annotations

from core import confirm, gmail, outcome


def send_email(parameters: dict = None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    to = (params.get("to") or "").strip()
    subject = (params.get("subject") or "").strip()
    body = params.get("body") or ""
    if not to:
        return outcome.failed("send_email", "no recipient was given")
    if not gmail.connected():
        return gmail.not_connected("send_email")

    preview = subject or "(no subject)"
    if confirm.pending_title():
        return outcome.ask(
            "send_email",
            "there is already a confirmation waiting on screen — the "
            "user has to answer that one first",
            to=to)

    return confirm.request(
        key="send_email",
        title=f"Send email to {to}",
        detail=(f'Subject: "{preview}" — sent from your Gmail account '
                f"as you. Once it is sent, it cannot be taken back."),
        run=lambda: gmail.send(to, subject, body),
    )


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "send_email",
    "description": (
        "Send an email from the user's Gmail account (connect it first "
        "with gmail_setup). Sending always asks for on-screen "
        "confirmation first — the email goes only after the user "
        "presses CONFIRM on the HUD. Provide the recipient, a subject "
        "and the full body text."
    ),
    "brief": "Send a Gmail ('to', 'subject', 'body') — asks on screen first.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "to": {
                "type": "STRING",
                "description": "Recipient email address",
            },
            "subject": {
                "type": "STRING",
                "description": "Subject line",
            },
            "body": {
                "type": "STRING",
                "description": "The email body, plain text",
            },
        },
        "required": ["to", "subject", "body"],
    },
    "handler": send_email,
}
