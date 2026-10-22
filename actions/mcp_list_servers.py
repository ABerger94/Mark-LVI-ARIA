"""
actions/mcp_list_servers.py — the MCP roster and its live state.

Config is the roster (what COULD connect); core/mcp_client's
connections are the state (what IS connected, and what its
tools are). This tool reads both and keeps them visibly
separate: a configured-but-idle server is not "available", and
a connected server lists its actual tools under their
namespaced names — the same names the dispatch routes.
"""

from __future__ import annotations

from core import mcp_client, outcome


def mcp_list_servers(parameters: dict = None, player=None,
                     session_memory=None) -> str:
    rows = mcp_client.status_rows()
    if not rows:
        return outcome.done("mcp_list_servers", servers=0) + (
            "\nNo MCP servers are configured. mcp_setup adds one.")
    lines = []
    for r in rows:
        head = f"{r['name']} [{r['state']}] {r['kind']}: {r['target']}"
        if r["state"] == "connected":
            tools = mcp_client.connected_tools(r["name"])
            names = ", ".join(t["decl"] for t in tools)
            head += f" — {r['tools']} tool(s): {names}"
        elif r["state"] == "failed" and r["error"]:
            head += f" — last error: {r['error']}"
        lines.append(head)
    note = "" if mcp_client.sdk_available() else (
        "\nNote: " + mcp_client.not_installed_text())
    return outcome.done("mcp_list_servers", servers=len(rows)) + \
        "\n" + "\n".join(lines) + note


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "mcp_list_servers",
    "description": (
        "List the configured MCP servers with their live state "
        "(configured, connected, failed) and, for connected "
        "ones, the tools they offer under their namespaced "
        "names (mcp_<server>_<tool>). Read-only."
    ),
    "brief": "List MCP servers and their tools.",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": mcp_list_servers,
}
