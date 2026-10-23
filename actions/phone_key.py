"""
actions/phone_key.py — press a system key on the phone (confirm-gated).

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_key

TOOL = {
    "name": "phone_key",
    "description": "Press a system key on the user's Android phone: 'back', 'home', 'recents' or 'enter'. CONFIRMATION-GATED: shown on the HUD, sent only when the user presses CONFIRM. Use 'back' to leave a screen, 'home' for the launcher, 'recents' for the app switcher, 'enter' to submit a focused field. Call phone_read_screen before and after — a key press can change more than expected (back can leave the app entirely).",
    "brief": "Press back / home / recents / enter on the Android phone — asks on screen first.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "key": {
                "type": "STRING",
                "description": "One of: back, home, recents, enter"
            }
        },
        "required": [
            "key"
        ]
    },
    "handler": phone_key,
}
