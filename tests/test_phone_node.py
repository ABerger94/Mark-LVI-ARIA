"""actions/phone_node.py — the phone_* tool handlers.

The hub is stubbed at the module attribute the handlers read
(core.node_hub.HUB), so every test drives the real handler code —
argument validation, result formatting, the confirm gate — with no
phone and no socket. The gate tests pin the run_shell discipline:
validation happens BEFORE the banner, gated tools park through
confirm.request under the shared key "phone_control" and send
nothing until the parked callable runs, trusted scopes refuse,
and the read-side tools never touch the gate at all.
"""
import time

import pytest

import actions.phone_node as tool
from core import confirm, node_hub
from core.node_hub import NodeHubError


class StubHub:
    """A hub that answers from a script and remembers the calls."""

    def __init__(self, data=None, nodes_list=None, error=None):
        self.calls: list[tuple] = []
        self._data = data if data is not None else {}
        self._nodes = nodes_list or []
        self._error = error

    def nodes(self):
        return list(self._nodes)

    def send_command(self, node, action, args, timeout=15.0):
        self.calls.append((action, dict(args or {})))
        if self._error is not None:
            raise self._error
        return self._data


@pytest.fixture
def no_node(monkeypatch):
    stub = StubHub(error=NodeHubError("no_node", "no phone node is connected"))
    monkeypatch.setattr(node_hub, "HUB", stub)
    return stub


@pytest.fixture
def confirm_spy(monkeypatch):
    """confirm.request as a recording double; the parked callable is
    captured, never run, until a test runs it."""
    calls = []

    def request(key, title, detail, run):
        calls.append({"key": key, "title": title, "detail": detail,
                      "run": run})
        return "[CONFIRMATION_PENDING] test"

    monkeypatch.setattr(confirm, "request", request)
    monkeypatch.setattr(confirm, "pending_title", lambda: "")
    monkeypatch.setattr(confirm, "in_trusted_scope", lambda: False)
    return calls


# ── no node: one plain sentence from every tool ─────────────────────────────

def test_no_node_every_tool_says_so(no_node):
    gated = [tool.phone_tap, tool.phone_swipe, tool.phone_type,
             tool.phone_key]
    plain = [tool.phone_status, tool.phone_list_apps,
             tool.phone_open_app, tool.phone_read_screen,
             tool.phone_notifications]
    for fn in plain:
        out = fn({"label": "Convoke", "text": "hi", "key": "back"})
        assert out.startswith("No phone node is connected"), fn.__name__
    # phone_status reads nodes(), not send_command — empty is no node.
    assert tool.phone_status({}).startswith("No phone node is connected")
    # Gated tools park at the gate, whose own machinery answers when
    # no interface is bound — but with a working gate they must reach
    # the hub check only after confirmation. Here the gate is the
    # real one, unbound in tests, so they refuse at the gate.
    calls_before = len(no_node.calls)  # the read side already called
    for fn in gated:
        args = {"ref": "1", "x1": 0, "y1": 0, "x2": 1, "y2": 1,
                "text": "hi", "key": "back"}
        out = fn(args)
        assert "status=failed" not in out  # refused by the gate, not run
        assert len(no_node.calls) == calls_before  # nothing new sent


# ── read side: formatting ───────────────────────────────────────────────────

def test_status_formats_connected_node(monkeypatch):
    stub = StubHub(nodes_list=[{
        "name": "alek-android", "kind": "android",
        "capabilities": ["open_app", "tap"],
        "connected_at": time.time() - 125}])
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = tool.phone_status({})
    assert "alek-android" in out
    assert "android" in out
    assert "2m ago" in out
    assert "open_app, tap" in out


def test_list_apps_formats_label_and_package(monkeypatch):
    stub = StubHub(data={"apps": [
        {"package": "com.convoke", "label": "Convoke"},
        {"package": "com.android.settings", "label": ""}]})
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = tool.phone_list_apps({})
    assert "Apps on the phone (2):" in out
    assert "- Convoke (com.convoke)" in out
    assert "- com.android.settings" in out
    assert stub.calls == [("list_apps", {})]


def test_list_apps_not_truncated_at_the_generic_cap(monkeypatch):
    # Regression (2026-10-24): the phone now reports ALL installed
    # apps (200-300 packages). The generic 6,000-char list cap cut
    # that at ~120 apps and the head was presented as the whole list.
    apps = [{"package": f"com.example.app{i:03d}",
             "label": f"Example App Number {i:03d}"} for i in range(250)]
    stub = StubHub(data={"apps": apps})
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = tool.phone_list_apps({})
    assert "Apps on the phone (250):" in out
    assert "- Example App Number 249 (com.example.app249)" in out
    assert "[list truncated]" not in out


def test_open_app_forwards_args_and_reports(monkeypatch):
    stub = StubHub(data={"package": "com.convoke", "label": "Convoke"})
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = tool.phone_open_app({"label": "Convoke"})
    assert stub.calls == [("open_app", {"label": "Convoke"})]
    assert out == "Opened Convoke (com.convoke) on the phone."

    out = tool.phone_open_app({})
    assert "status=failed" in out
    assert len(stub.calls) == 1  # validation stopped the second call


def test_read_screen_formats_refs_flags_and_truncation(monkeypatch):
    stub = StubHub(data={
        "package": "com.convoke",
        "truncated": True,
        "nodes": [
            {"ref": "1", "class": "android.widget.Button", "text": "OK",
             "desc": "", "id": "", "bounds": [0, 1, 2, 3],
             "clickable": True, "editable": False, "scrollable": False},
            {"ref": "2", "class": "android.widget.EditText", "text": "",
             "desc": "Search decks", "id": "", "bounds": [4, 5, 6, 7],
             "clickable": True, "editable": True, "scrollable": False},
        ]})
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = tool.phone_read_screen({})
    assert "Screen: com.convoke — 2 elements" in out
    assert "truncated" in out
    assert '1: Button "OK" (clickable) @ [0,1,2,3]' in out
    assert '2: EditText "Search decks" (clickable, editable)' in out
    assert "phone_tap as 'ref'" in out


def test_notifications_formats_items_and_clamps_limit(monkeypatch):
    ts_ms = int((time.time() - 60) * 1000)
    stub = StubHub(data={"items": [
        {"package": "com.whatsapp", "app": "WhatsApp", "title": "Sam",
         "text": "gg last game", "ts": ts_ms}]})
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = tool.phone_notifications({"limit": 500})
    assert stub.calls == [("notifications", {"limit": 50})]
    assert "[WhatsApp] Sam — gg last game" in out

    empty = StubHub(data={"items": []})
    monkeypatch.setattr(node_hub, "HUB", empty)
    assert tool.phone_notifications({}) == "No notifications on the phone."


def test_hub_failure_renders_as_failed_outcome(monkeypatch):
    stub = StubHub(error=NodeHubError("timeout", "did not answer in time"))
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = tool.phone_read_screen({})
    assert "status=failed" in out
    assert "did not answer in time" in out


# ── control side: the gate ──────────────────────────────────────────────────

@pytest.mark.parametrize("fn,args,action,sent", [
    (tool.phone_tap, {"ref": "7"}, "tap", {"ref": "7"}),
    (tool.phone_tap, {"x": 10, "y": 20}, "tap", {"x": 10, "y": 20}),
    (tool.phone_swipe, {"x1": 0, "y1": 100, "x2": 0, "y2": 10},
     "swipe", {"x1": 0, "y1": 100, "x2": 0, "y2": 10,
               "duration_ms": 300}),
    (tool.phone_type, {"text": "hello", "ref": "2"}, "type",
     {"text": "hello", "ref": "2"}),
    (tool.phone_key, {"key": "back"}, "key", {"key": "back"}),
])
def test_gated_tools_park_under_phone_control(monkeypatch, confirm_spy,
                                               fn, args, action, sent):
    stub = StubHub(data={})
    monkeypatch.setattr(node_hub, "HUB", stub)

    out = fn(args)
    assert out == "[CONFIRMATION_PENDING] test"
    assert stub.calls == []                      # nothing sent yet
    (call,) = confirm_spy
    assert call["key"] == "phone_control"

    # The user pressed CONFIRM: the parked callable does the work.
    result = call["run"]()
    assert stub.calls == [(action, sent)]
    assert "on the phone." in result


@pytest.mark.parametrize("fn,args", [
    (tool.phone_tap, {}),
    (tool.phone_tap, {"x": 10}),
    (tool.phone_swipe, {"x1": 0, "y1": 0, "x2": 5}),
    (tool.phone_type, {}),
    (tool.phone_type, {"text": ""}),
    (tool.phone_key, {}),
    (tool.phone_key, {"key": "volume_up"}),
])
def test_gated_tools_validate_before_the_banner(monkeypatch, confirm_spy,
                                                fn, args):
    stub = StubHub(data={})
    monkeypatch.setattr(node_hub, "HUB", stub)
    out = fn(args)
    assert "status=failed" in out
    assert confirm_spy == []       # no banner for a malformed call
    assert stub.calls == []


def test_gated_tools_refuse_inside_a_trusted_scope(monkeypatch, confirm_spy):
    stub = StubHub(data={})
    monkeypatch.setattr(node_hub, "HUB", stub)
    monkeypatch.setattr(confirm, "in_trusted_scope", lambda: True)
    out = tool.phone_tap({"ref": "1"})
    assert "status=failed" in out
    assert "routine replay" in out
    assert confirm_spy == []
    assert stub.calls == []


def test_read_side_never_touches_the_gate(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("read-side tools must not call confirm")

    monkeypatch.setattr(confirm, "request", boom)
    stub = StubHub(data={"apps": [], "nodes": [], "items": []},
                   nodes_list=[])
    monkeypatch.setattr(node_hub, "HUB", stub)
    tool.phone_list_apps({})
    tool.phone_read_screen({})
    tool.phone_notifications({})
    tool.phone_open_app({"label": "Convoke"})
