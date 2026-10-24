"""
actions/phone_list_apps.py — what apps are on the phone?

The implementation lives in actions/phone_node.py with the rest of
the phone family; this file is the discovery record — one TOOL per
file is core/action_loader.py's rule.
"""

from __future__ import annotations

from actions.phone_node import phone_list_apps

TOOL = {
    "name": "phone_list_apps",
    "description": "List ALL apps installed on the user's Android phone — including system and non-launchable apps — by label and package name. Use this when the user asks what apps are on their phone, or before phone_open_app when you are unsure of an app's exact name. Note that not every listed app can be opened: phone_open_app still needs a launcher entry. Read-only; needs the phone node connected (phone_status checks).",
    "brief": "List all installed apps on the Android phone (label + package).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": []
    },
    "handler": phone_list_apps,
}
