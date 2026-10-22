"""
actions/mcp_disconnect.py — close an MCP server's session.

Ungated: stopping a process she started is not an irreversible
act, it is the safe direction. Closing the session terminates
a stdio server's child process (the SDK transport owns it).
The answer is read back from the live state — a server that was
not connected is reported as such, not as a successful stop.
"""

from __future__ import annotations

from core import mcp_client, outcome


def mcp_disconnect(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    name = ((parameters or {}).get("name") or "").strip().lower()
    if mcp_client.disconnect(name):
        return outcome.done("mcp_disconnect", server=name) + (
            f"\nMCP server '{name}' disconnected — its session "
            f"is closed and its process ended.")
    return outcome.failed(
        "mcp_disconnect",
        f"MCP server '{name}' was not connected, so there was "
        f"nothing to disconnect")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "mcp_disconnect",
    "description": (
        "Disconnect a connected MCP server by name: closes its "
        "session and ends its child process (for a stdio "
        "server). The server stays configured — mcp_connect can "
        "relaunch it. Reports plainly when the server was not "
        "connected."
    ),
    "brief": "Disconnect an MCP server ('name').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING",
                     "description": "The server's name"},
        },
        "required": ["name"],
    },
    "handler": mcp_disconnect,
}
