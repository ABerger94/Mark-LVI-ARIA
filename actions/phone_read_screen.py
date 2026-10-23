"""
actions/phone_read_screen.py — read the phone's screen.

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_read_screen

TOOL = {
    "name": "phone_read_screen",
    "description": "Read what is on the user's Android phone screen RIGHT NOW: the foreground app and its visible elements, each with a ref number, class, text, and clickable/editable/scrollable flags. ALWAYS call this before phone_tap, phone_swipe or phone_type in any task — refs come from this snapshot and are only valid for the screen they were taken from — and call it again after any action that changes the screen before acting further. Read-only and NOT confirmation-gated.",
    "brief": "Read the phone's current screen: foreground app + elements with refs.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": []
    },
    "handler": phone_read_screen,
}
