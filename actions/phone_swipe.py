"""
actions/phone_swipe.py — swipe on the phone (confirm-gated).

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_swipe

TOOL = {
    "name": "phone_swipe",
    "description": "Swipe on the user's Android phone — scroll a list, dismiss a card, pull a shade. CONFIRMATION-GATED: the swipe is shown on the HUD and nothing is sent until the user presses CONFIRM. Give both endpoints in pixels ('x1','y1' to 'x2','y2'); 'duration_ms' sets the speed (default 300). Call phone_read_screen first so the swipe starts on the element you mean, and read again afterwards to confirm it did what you intended.",
    "brief": "Swipe on the Android phone ('x1','y1' to 'x2','y2', 'duration_ms') — asks on screen first.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "x1": {
                "type": "NUMBER",
                "description": "Start X in pixels"
            },
            "y1": {
                "type": "NUMBER",
                "description": "Start Y in pixels"
            },
            "x2": {
                "type": "NUMBER",
                "description": "End X in pixels"
            },
            "y2": {
                "type": "NUMBER",
                "description": "End Y in pixels"
            },
            "duration_ms": {
                "type": "NUMBER",
                "description": "Swipe duration in milliseconds (optional; default 300, clamped 50-2000)"
            }
        },
        "required": [
            "x1",
            "y1",
            "x2",
            "y2"
        ]
    },
    "handler": phone_swipe,
}
