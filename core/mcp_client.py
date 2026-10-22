"""
core/mcp_client.py — MCP servers as an extra tool source.

WHAT THIS IS
    The client half of the Model Context Protocol. A server the
    user configures (config key "mcp_servers": a stdio command or
    a streamable-HTTP URL) can be connected; the server's tools
    are then registered into the same dispatch the model uses for
    actions and plugins, namespaced "mcp_<server>_<tool>", and
    their results come back as tool results like any other's —
    the server's own content, flattened to text, with an isError
    result reported as the failure it is.

    Tools join the model's declaration list where every tool list
    in this app is assembled (main.py's session-config build and
    the Commands screen), which is also the honest limit of their
    dynamism: actions and plugins are startup-discovered in this
    app too, so a server connected mid-session offers its tools
    from the next session build — mcp_list_servers shows the live
    truth at any moment, and mcp_connect says this out loud.

THE ADAPTER RULE (why nothing here imports mcp at the top)
    The `mcp` Python SDK is an OPTIONAL dependency. Every
    SDK-shaped object is reached through _sdk(), imported lazily
    at connect time; with the SDK absent the module imports
    cleanly, discovery of its actions succeeds, diagnostics'
    registry walk stays green, and every MCP tool answers "MCP
    support is not installed (pip install mcp)" instead of an
    import error taking the registry down with it. A broken
    optional dependency failing the whole walk is exactly the
    failure class diagnostics probe 6 exists to catch — this
    module refuses to be its cause.

PROCESSES
    A stdio server is a child process of this app, spawned by the
    SDK's transport on a dedicated event-loop thread this module
    owns (the app's own loop belongs to the conversation; MCP
    sessions live and die on theirs). disconnect() closes the
    session and the transport, which terminates the child. The
    app's hard exits (os._exit paths) can orphan a connected
    server's process — stated here rather than hidden: the clean
    paths are disconnect and a normal quit, and the processes are
    the user's own configured commands, visible in their process
    list under their own names.

NAMES
    Server names are validated at setup (^[a-z][a-z0-9_]{0,23}$)
    so the namespace arithmetic is exact: tool names are the
    server's own, sanitised to the declaration charset and capped
    so the whole name fits the 64-character tool limit, with a
    numeric suffix when sanitising makes two tools collide.
"""

from __future__ import annotations

import asyncio
import re
import threading
from concurrent.futures import TimeoutError as _FutureTimeout

CONNECT_TIMEOUT_S = 45.0
CALL_TIMEOUT_S = 60.0

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,23}$")
_UNSAFE = re.compile(r"[^a-zA-Z0-9_]")


def valid_server_name(name: str) -> bool:
    return bool(_NAME_RE.match(name or ""))


def sdk_available() -> bool:
    try:
        _sdk()
        return True
    except Exception:
        return False


def _sdk():
    """The SDK, imported on demand. Raises ImportError with the
    install instruction when absent — callers turn that into the
    degradation sentence, never a traceback."""
    try:
        import mcp
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        from mcp.client.streamable_http import streamablehttp_client
    except ImportError as e:
        raise ImportError(
            "MCP support is not installed (pip install mcp)") from e
    return mcp, ClientSession, StdioServerParameters, stdio_client, \
        streamablehttp_client


def not_installed_text() -> str:
    return ("MCP support is not installed on this machine — "
            "install it with: pip install mcp")


# ── Declaration shaping ────────────────────────────────────────────────────

def _gemini_params(node) -> dict:
    """An MCP inputSchema (plain JSON Schema) in the declaration
    shape this app's own tools use: upper-case type names, and
    only the keys the declaration surface carries (type,
    description, properties, items, required, enum). Anything
    else a server declares is dropped rather than passed through
    to fail at the vendor."""
    if not isinstance(node, dict):
        return {"type": "OBJECT", "properties": {}}
    out: dict = {}
    t = node.get("type")
    if isinstance(t, str):
        out["type"] = t.upper()
    if node.get("description"):
        out["description"] = str(node["description"])
    props = node.get("properties")
    if isinstance(props, dict):
        out["properties"] = {str(k): _gemini_params(v)
                             for k, v in props.items()}
    items = node.get("items")
    if isinstance(items, dict):
        out["items"] = _gemini_params(items)
    if isinstance(node.get("required"), list):
        out["required"] = [str(x) for x in node["required"]]
    if isinstance(node.get("enum"), list):
        out["enum"] = node["enum"]
    if out.get("type") == "OBJECT" and "properties" not in out:
        out["properties"] = {}
    return out or {"type": "OBJECT", "properties": {}}


def _decl_name(server: str, raw_tool: str, taken: set[str]) -> str:
    tool = _UNSAFE.sub("_", raw_tool or "tool")
    name = f"mcp_{server}_{tool}"[:64]
    base, n = name, 2
    while name in taken:
        name = f"{base[:60]}_{n}"
        n += 1
    taken.add(name)
    return name


# ── The manager ────────────────────────────────────────────────────────────

class _Conn:
    __slots__ = ("name", "spec", "state", "error", "tools",
                 "session", "stack")

    def __init__(self, name: str, spec: dict):
        self.name = name
        self.spec = spec
        self.state = "connecting"
        self.error = ""
        self.tools: list[dict] = []   # {decl, raw, description, parameters}
        self.session = None
        self.stack = None


_lock = threading.RLock()
_loop: asyncio.AbstractEventLoop | None = None
_loop_thread: threading.Thread | None = None
_conns: dict[str, _Conn] = {}


def _ensure_loop() -> asyncio.AbstractEventLoop:
    global _loop, _loop_thread
    with _lock:
        if _loop is not None and _loop_thread is not None \
                and _loop_thread.is_alive():
            return _loop
        _loop = asyncio.new_event_loop()

        def _run():
            asyncio.set_event_loop(_loop)
            _loop.run_forever()

        _loop_thread = threading.Thread(target=_run, daemon=True,
                                        name="mcp-loop")
        _loop_thread.start()
        return _loop


def _run_coro(coro, timeout: float):
    loop = _ensure_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=timeout)


async def _connect_async(conn: _Conn) -> None:
    from contextlib import AsyncExitStack
    _mcp, ClientSession, StdioServerParameters, stdio_client, \
        http_client = _sdk()
    stack = AsyncExitStack()
    spec = conn.spec
    if spec.get("url"):
        transport = http_client(str(spec["url"]))
    else:
        params = StdioServerParameters(
            command=str(spec.get("command") or ""),
            args=[str(a) for a in spec.get("args") or []])
        transport = stdio_client(params)
    read, write, *_rest = await stack.enter_async_context(transport)
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    listing = await session.list_tools()
    taken: set[str] = set()
    tools = []
    for tool in listing.tools or []:
        raw = str(getattr(tool, "name", "") or "")
        if not raw:
            continue
        tools.append({
            "decl": _decl_name(conn.name, raw, taken),
            "raw": raw,
            "description": str(getattr(tool, "description", "")
                               or f"MCP tool {raw} on {conn.name}"),
            "parameters": _gemini_params(
                getattr(tool, "inputSchema", None)),
        })
    conn.session = session
    conn.stack = stack
    conn.tools = tools
    conn.state = "connected"
    conn.error = ""


def connect(name: str) -> tuple[bool, str]:
    """Connect a configured server. (ok, detail): already-connected
    is a success with that said; an unknown name, a missing SDK and
    a failed handshake are failures with the reason kept."""
    name = (name or "").strip()
    from memory.config_manager import get_mcp_servers
    spec = get_mcp_servers().get(name)
    if spec is None:
        return False, f"no MCP server named '{name}' is configured"
    with _lock:
        conn = _conns.get(name)
        if conn is not None and conn.state == "connected":
            return True, f"'{name}' is already connected"
        conn = _Conn(name, spec)
        _conns[name] = conn
    try:
        _run_coro(_connect_async(conn), CONNECT_TIMEOUT_S)
    except _FutureTimeout:
        _drop(name)
        return False, (f"connecting to '{name}' timed out after "
                       f"{CONNECT_TIMEOUT_S:g}s")
    except Exception as e:
        with _lock:
            conn.state = "failed"
            conn.error = str(e)
        _incident("mcp_connect_failed", f"{name}: {e}")
        return False, f"connecting to '{name}' failed: {e}"
    return True, (f"'{name}' connected — {len(conn.tools)} tool(s) "
                  f"available")


def _drop(name: str) -> None:
    with _lock:
        _conns.pop(name, None)


def disconnect(name: str) -> bool:
    """Close a server's session (which terminates its child
    process for stdio servers). True when it is disconnected by
    the time this returns — including when it never was connected
    is False: there was nothing to do, and the answer says so via
    the caller's read-back of status."""
    name = (name or "").strip()
    with _lock:
        conn = _conns.get(name)
    if conn is None or conn.state != "connected" \
            or conn.stack is None:
        _drop(name)
        return False

    async def _close():
        try:
            await conn.stack.aclose()
        except Exception:
            pass

    try:
        _run_coro(_close(), 10.0)
    except Exception:
        pass   # a close that hangs must not wedge the caller; the
        # record is dropped either way (the loop-side close keeps
        # running in the background if it was merely slow)
    _drop(name)
    return True


def shutdown() -> None:
    """Disconnect everything (graceful app paths)."""
    for name in list(_conns):
        disconnect(name)


# ── The model's view ───────────────────────────────────────────────────────

def declarations() -> list[dict]:
    """Gemini-shaped declarations for every CONNECTED server's
    tools. Empty when nothing is connected or the SDK is absent —
    which is what keeps a default install's tool list exactly as
    it was."""
    out = []
    with _lock:
        conns = [c for c in _conns.values() if c.state == "connected"]
    for conn in conns:
        for t in conn.tools:
            out.append({"name": t["decl"],
                        "description": t["description"],
                        "parameters": t["parameters"]})
    return out


def tool_names() -> set[str]:
    return {d["name"] for d in declarations()}


def has_tool(decl_name: str) -> bool:
    return decl_name in tool_names()


def _find(decl_name: str):
    with _lock:
        for conn in _conns.values():
            if conn.state != "connected":
                continue
            for t in conn.tools:
                if t["decl"] == decl_name:
                    return conn, t["raw"]
    return None, None


def run_tool(decl_name: str, args: dict) -> str:
    """Execute one namespaced MCP tool and return its result as
    text — the server's own content blocks flattened (text as
    text; anything else named as the kind it is). An isError
    result and a dead session both come back as failure text,
    the same honesty as a native tool's crash."""
    conn, raw = _find(decl_name)
    if conn is None:
        return (f"MCP tool '{decl_name}' is not available — its "
                f"server is not connected.")
    try:
        result = _run_coro(conn.session.call_tool(raw, dict(args or {})),
                           CALL_TIMEOUT_S)
    except _FutureTimeout:
        return (f"MCP tool '{raw}' on '{conn.name}' timed out "
                f"after {CALL_TIMEOUT_S:g}s.")
    except Exception as e:
        with _lock:
            conn.state = "failed"
            conn.error = str(e)
        _incident("mcp_tool_failed", f"{conn.name}/{raw}: {e}")
        return (f"MCP tool '{raw}' on '{conn.name}' failed — the "
                f"session is broken: {e}")
    parts = []
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text is not None:
            parts.append(str(text))
        else:
            parts.append(f"[{type(block).__name__} content from "
                         f"the MCP server]")
    flat = "\n".join(parts).strip()
    if getattr(result, "isError", False):
        return (f"MCP tool '{raw}' on '{conn.name}' reported an "
                f"error: {flat or '(no detail)'}")
    return flat or "Done."


# ── Status for the tools ───────────────────────────────────────────────────

def status_rows() -> list[dict]:
    """Every configured server with its live state: configured
    (not connected), connecting, connected (with tool count),
    failed (with the error). Config is the roster; this module's
    connections are the state — neither pretends to be the other."""
    from memory.config_manager import get_mcp_servers
    configured = get_mcp_servers()
    rows = []
    for name in sorted(configured):
        spec = configured[name]
        with _lock:
            conn = _conns.get(name)
        row = {"name": name,
               "kind": "url" if spec.get("url") else "command",
               "target": (str(spec.get("url")) if spec.get("url")
                          else " ".join(
                              [str(spec.get("command") or "")]
                              + [str(a) for a in
                                 spec.get("args") or []]).strip()),
               "state": "configured",
               "tools": 0,
               "error": ""}
        if conn is not None:
            row["state"] = conn.state
            row["tools"] = len(conn.tools)
            row["error"] = conn.error
        rows.append(row)
    return rows


def connected_tools(name: str) -> list[dict]:
    with _lock:
        conn = _conns.get(name)
        if conn is None or conn.state != "connected":
            return []
        return [dict(t) for t in conn.tools]


def _incident(kind: str, message: str) -> None:
    try:
        from core import incidents
        incidents.record("mcp", kind, message)
    except Exception:
        pass


def reset_for_tests() -> None:
    """Drop all connection state (the loop thread is left running;
    it owns nothing between connections)."""
    shutdown()
    with _lock:
        _conns.clear()
