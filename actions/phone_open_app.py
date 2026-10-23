"""
actions/phone_open_app.py — open an app on the phone.

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_open_app

TOOL = {
    "name": "phone_open_app",
    "description": "Open an app on the user's Android phone — whenever the user says 'open Convoke on my phone', 'launch Settings on the Android', or anything like it, use this tool, not conversation. Give 'label' for the human name or 'package' for the exact package id (phone_list_apps lists both). Launching an app is benign and is NOT confirmation-gated. Returns the app the phone reports it opened.",
    "brief": "Open an app on the Android phone ('label' or 'package').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "label": {
                "type": "STRING",
                "description": "The app's display name, e.g. 'Convoke' (optional if package is given)"
            },
            "package": {
                "type": "STRING",
                "description": "The app's exact package id (optional if label is given)"
            }
        },
        "required": []
    },
    "handler": phone_open_app,
}
