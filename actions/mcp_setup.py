"""
actions/mcp_setup.py — configure an MCP server (does not run it).

A server spec is one of:
  * a stdio command — command + args — which mcp_connect will
    later spawn as a child process of this app;
  * a streamable-HTTP url.

Storing the spec is confirm-gated when it creates or CHANGES a
spec: the spec is a standing instruction to launch a process or
talk to an endpoint, and the model must not be able to plant one
unseen — the same reason dev_agent's builds are gated. The
banner shows the exact command or URL. Re-saving the identical
spec is a no-op and needs no gate. Connecting is a separate act
(mcp_connect), itself gated.
"""

from __future__ import annotations

import shlex

from core import confirm, mcp_client, outcome
from memory import config_manager


def mcp_setup(parameters: dict = None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    name = (params.get("name") or "").strip().lower()
    if not mcp_client.valid_server_name(name):
        return outcome.failed(
            "mcp_setup",
            "the server name must be a lowercase letter followed "
            "by lowercase letters, digits or underscores "
            "(24 characters at most) — it becomes part of every "
            "tool name the server offers")
    url = (params.get("url") or "").strip()
    command = (params.get("command") or "").strip()
    if bool(url) == bool(command):
        return outcome.failed(
            "mcp_setup",
            "give exactly one of url (an HTTP MCP server) or "
            "command (a program to run as a stdio MCP server)")
    if url:
        if not url.startswith(("http://", "https://")):
            return outcome.failed(
                "mcp_setup", f"'{url}' does not look like a URL")
        spec = {"url": url}
        what = f"URL {url}"
    else:
        raw_args = params.get("args") or ""
        if isinstance(raw_args, list):
            args = [str(a) for a in raw_args]
        else:
            try:
                args = shlex.split(str(raw_args), posix=False)
            except ValueError as e:
                return outcome.failed(
                    "mcp_setup", f"the arguments do not parse: {e}")
            args = [a.strip('"') for a in args]
        spec = {"command": command, "args": args}
        what = "command " + " ".join([command] + args)

    existing = config_manager.get_mcp_servers().get(name)
    if existing == spec:
        return outcome.done("mcp_setup", server=name) + (
            f"\nMCP server '{name}' is already configured with "
            f"exactly that {what} — nothing changed.")

    if confirm.pending_title():
        return ("There is already a confirmation waiting on "
                "screen — answer that one first. Nothing was "
                "configured.")
    verb = "Change" if existing else "Configure"
    return confirm.request(
        key=f"mcp_setup:{name}",
        title=f"{verb} MCP server '{name}'",
        detail=(f"{name} → {what}. Saving this does not run "
                f"anything; mcp_connect launches it, under its "
                f"own confirmation. A stdio server runs as a "
                f"child process of this app with this app's "
                f"permissions."),
        run=lambda: _save(name, spec),
    )


def _save(name: str, spec: dict) -> str:
    config_manager.save_mcp_server(name, spec)
    if config_manager.get_mcp_servers().get(name) == spec:
        return (f"MCP server '{name}' configured. Connect it "
                f"with mcp_connect when ready.")
    return "The MCP server spec did not save on re-read."


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "mcp_setup",
    "description": (
        "Configure an MCP (Model Context Protocol) server for "
        "later connection: name (lowercase, becomes part of its "
        "tool names) plus EITHER command + args (a stdio server — "
        "a program this app will run as a child process) OR url "
        "(a streamable-HTTP MCP server). Saving a new or changed "
        "spec puts a confirmation on the user's screen showing "
        "the exact command or URL; nothing is saved until they "
        "confirm, and nothing runs at setup time — mcp_connect "
        "is the separate, also-confirmed launch."
    ),
    "brief": "Configure an MCP server ('name', 'command'+'args' or 'url').",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "name": {"type": "STRING",
                     "description": "Server name: lowercase letters, digits, underscores"},
            "command": {"type": "STRING",
                        "description": "Program to run (stdio server), e.g. npx or a python path"},
            "args": {"type": "STRING",
                     "description": "The command's arguments, space-separated"},
            "url": {"type": "STRING",
                    "description": "HTTP(S) URL of a streamable-HTTP MCP server"},
        },
        "required": ["name"],
    },
    "handler": mcp_setup,
}
