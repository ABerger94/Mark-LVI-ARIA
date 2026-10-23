"""
actions/phone_tap.py — tap on the phone (confirm-gated).

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_tap

TOOL = {
    "name": "phone_tap",
    "description": "Tap something on the user's Android phone. CONFIRMATION-GATED: the exact tap is shown on the HUD and nothing is sent to the phone until the user presses CONFIRM. ALWAYS call phone_read_screen first and prefer 'ref' — the ref number of an element from your LATEST screen snapshot — over coordinates: refs survive layout shifts, coordinates do not. Give 'x' and 'y' together only when the target has no ref (a canvas, a game). Never tap from a stale snapshot: read, tap, then read again to see what changed.",
    "brief": "Tap on the Android phone ('ref' preferred, or 'x'+'y') — asks on screen first.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "ref": {
                "type": "STRING",
                "description": "Ref of the element to tap, from the latest phone_read_screen snapshot (preferred)"
            },
            "x": {
                "type": "NUMBER",
                "description": "X coordinate in pixels (only with 'y', and only when no ref fits)"
            },
            "y": {
                "type": "NUMBER",
                "description": "Y coordinate in pixels (only with 'x')"
            }
        },
        "required": []
    },
    "handler": phone_tap,
}
