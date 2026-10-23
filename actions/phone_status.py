"""
actions/phone_status.py — is the Android phone node connected?

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_status

TOOL = {
    "name": "phone_status",
    "description": "Check whether the user's Android phone node is connected to you RIGHT NOW, and what it can do. Use this FIRST whenever the user mentions 'my phone', 'the Android', or asks you to do anything on the phone and you have not confirmed a node is connected in this conversation. It reports the node's name, kind, capabilities and how long it has been connected. If no node is connected it says so plainly, and no other phone_* tool will work until the ARIA Node app on the phone is running and paired.",
    "brief": "Check whether the Android phone node is connected and what it can do.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": []
    },
    "handler": phone_status,
}
