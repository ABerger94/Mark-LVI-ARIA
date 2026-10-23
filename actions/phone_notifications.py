"""
actions/phone_notifications.py — read the phone's notifications.

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_notifications

TOOL = {
    "name": "phone_notifications",
    "description": "Read the user's Android phone notifications — app, title, text and time, newest first. Use this when the user asks what notifications they have, what just buzzed, or whether a specific app has said anything. Read-only and NOT confirmation-gated. 'limit' caps how many come back (default 10, max 50).",
    "brief": "Read the Android phone's notifications ('limit', default 10).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "limit": {
                "type": "NUMBER",
                "description": "How many notifications to return, newest first (optional; default 10, clamped 1-50)"
            }
        },
        "required": []
    },
    "handler": phone_notifications,
}
