"""core/node_hub.py + the /node/ws dashboard endpoint.

The hub is transport-agnostic on purpose (register() takes a send
callable), so most of this file exercises the channel with fake
transports and never opens a socket. The endpoint tests at the
bottom run the real FastAPI app through TestClient: they pin the
hello handshake (the dashboard pairing key, reused — a bad key
closes 4401), the welcome, a full command round trip through a
tool thread exactly the way actions/phone_node.py drives it, and
the key promotion that lets a paired node reconnect.
"""
import threading
import time

import pytest

from core import node_hub
from core.node_hub import NodeHub, NodeHubError


# ── fake transports ─────────────────────────────────────────────────────────

class FakeNode:
    """A node's half of the wire, in memory.

    send() records every frame; an optional responder answers a
    command frame by routing a result back through the hub, the
    way the endpoint's pump would."""

    def __init__(self, hub: NodeHub, responder=None):
        self.hub = hub
        self.frames: list[dict] = []
        self.responder = responder
        self.closed = False

    def send(self, msg: dict) -> None:
        self.frames.append(msg)
        if self.responder is not None and msg.get("type") == "command":
            reply = self.responder(msg)
            if reply is not None:
                self.hub.handle_result(reply)

    def close(self) -> None:
        self.closed = True

    def commands(self) -> list[dict]:
        return [f for f in self.frames if f.get("type") == "command"]


def _ok(data=None):
    def respond(msg):
        return {"type": "result", "id": msg["id"], "ok": True,
                "data": data or {}}
    return respond


# ── hub: round trips and failure shapes ─────────────────────────────────────

def test_send_command_round_trip():
    hub = NodeHub()
    fake = FakeNode(hub, responder=_ok({"package": "com.convoke",
                                        "label": "Convoke"}))
    hub.register("phone", "android", ["open_app"], fake.send, fake.close)

    data = hub.send_command(None, "open_app", {"label": "Convoke"})

    assert data == {"package": "com.convoke", "label": "Convoke"}
    (frame,) = fake.commands()
    assert frame["type"] == "command"
    assert frame["action"] == "open_app"
    assert frame["args"] == {"label": "Convoke"}
    assert frame["id"]


def test_send_command_no_node_raises_no_node():
    hub = NodeHub()
    with pytest.raises(NodeHubError) as ei:
        hub.send_command(None, "open_app", {})
    assert ei.value.code == "no_node"
    with pytest.raises(NodeHubError) as ei:
        hub.send_command("ghost", "open_app", {})
    assert ei.value.code == "no_node"


def test_send_command_timeout_raises_and_cleans_up():
    hub = NodeHub()
    fake = FakeNode(hub)  # silent: never answers
    hub.register("phone", "android", [], fake.send)

    with pytest.raises(NodeHubError) as ei:
        hub.send_command(None, "read_screen", {}, timeout=0.3)
    assert ei.value.code == "timeout"
    # The pending entry is gone: the node's late answer routes nowhere.
    (frame,) = fake.commands()
    assert hub.handle_result({"type": "result", "id": frame["id"],
                              "ok": True, "data": {}}) is False


def test_send_command_node_error_carries_the_nodes_words():
    hub = NodeHub()

    def refuse(msg):
        return {"type": "result", "id": msg["id"], "ok": False,
                "error": "accessibility service is off"}

    fake = FakeNode(hub, responder=refuse)
    hub.register("phone", "android", [], fake.send)
    with pytest.raises(NodeHubError) as ei:
        hub.send_command(None, "tap", {"ref": "3"})
    assert ei.value.code == "node_error"
    assert "accessibility service is off" in str(ei.value)


def test_handle_result_unknown_id_is_false():
    hub = NodeHub()
    assert hub.handle_result({"type": "result", "id": "nope",
                              "ok": True, "data": {}}) is False


def test_disconnect_strands_a_waiting_command():
    hub = NodeHub()
    fake = FakeNode(hub)
    hub.register("phone", "android", [], fake.send, fake.close)

    box = {}

    def _call():
        try:
            hub.send_command(None, "read_screen", {}, timeout=30)
        except NodeHubError as e:
            box["err"] = e

    th = threading.Thread(target=_call)
    th.start()
    for _ in range(100):
        if fake.commands():
            break
        time.sleep(0.02)
    assert fake.commands(), "command never reached the fake node"

    assert hub.unregister("phone", send=fake.send) is True
    th.join(5)
    assert box["err"].code == "disconnected"


def test_reregister_replaces_and_closes_the_old_socket():
    hub = NodeHub()
    old = FakeNode(hub, responder=_ok({"from": "old"}))
    new = FakeNode(hub, responder=_ok({"from": "new"}))
    hub.register("phone", "android", [], old.send, old.close)
    hub.register("phone", "android", [], new.send, new.close)

    assert old.closed is True
    # The replaced socket's late disconnect must not evict the new one.
    assert hub.unregister("phone", send=old.send) is False
    assert hub.send_command(None, "open_app", {}) == {"from": "new"}
    assert old.commands() == []
    assert len(new.commands()) == 1


def test_nodes_snapshot_and_most_recent_pick():
    hub = NodeHub()
    a = FakeNode(hub)
    b = FakeNode(hub, responder=_ok({"picked": "b"}))
    hub.register("first", "android", ["tap"], a.send)
    hub.register("second", "android", ["tap", "type"], b.send)

    snap = hub.nodes()
    assert [n["name"] for n in snap] == ["first", "second"]
    assert snap[0]["kind"] == "android"
    assert snap[0]["capabilities"] == ["tap"]
    assert snap[1]["connected_at"] >= snap[0]["connected_at"]
    # No name asked for -> the most recently connected node answers.
    assert hub.send_command(None, "tap", {"ref": "1"}) == {"picked": "b"}
    assert a.commands() == []


# ── the pairing-key claim (dashboard) ───────────────────────────────────────

def _server():
    import dashboard.server as srv_mod
    if not srv_mod._DEPS_OK:
        pytest.skip("fastapi/uvicorn not installed in this environment")
    return srv_mod.DashboardServer()


def test_claim_node_key_pending_consumed_then_remembered():
    srv = _server()
    key = srv.new_key()
    assert srv._claim_node_key(key, "phone") is True
    assert key not in srv._pending_keys          # spent, like auto_login
    assert srv._claim_node_key(key, "phone") is True   # device session now


def test_claim_node_key_rejects_unknown_expired_and_empty():
    srv = _server()
    assert srv._claim_node_key("", "phone") is False
    assert srv._claim_node_key("ZZZZ9999", "phone") is False
    srv._pending_keys["OLDKEY1"] = time.time() - 1
    assert srv._claim_node_key("OLDKEY1", "phone") is False
    # A key already spent pairing the browser is that device's
    # session key and still validates for its node.
    srv._token_keys["tok"] = "BROWSER1"
    assert srv._claim_node_key("BROWSER1", "phone") is True


# ── the /node/ws endpoint, end to end ───────────────────────────────────────

def _client(srv):
    from fastapi.testclient import TestClient
    return TestClient(srv.app)


def _hello(key, name="testphone"):
    return {"type": "hello", "node": name, "kind": "android",
            "key": key,
            "capabilities": ["open_app", "read_screen"],
            "app_version": "1.0"}


def test_node_ws_bad_key_closes_4401(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    srv = _server()
    monkeypatch.setattr(node_hub, "HUB", NodeHub())
    client = _client(srv)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/node/ws") as ws:
            ws.send_json(_hello("WRONGKEY"))
            ws.receive_json()
    assert ei.value.code == 4401


def test_node_ws_first_frame_must_be_hello(monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    srv = _server()
    monkeypatch.setattr(node_hub, "HUB", NodeHub())
    client = _client(srv)
    with pytest.raises(WebSocketDisconnect) as ei:
        with client.websocket_connect("/node/ws") as ws:
            ws.send_json({"type": "command", "id": "1",
                          "action": "open_app", "args": {}})
            ws.receive_json()
    assert ei.value.code == 4401


def test_node_ws_welcome_command_round_trip_and_pong(monkeypatch):
    srv = _server()
    hub = NodeHub()
    monkeypatch.setattr(node_hub, "HUB", hub)
    client = _client(srv)
    key = srv.new_key()

    with client.websocket_connect("/node/ws") as ws:
        ws.send_json(_hello(key))
        assert ws.receive_json() == {"type": "welcome", "ok": True}
        assert [n["name"] for n in hub.nodes()] == ["testphone"]

        box = {}

        def _call():
            try:
                box["data"] = hub.send_command(
                    None, "open_app", {"label": "Convoke"}, timeout=5)
            except Exception as e:  # noqa: BLE001 - asserted below
                box["err"] = e

        th = threading.Thread(target=_call)
        th.start()
        frame = ws.receive_json()
        assert frame["type"] == "command"
        assert frame["action"] == "open_app"
        assert frame["args"] == {"label": "Convoke"}
        ws.send_json({"type": "result", "id": frame["id"], "ok": True,
                      "data": {"package": "com.convoke",
                               "label": "Convoke"}})
        th.join(5)
        assert box.get("data") == {"package": "com.convoke",
                                   "label": "Convoke"}

        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}

    # Socket closed -> the registration goes away (server-side
    # cleanup is asynchronous; give it a moment to land).
    for _ in range(100):
        if not hub.nodes():
            break
        time.sleep(0.02)
    assert hub.nodes() == []
