"""
actions/phone_tap.py — tap on the phone (ungated).

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_tap

TOOL = {
    "name": "phone_tap",
    "description": "Tap something on the user's Android phone. NOT confirmation-gated: the tap is sent immediately when you call this (the user removed the phone confirmation gate on 2026-10-24), so chain the steps of a phone task without pausing to ask. ALWAYS call phone_read_screen first and prefer 'ref' — the ref number of an element from your LATEST screen snapshot — over coordinates: refs survive layout shifts, coordinates do not. Give 'x' and 'y' together only when the target has no ref (a canvas, a game). Never tap from a stale snapshot: read, tap, then read again to see what changed.",
    "brief": "Tap on the Android phone ('ref' preferred, or 'x'+'y') — runs immediately.",
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
