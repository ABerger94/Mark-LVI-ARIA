"""
actions/gmail_setup.py — connect Gmail (ARIA's gmail_setup, ported).

Saves the user's Gmail address and app password into the config store
(config/api_keys.json, keys gmail_user / gmail_app_password) — the same
file and the same read-modify-write discipline as every other setting
here. After this, read_email / triage_email / send_email work; before
this, each of them answers with a clean "not connected yet" outcome
pointing back here.

An app password is not the account password: it is generated at
myaccount.google.com/apppasswords (2-Step Verification required) and
can be revoked there at any time without touching the real password.
"""

from __future__ import annotations

from core import outcome
from memory import config_manager


def gmail_setup(parameters: dict = None, player=None,
                session_memory=None) -> str:
    params = parameters or {}
    user = (params.get("gmail_user") or "").strip()
    pw = (params.get("app_password") or "").replace(" ", "").strip()
    if not user or not pw:
        return outcome.failed(
            "gmail_setup",
            "both a Gmail address and an app password are needed — "
            "the app password is generated at "
            "myaccount.google.com/apppasswords, it is not the "
            "account password")
    config_manager.save_gmail_credentials(user, pw)
    return outcome.done("gmail_setup", account=user) + (
        "\nGmail is connected for that address. Mail can now be read "
        "with read_email, triaged with triage_email, and sent with "
        "send_email (which asks on screen before sending).")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "gmail_setup",
    "description": (
        "Connect Gmail: save the user's Gmail address and app password "
        "so mail can be read and sent. The app password is NOT the "
        "account password — it is generated at "
        "myaccount.google.com/apppasswords and needs 2-Step "
        "Verification turned on. Call this when the user gives you "
        "their Gmail address and app password, or when read_email / "
        "send_email say Gmail is not connected and the user provides "
        "them."
    ),
    "brief": "Connect Gmail ('gmail_user', 'app_password').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "gmail_user": {
                "type": "STRING",
                "description": "The Gmail address, e.g. 'name@gmail.com'",
            },
            "app_password": {
                "type": "STRING",
                "description": "The 16-character app password (spaces are ignored)",
            },
        },
        "required": ["gmail_user", "app_password"],
    },
    "handler": gmail_setup,
}
