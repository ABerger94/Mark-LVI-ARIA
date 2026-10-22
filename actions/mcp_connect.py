"""
actions/mcp_connect.py — launch a configured MCP server.

Connecting is the act that runs something: a stdio server is a
child process spawned with this app's permissions. So the
launch is confirm-gated, in the idiom — the banner names the
server and its exact command or URL, and nothing spawns until
the user presses CONFIRM. The connect itself (handshake + tool
listing) runs inside the parked callable; its verified result
— how many tools the server actually offered — is what the
confirmation log reports, and mcp_list_servers reads the live
state back at any time.

With the MCP SDK absent the tool answers the degradation
sentence instead of asking for a launch that cannot happen.
"""

from __future__ import annotations

from core import confirm, mcp_client, outcome
from memory import config_manager


def mcp_connect(parameters: dict = None, player=None,
                session_memory=None) -> str:
    name = ((parameters or {}).get("name") or "").strip().lower()
    spec = config_manager.get_mcp_servers().get(name)
    if spec is None:
        return outcome.failed(
            "mcp_connect",
            f"no MCP server named '{name}' is configured — "
            f"mcp_setup first")
    if not mcp_client.sdk_available():
        return outcome.failed("mcp_connect",
                              mcp_client.not_installed_text())
    rows = {r["name"]: r for r in mcp_client.status_rows()}
    if rows.get(name, {}).get("state") == "connected":
        return outcome.done("mcp_connect", server=name) + (
            f"\nMCP server '{name}' is already connected.")
    if confirm.pending_title():
        return ("There is already a confirmation waiting on "
                "screen — answer that one first. Nothing was "
                "launched.")
    target = (str(spec.get("url")) if spec.get("url")
              else " ".join([str(spec.get("command") or "")]
                            + [str(a) for a in
                               spec.get("args") or []]).strip())
    return confirm.request(
        key=f"mcp_connect:{name}",
        title=f"Connect MCP server '{name}'",
        detail=(f"{name} → {target}. A stdio server runs as a "
                f"child process of this app, with this app's "
                f"permissions, until disconnected."),
        run=lambda: _connect(name),
    )


def _connect(name: str) -> str:
    ok, detail = mcp_client.connect(name)
    if not ok:
        return f"MCP connect failed — {detail}."
    return (f"{detail}. Its tools join the tool list at the "
            f"next session rebuild; mcp_list_servers shows them "
            f"now.")


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "mcp_connect",
    "description": (
        "Connect a configured MCP server by name. This LAUNCHES "
        "it — a stdio server is a child process of this app — so "
        "it puts a confirmation on the user's screen first and "
        "nothing spawns until they confirm. On success the "
        "server's tools are registered (namespaced "
        "mcp_<server>_<tool>) and join the tool list at the next "
        "session rebuild. Fails plainly when the server is not "
        "configured or the MCP SDK is not installed."
    ),
    "brief": "Connect an MCP server ('name').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING",
                     "description": "The configured server's name"},
        },
        "required": ["name"],
    },
    "handler": mcp_connect,
}
