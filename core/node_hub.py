"""
core/node_hub.py — the phone-node registry and command channel.

WHAT THIS IS
    ARIA's laptop side of the Android node: the phone runs a small
    client that connects OUT to her dashboard over a WebSocket
    (dashboard/server.py, /node/ws) and announces itself with a hello
    carrying the dashboard pairing key. Once registered here, her
    phone tools (actions/phone_node.py) can ask that node to do
    things — open an app, read its screen, tap, type — and get a
    structured result back. The protocol contract (hello / welcome /
    command / result / ping / pong) is shared verbatim with the
    Android client; this module is the half that lives here.

WHY A HUB, AND WHY THIS SHAPE
    The two callers live on different threading worlds:

      * The WebSocket endpoint runs on the dashboard's asyncio
        loop. It may not block, and it owns the socket.
      * Her tools are synchronous functions on executor threads
        (core/action_loader dispatch). They must block until the
        phone answers, with a timeout.

    concurrent.futures.Future is the bridge that fits both: the
    endpoint registers one future per command id, the tool thread
    waits on it, and whichever thread delivers the result — the
    loop thread, when the result frame arrives — completes it.
    Futures are explicitly thread-safe for exactly this.

    Delivery TO the socket is the endpoint's business: register()
    takes a `send` callable, so the hub never sees a WebSocket and
    never sees an event loop. The endpoint's send schedules
    send_json on its loop (run_coroutine_threadsafe); tests inject
    a plain function. That seam is what makes the whole channel
    testable without a live server.

ONE NODE PER NAME
    A new hello under an existing name REPLACES the old registration
    (the phone restarted its app, the network moved) and the old
    transport is closed if a close callable was given. A late
    disconnect from the replaced socket must not evict the
    replacement, so unregister() only removes the registration whose
    send callable is the one disconnecting.

FAILURE SHAPE
    send_command raises NodeHubError with a machine-readable `code`
    ("no_node", "timeout", "disconnected", "node_error") and a
    human message. Tools render the no_node case as guidance and the
    rest as failures; nothing here returns a half-result dressed as
    success — a command the node answered ok=false raises with the
    node's own error text.
"""

from __future__ import annotations

import concurrent.futures
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Callable, Optional

DEFAULT_TIMEOUT = 15.0


class NodeHubError(Exception):
    """A command could not be delivered or answered.

    `code` says which kind of failure, so callers can tell "there is
    no phone" (guidance) from "the phone said no" (a real failure)
    without parsing the message."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass
class _Node:
    name:         str
    kind:         str
    capabilities: list
    connected_at: float
    send:         Callable[[dict], None]
    close:        Optional[Callable[[], None]] = None


@dataclass
class _Pending:
    node:   str
    future: concurrent.futures.Future


class NodeHub:
    """Thread-safe registry of connected nodes + pending commands.

    Every public method takes the lock; futures are completed
    OUTSIDE it (a completion can wake a tool thread that immediately
    calls back in here, and holding the lock across that is a
    deadlock invitation)."""

    def __init__(self):
        self._lock    = threading.Lock()
        self._nodes:  dict[str, _Node]   = {}
        self._pending: dict[str, _Pending] = {}

    # ── registration (endpoint side) ──────────────────────────────────

    def register(self, name: str, kind: str, capabilities,
                 send: Callable[[dict], None],
                 close: Optional[Callable[[], None]] = None) -> None:
        """Register (or replace) a node. Called by the WS endpoint
        after the hello handshake validates."""
        old: Optional[_Node] = None
        with self._lock:
            old = self._nodes.get(name)
            caps = (list(capabilities)
                    if isinstance(capabilities, (list, tuple)) else [])
            self._nodes[name] = _Node(
                name=name, kind=kind or "",
                capabilities=caps,
                connected_at=time.time(), send=send, close=close)
        if old is not None and old.close is not None:
            try:
                old.close()
            except Exception:
                pass

    def unregister(self, name: str,
                   send: Optional[Callable[[dict], None]] = None) -> bool:
        """Drop a node's registration when its socket closes.

        With `send` given, only the registration that send belongs
        to is dropped — a replaced socket disconnecting late must
        not evict its replacement. Any command still waiting on the
        node fails with 'disconnected' instead of riding out its
        full timeout. Returns True when a registration was removed."""
        stranded: list[concurrent.futures.Future] = []
        with self._lock:
            node = self._nodes.get(name)
            if node is None:
                return False
            # Identity first, equality second: a bound method is a
            # fresh object on every attribute read, yet still the
            # same registration's transport.
            if (send is not None and node.send is not send
                    and node.send != send):
                return False
            del self._nodes[name]
            for cid in [c for c, p in self._pending.items()
                        if p.node == name]:
                stranded.append(self._pending.pop(cid).future)
        for fut in stranded:
            if not fut.done():
                fut.set_exception(NodeHubError(
                    "disconnected",
                    f"phone node '{name}' disconnected before answering"))
        return True

    # ── result routing (endpoint side) ────────────────────────────────

    def handle_result(self, msg: dict) -> bool:
        """Route one result frame to its waiting command.

        Returns True when the id was pending. An ok=false result
        completes the future with NodeHubError carrying the node's
        own error text, so the tool thread sees one failure shape
        for every way a command can fail."""
        cid = str((msg or {}).get("id") or "")
        with self._lock:
            pend = self._pending.pop(cid, None)
        if pend is None:
            return False
        if pend.future.done():
            return True   # timed out / stranded first; answer is moot
        if msg.get("ok"):
            data = msg.get("data")
            pend.future.set_result(data if isinstance(data, dict) else {})
        else:
            err = str(msg.get("error") or "the node reported an error")
            pend.future.set_exception(NodeHubError("node_error", err))
        return True

    # ── introspection (tools side) ────────────────────────────────────

    def nodes(self) -> list[dict]:
        """Snapshot of connected nodes, oldest first: name, kind,
        capabilities, connected_at (epoch seconds)."""
        with self._lock:
            return [{"name": n.name, "kind": n.kind,
                     "capabilities": list(n.capabilities),
                     "connected_at": n.connected_at}
                    for n in sorted(self._nodes.values(),
                                    key=lambda n: n.connected_at)]

    def _pick(self, node: Optional[str]) -> Optional[_Node]:
        with self._lock:
            if node:
                return self._nodes.get(node)
            if not self._nodes:
                return None
            # No name asked for: the most recently connected node —
            # with one phone in the house that is simply the phone,
            # and with several it is the one that most recently
            # proved it is alive.
            return max(self._nodes.values(), key=lambda n: n.connected_at)

    # ── commands (tools side) ─────────────────────────────────────────

    def send_command(self, node: Optional[str], action: str,
                     args: Optional[dict] = None,
                     timeout: float = DEFAULT_TIMEOUT) -> dict:
        """Send one command to a node and wait for its result data.

        Raises NodeHubError: no_node when nothing suitable is
        connected, timeout when the node does not answer in time,
        disconnected when the node goes away mid-command, node_error
        when the node answers ok=false (or the transport fails)."""
        target = self._pick(node)
        if target is None:
            if node:
                raise NodeHubError(
                    "no_node",
                    f"no phone node named '{node}' is connected")
            raise NodeHubError(
                "no_node", "no phone node is connected")

        cid = uuid.uuid4().hex
        fut: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            # A same-named node may have been replaced between the
            # pick and here; pending is keyed by node NAME, and the
            # result routing does not care which socket answers.
            self._pending[cid] = _Pending(node=target.name, future=fut)

        frame = {"type": "command", "id": cid, "action": action,
                 "args": dict(args or {})}
        try:
            target.send(frame)
        except Exception as e:
            with self._lock:
                self._pending.pop(cid, None)
            raise NodeHubError(
                "node_error",
                f"could not deliver the command to '{target.name}': {e}")

        try:
            return fut.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            with self._lock:
                self._pending.pop(cid, None)
            raise NodeHubError(
                "timeout",
                f"phone node '{target.name}' did not answer "
                f"'{action}' within {timeout:g}s")
        except NodeHubError:
            raise
        except Exception as e:  # a future cancelled under us, etc.
            raise NodeHubError("node_error", str(e))


# The process-wide hub the dashboard endpoint registers into and the
# phone tools send through. One hub, like core/notify's one call:
# there is one phone channel in this process however it is reached.
HUB = NodeHub()
