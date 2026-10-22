"""
actions/mcp_remove_server.py — forget an MCP server entirely.

Removal disconnects the server if it is connected, then deletes
its spec from the config. Confirm-gated, like the other
deletions in this app (routines, files): the spec may be the
only record of how that server was wired, and re-creating it
means dictating the command again. The banner says whether it
is currently connected, so the user knows a process ends too.
"""

from __future__ import annotations

from core import confirm, mcp_client, outcome
from memory import config_manager


def mcp_remove_server(parameters: dict = None, player=None,
                      session_memory=None) -> str:
    name = ((parameters or {}).get("name") or "").strip().lower()
    if config_manager.get_mcp_servers().get(name) is None:
        return outcome.failed(
            "mcp_remove_server",
            f"no MCP server named '{name}' is configured")
    rows = {r["name"]: r for r in mcp_client.status_rows()}
    connected = rows.get(name, {}).get("state") == "connected"
    if confirm.pending_title():
        return ("There is already a confirmation waiting on "
                "screen — answer that one first. Nothing was "
                "removed.")
    detail = (f"'{name}' will be disconnected and its "
              f"configuration deleted. Re-adding it means "
              f"dictating its command or URL again.")
    if not connected:
        detail = (f"'{name}' is not connected. Its configuration "
                  f"will be deleted; re-adding it means dictating "
                  f"its command or URL again.")
    return confirm.request(
        key=f"mcp_remove:{name}",
        title=f"Remove MCP server '{name}'",
        detail=detail,
        run=lambda: _remove(name),
    )


def _remove(name: str) -> str:
    mcp_client.disconnect(name)
    if config_manager.remove_mcp_server(name):
        return f"MCP server '{name}' removed."
    return f"MCP server '{name}' could not be removed on re-read."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "mcp_remove_server",
    "description": (
        "Remove an MCP server completely: disconnect it if "
        "connected, then delete its configuration. Puts a "
        "confirmation on the user's screen first — nothing is "
        "removed until they confirm."
    ),
    "brief": "Remove an MCP server ('name').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING",
                     "description": "The server's name"},
        },
        "required": ["name"],
    },
    "handler": mcp_remove_server,
}
