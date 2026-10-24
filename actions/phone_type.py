"""
actions/phone_type.py — type on the phone (ungated).

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_type

TOOL = {
    "name": "phone_type",
    "description": "Type text into the user's Android phone — into the focused field, or into the editable element named by 'ref' from your latest phone_read_screen snapshot. NOT confirmation-gated: the text is typed immediately when you call this (the user removed the phone confirmation gate on 2026-10-24). ALWAYS read the screen first and name the field's ref unless the user has just tapped it into focus in this task. Never type passwords, payment details, or message content the user has not dictated or approved in this conversation.",
    "brief": "Type text on the Android phone ('text', optional 'ref') — runs immediately.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "text": {
                "type": "STRING",
                "description": "The exact text to type"
            },
            "ref": {
                "type": "STRING",
                "description": "Ref of the editable element to type into, from the latest phone_read_screen snapshot (optional; default the focused field)"
            }
        },
        "required": [
            "text"
        ]
    },
    "handler": phone_type,
}
