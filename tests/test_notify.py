"""Phone notifications — core/notify.py, the notify_phone tool, the
dashboard's push surface, and the two system-source joins.

What is pinned here:

  * The VAPID identity is generated ONCE and reused — a fresh keypair
    would orphan every subscription made under the old one. The
    public half is the base64url uncompressed EC point the browser
    expects as applicationServerKey (65 bytes, 0x04 first).
  * Subscriptions dedupe by endpoint (a re-subscribe refreshes keys,
    never stacks), unsubscribe removes, and the /api/push/* routes
    sit behind the dashboard's bearer auth while the manifest, the
    service worker and the icons — fetched by the browser itself,
    carrying nothing secret — answer without it.
  * notify() fans out to all three sinks: the /ws broadcast in its
    exact {"type": "notification", ...} shape (pinned over a real
    websocket, the dashboard suites' own pattern), pywebpush to
    every subscription (a 404/410 prunes; any other failure keeps
    the subscription and never raises), and the missed queue.
  * Dedupe: identical (title, body) inside 60 minutes delivers
    once; the same body inside 2 minutes folds across titles (the
    price pass and the asleep hook announce one crossing twice).
  * notify_enabled off silences ALL sinks — including the queue.
  * The tool's rate discipline: 1 per 5 minutes, 6 per rolling hour,
    counted from the PERSISTED ledger (a restart cannot reset it),
    and only delivered calls spend the budget.
  * The joins: a price crossing notifies beside its spoken line, and
    a scheduler line that lands while she sleeps pushes through the
    bound hook — while she is awake it does not.
  * Degradation: with the push transport absent, notify() still
    toasts and queues, and status() says push is unavailable.

Seams, as elsewhere: stores and config on tmp paths, the module
clock replaced, the push transport stubbed at _send_push, JarvisLive
built with __new__ and only the attributes the hook reads.
"""
import base64
import json
import struct
import time
from pathlib import Path

import pytest

import actions.notify_phone as tool
from core import notify, pricecheck, scheduler
from memory import config_manager as cm

ROOT = Path(__file__).resolve().parent.parent
T0 = 1_800_000_000.0

ENDPOINT = "https://push.example.com/abc"


def _sub(endpoint=ENDPOINT, auth="a" * 16):
    return {"endpoint": endpoint,
            "keys": {"p256dh": "BP" + "x" * 40, "auth": auth}}


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """notify's stores + the config on tmp paths, a fake clock, no
    broadcaster, and a recording push transport whose verdicts can
    be steered per endpoint via state["verdicts"]."""
    monkeypatch.setattr(cm, "CONFIG_FILE", tmp_path / "api_keys.json")
    monkeypatch.setattr(notify, "SUBS_PATH",
                        tmp_path / "push_subscriptions.json")
    monkeypatch.setattr(notify, "QUEUE_PATH",
                        tmp_path / "notify_queue.json")
    state = {"now": T0, "sent": [], "verdicts": {}}
    monkeypatch.setattr(notify, "clock", lambda: state["now"])

    def _fake_send(sub, payload, priv):
        state["sent"].append((sub, payload, priv))
        return state["verdicts"].get(sub.get("endpoint"), "ok")

    monkeypatch.setattr(notify, "_send_push", _fake_send)
    notify._recent.clear()
    notify.bind_broadcaster(None)
    yield state
    notify._recent.clear()
    notify.bind_broadcaster(None)


# ── VAPID identity ───────────────────────────────────────────────────────────

def test_vapid_generated_once_and_reused(env, monkeypatch):
    generated = []
    real = notify._Vapid

    class Counting(real):
        def generate_keys(self):
            generated.append(1)
            return super().generate_keys()

    monkeypatch.setattr(notify, "_Vapid", Counting)
    pub1, priv1 = notify.ensure_vapid()
    pub2, priv2 = notify.ensure_vapid()
    assert generated == [1]                      # once, not per call
    assert (pub1, priv1) == (pub2, priv2)
    assert cm.get_vapid_keys() == (pub1, priv1)  # persisted via config
    # The browser's applicationServerKey shape: base64url of the
    # 65-byte uncompressed point.
    raw = base64.urlsafe_b64decode(pub1 + "=" * (-len(pub1) % 4))
    assert len(raw) == 65 and raw[0] == 4
    assert "PRIVATE KEY" in priv1


def test_push_absent_degrades_to_toast_and_queue(env, monkeypatch):
    monkeypatch.setattr(notify, "_webpush_send", None)
    monkeypatch.setattr(notify, "_Vapid", None)
    assert notify.webpush_available() is False
    assert notify.vapid_public_key() == ""
    assert notify.status()["webpush_available"] is False
    seen = []
    notify.bind_broadcaster(seen.append)
    res = notify.notify("Deploy", "Done.", source="test")
    assert res["ok"] and res["toast"] and res["queued"]
    assert res["pushed"] == 0
    assert seen and seen[0]["type"] == "notification"


# ── Subscriptions ────────────────────────────────────────────────────────────

def test_subscribe_dedupes_by_endpoint(env):
    assert notify.add_subscription(_sub()) is True
    assert notify.add_subscription(_sub()) is True      # identical: stored
    assert notify.subscription_count() == 1             # …but never stacked
    rotated = _sub(auth="b" * 16)                       # key rotation
    assert notify.add_subscription(rotated) is True     # refreshes in place
    assert notify.subscription_count() == 1
    assert notify.list_subscriptions()[0]["keys"]["auth"] == "b" * 16
    assert notify.add_subscription(
        _sub("https://push.example.com/other")) is True
    assert notify.subscription_count() == 2
    # Malformed subscriptions are refused, never stored.
    assert notify.add_subscription({"endpoint": ""}) is False
    assert notify.add_subscription({"keys": {}}) is False
    assert notify.add_subscription("nope") is False
    assert notify.subscription_count() == 2


def test_unsubscribe(env):
    notify.add_subscription(_sub())
    assert notify.remove_subscription(ENDPOINT) is True
    assert notify.remove_subscription(ENDPOINT) is False
    assert notify.subscription_count() == 0


# ── The fan-out ──────────────────────────────────────────────────────────────

def test_notify_fans_out_to_all_three_sinks(env):
    notify.add_subscription(_sub())
    seen = []
    notify.bind_broadcaster(seen.append)
    res = notify.notify("Deploy", "Build finished.", source="test")
    assert res["ok"] and res["queued"] and res["toast"]
    assert res["pushed"] == 1 and res["pruned"] == 0
    (msg,) = seen
    assert msg == {"type": "notification", "title": "Deploy",
                   "body": "Build finished.", "source": "test",
                   "ts": T0}
    (_sub_sent, payload, priv) = env["sent"][0]
    data = json.loads(payload)
    assert data["title"] == "Deploy"
    assert data["body"] == "Build finished."
    (entry,) = notify.queued()
    assert entry["title"] == "Deploy" and entry["source"] == "test"
    assert entry["ts"] == T0
    assert data["tag"] == entry["id"]       # the tag IS the queue id
    assert priv == cm.get_vapid_keys()[1]   # signed with the stored key


def test_push_gone_prunes_other_errors_keep(env):
    notify.add_subscription(_sub())
    notify.add_subscription(_sub("https://push.example.com/other"))
    env["verdicts"] = {ENDPOINT: "gone",
                       "https://push.example.com/other": "error"}
    res = notify.notify("Deploy", "Done.", source="test")
    assert res["pushed"] == 0 and res["pruned"] == 1
    remaining = [s["endpoint"] for s in notify.list_subscriptions()]
    assert remaining == ["https://push.example.com/other"]


def test_send_push_verdicts(monkeypatch):
    """The transport seam itself: 410 is 'gone', a network failure
    is 'error' (kept), a clean send is 'ok' — and none of them raise."""
    from pywebpush import WebPushException

    class _Resp:
        def __init__(self, code):
            self.status_code = code

    def _gone(**_kw):
        raise WebPushException("gone", response=_Resp(410))

    def _notfound(**_kw):
        raise WebPushException("gone", response=_Resp(404))

    def _down(**_kw):
        raise ConnectionError("no route")

    def _fine(**_kw):
        return None

    for fn, want in ((_gone, "gone"), (_notfound, "gone"),
                     (_down, "error"), (_fine, "ok")):
        monkeypatch.setattr(notify, "_webpush_send", fn)
        assert notify._send_push(_sub(), "{}", "priv") == want


def test_stores_persist_on_disk(env, tmp_path):
    notify.add_subscription(_sub())
    notify.notify("A", "B")
    subs = json.loads(
        (tmp_path / "push_subscriptions.json").read_text())
    assert subs[0]["endpoint"] == ENDPOINT
    queue = json.loads((tmp_path / "notify_queue.json").read_text())
    assert queue["entries"][0]["title"] == "A"
    assert queue["last"]["body"] == "B"


# ── Dedupe + the flag + the queue ────────────────────────────────────────────

def test_dedupe_window(env):
    notify.notify("Deploy", "Done.", source="test")
    env["now"] += 1800
    res = notify.notify("Deploy", "Done.", source="test")
    assert res["deduped"] is True
    assert len(notify.queued()) == 1
    env["now"] += 1801                       # 3601s after the first
    res = notify.notify("Deploy", "Done.", source="test")
    assert res["deduped"] is False
    assert len(notify.queued()) == 2


def test_same_body_folds_across_titles(env):
    """One crossing, two announcers (the price pass and main's
    asleep hook), seconds apart: one notification."""
    notify.notify("Price watch", "Widget dropped.", source="price_watch")
    env["now"] += 30
    res = notify.notify("ARIA", "Widget dropped.", source="scheduler")
    assert res["deduped"] is True
    env["now"] += 300                        # outside the fold window
    res = notify.notify("ARIA", "Widget dropped.", source="scheduler")
    assert res["deduped"] is False


def test_notify_disabled_silences_everything(env):
    cm.save_notify_enabled(False)
    seen = []
    notify.bind_broadcaster(seen.append)
    notify.add_subscription(_sub())
    res = notify.notify("Deploy", "Done.")
    assert res["enabled"] is False and res["ok"] is False
    assert seen == [] and env["sent"] == [] and notify.queued() == []


def test_queue_cap_and_dismiss(env):
    for i in range(55):
        notify.notify(f"T{i}", f"body {i}")
    items = notify.queued()
    assert len(items) == 50                  # capped, oldest dropped
    assert items[0]["title"] == "T5" and items[-1]["title"] == "T54"
    assert notify.dismiss(items[0]["id"]) == 1
    assert notify.queued()[0]["title"] == "T6"
    assert notify.dismiss() == 49            # no ids: clear all
    assert notify.queued() == []


# ── The model tool ───────────────────────────────────────────────────────────

def test_tool_rate_limits(env):
    call = lambda body: tool.notify_phone({"title": "Job", "body": body})
    assert call("one").startswith("status=done")
    refused = call("two")
    assert refused.startswith("status=failed")
    assert "5 minutes" in refused            # the gap bites first
    env["now"] += 301
    assert call("three").startswith("status=done")
    for i in range(4):                       # six inside the hour
        env["now"] += 301
        assert call(f"n{i}").startswith("status=done"), i
    env["now"] += 301
    refused = call("seven")
    assert refused.startswith("status=failed")
    assert "hourly cap" in refused
    env["now"] = T0 + 3601                   # first call ages out
    assert call("later").startswith("status=done")


def test_tool_refusals_do_not_spend_budget(env):
    # A deduped attempt (a system source delivered the same text
    # first) is a failure that must NOT consume the rate budget.
    notify.notify("Job", "same text", source="scheduler")
    res = tool.notify_phone({"title": "Job", "body": "same text"})
    assert res.startswith("status=failed")
    assert "not sent twice" in res
    assert notify.model_call_times() == []
    # And a validation failure spends nothing either.
    assert tool.notify_phone({"title": "Job"}).startswith("status=failed")
    assert notify.model_call_times() == []


def test_tool_discovered_and_excluded_from_delegation():
    import main
    from core.action_loader import discover_actions

    assert "notify_phone" in main.JarvisLive._DELEGATED_EXCLUDE
    reg = discover_actions(actions_dir=ROOT / "actions",
                           reserved_names=set(), logger=lambda m: None)
    assert "notify_phone" in reg.names()


# ── The system-source joins ──────────────────────────────────────────────────

@pytest.fixture()
def wires(tmp_path, monkeypatch, env):
    monkeypatch.setattr(scheduler, "STORE_PATH",
                        tmp_path / "scheduler.json")
    monkeypatch.setattr(pricecheck, "STORE_PATH",
                        tmp_path / "price_watches.json")
    said = []
    scheduler.bind(say=lambda t: (said.append(t), True)[1],
                   run_prompt=lambda t: True, briefing=lambda: True)
    pages = {}
    monkeypatch.setattr(pricecheck, "fetch_page",
                        lambda url: pages[url])
    yield {"said": said, "pages": pages}
    scheduler.bind(None, None)


def test_price_crossing_also_notifies(wires):
    url = "https://example.com/widget"
    pricecheck.add_watch(url, 50.0, "Widget")
    scheduler.add_task("pricecheck", "pricecheck",
                       interval_s=3600, now=T0 - 3600)
    wires["pages"][url] = (
        '<html><script type="application/ld+json">'
        '{"price": "45.00"}</script></html>')
    scheduler.tick(T0)
    assert wires["said"] == [
        "Price alert: Widget is $45.00, at or below your "
        "$50.00 target."]
    (entry,) = notify.queued()
    assert entry["title"] == "Price watch"
    assert entry["body"] == wires["said"][0]     # the same sentence
    assert entry["source"] == "price_watch"


def test_asleep_scheduler_line_also_pushes(env):
    import main

    class _UI:
        def write_log(self, msg):
            pass

    live = main.JarvisLive.__new__(main.JarvisLive)
    live.ui = _UI()
    live.session = object()
    live._awake = False
    live._pending_announce = []
    got = []
    live._notify_hook = \
        lambda t, b, source="system": got.append((t, b, source))
    assert live._scheduler_say("Timer done: tea.") is True
    assert live._pending_announce == ["Timer done: tea."]
    assert got and got[0][1] == "Timer done: tea."
    assert got[0][2] == "scheduler"

    live._awake = True                       # awake: spoken, not pushed
    live.plugin_say = lambda text: None
    got.clear()
    assert live._scheduler_say("Timer done: tea.") is True
    assert got == []


# ── The dashboard surface ────────────────────────────────────────────────────

def _server():
    import dashboard.server as srv_mod
    if not srv_mod._DEPS_OK:
        pytest.skip("fastapi/uvicorn not installed in this environment")
    return srv_mod.DashboardServer()


@pytest.fixture()
def client(env):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    srv = _server()
    c = TestClient(srv.app)
    key = srv.new_key()
    tok = c.post("/login", json={"pin": key}).json()["token"]
    return c, {"Authorization": f"Bearer {tok}"}


def test_push_endpoints_need_auth(client):
    c, _h = client
    assert c.get("/api/push/status").status_code == 401
    assert c.get("/api/push/public-key").status_code == 401
    assert c.post("/api/push/subscribe", json=_sub()).status_code == 401
    assert c.get("/api/notify/queue").status_code == 401


def test_push_subscribe_status_unsubscribe_flow(client):
    c, h = client
    r = c.post("/api/push/subscribe", json=_sub(), headers=h)
    assert r.status_code == 200 and r.json()["subscriptions"] == 1
    r = c.post("/api/push/subscribe", json=_sub(), headers=h)
    assert r.json()["subscriptions"] == 1        # dedupe over HTTP too
    r = c.get("/api/push/status", headers=h)
    body = r.json()
    assert body["subscriptions"] == 1
    assert body["webpush_available"] is True
    assert body["notify_enabled"] is True
    r = c.post("/api/push/subscribe", json={"endpoint": "x"}, headers=h)
    assert r.status_code == 400                  # not a subscription
    r = c.post("/api/push/unsubscribe",
               json={"endpoint": ENDPOINT}, headers=h)
    assert r.json()["removed"] is True
    assert c.get("/api/push/status",
                 headers=h).json()["subscriptions"] == 0


def test_public_key_endpoint_matches_config(client):
    c, h = client
    r = c.get("/api/push/public-key", headers=h)
    assert r.status_code == 200
    assert r.json()["public_key"] == notify.ensure_vapid()[0]
    assert r.json()["webpush_available"] is True


def test_queue_endpoints(client):
    c, h = client
    notify.notify("Deploy", "Done.", source="test")
    r = c.get("/api/notify/queue", headers=h)
    (item,) = r.json()["items"]
    assert item["title"] == "Deploy"
    r = c.post("/api/notify/dismiss", json={"id": item["id"]},
               headers=h)
    assert r.json()["removed"] == 1 and r.json()["items"] == []
    notify.notify("Again", "More.", source="test")
    r = c.post("/api/notify/dismiss", json={"all": True}, headers=h)
    assert r.json()["removed"] == 1 and r.json()["items"] == []


def test_manifest_sw_and_icons_need_no_auth(client):
    """The browser fetches these itself, without headers — and they
    carry nothing secret."""
    c, _h = client
    r = c.get("/manifest.webmanifest")
    assert r.status_code == 200
    assert "application/manifest+json" in r.headers["content-type"]
    manifest = r.json()
    assert manifest["display"] == "standalone"
    assert manifest["name"]                        # the configured name
    assert {i["sizes"] for i in manifest["icons"]} == {"192x192", "512x512"}

    r = c.get("/sw.js")
    assert r.status_code == 200
    assert "showNotification" in r.text
    assert "notificationclick" in r.text
    assert r.headers.get("service-worker-allowed") == "/"

    for size in (192, 512):
        r = c.get(f"/icons/icon-{size}.png")
        assert r.status_code == 200
        assert r.content[:8] == b"\x89PNG\r\n\x1a\n"
        w, h = struct.unpack(">II", r.content[16:24])   # the IHDR
        assert (w, h) == (size, size)
    assert c.get("/icons/icon-100.png").status_code == 404


def test_notification_broadcast_shape_over_ws(env):
    """The toast sink end to end: notify() → the bound broadcaster →
    a real dashboard websocket, in the exact message shape the page
    dispatches on."""
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    srv = _server()
    with TestClient(srv.app) as c:
        key = srv.new_key()
        tok = c.post("/login", json={"pin": key}).json()["token"]
        with c.websocket_connect(f"/ws?token={tok}") as ws:
            notify.bind_broadcaster(
                lambda msg: c.portal.call(srv.broadcast, msg))
            notify.notify("Deploy", "Build finished.", source="test")
            got = None
            deadline = time.time() + 5
            while time.time() < deadline and got is None:
                m = ws.receive_json()
                if m.get("type") == "notification":
                    got = m
            assert got is not None
            assert got["title"] == "Deploy"
            assert got["body"] == "Build finished."
            assert got["source"] == "test"
            assert got["ts"] == T0
