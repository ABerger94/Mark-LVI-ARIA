"""
core/notify.py — phone notifications: one call, three sinks.

WHY THIS EXISTS
    Until now everything ARIA noticed reached the user exactly one
    way: her voice, in the room, at the moment it happened. Anything
    that fired while she was asleep was queued for the wake-up, and
    anything that fired while the user was out of the room might as
    well not have fired. This module is the channel that survives
    both: a notification the phone shows whether or not anybody is
    home to hear her.

THE SHAPE — ONE CALL, THREE SINKS
    notify(title, body, source) fans out to:

      (a) the live toast — a {"type": "notification", ...} broadcast
          over the dashboard's /ws channel, so an open dashboard page
          shows it the moment it happens. The broadcast itself is a
          bound hook (bind_broadcaster, wired by main.py to the
          dashboard it owns): no dashboard, no hook, sink skipped
          silently — the other two sinks still carried it.
      (b) Web Push — pywebpush to every stored subscription. This is
          how a notification reaches a phone whose dashboard is not
          open: the push services (Apple's, Mozilla's, Google's)
          wake the dashboard's service worker, which shows it. No
          third-party relay of ours is involved — the subscription
          endpoint IS the relay, created by the phone's own browser
          when the user pressed Enable on the dashboard page.
      (c) the missed queue — memory/notify_queue.json, capped at 50,
          oldest dropped. The dashboard shows it on load, so what
          arrived while the phone was off is listed, not lost; a
          dismiss clears entries. The queue is written on EVERY
          delivery, not only failed ones: it is the record, and the
          toast/push are just how it interrupts.

DEDUPE
    The identical (title, body) inside 60 minutes is delivered once.
    The record is the queue file's "last" entry (it survives
    restarts), mirrored in-process. One narrow widening: the same
    BODY inside 2 minutes counts as the same event whatever title it
    arrived under — a price crossing that lands while she sleeps is
    announced by two sources (the price pass and the asleep-queue
    hook in main.py) seconds apart, and one event is one
    notification. Two genuinely different announcements do not share
    a byte-identical body inside two minutes.

DEGRADATION — PUSH IS OPTIONAL, THE REST IS NOT
    pywebpush is imported behind a guard, the way the MCP SDK and
    python-dateutil are elsewhere: an install without it runs
    exactly as before, with sinks (a) and (c) working and push
    quietly absent. status() reports webpush_available so the
    dashboard (and diagnostics through it) says so instead of the
    user discovering a silent phone. Nothing in this module raises
    into a caller: a dead push endpoint, a corrupt store, a hung
    network — each is logged, pruned or skipped, never propagated.
    A 404/410 from a push service means the subscription is gone
    for good (app uninstalled, permission revoked, token rotated)
    and it is pruned; any other failure keeps the subscription —
    a network blip must not unsubscribe the user's phone.

THE FLAG
    config's notify_enabled (default on) silences all three sinks
    together — off means nothing is toasted, pushed OR queued. The
    flag is read on every call, so flipping it takes effect at once.

STORES
    memory/push_subscriptions.json — [{endpoint, keys, created}]
    memory/notify_queue.json      — {"entries": [...], "last": {...},
                                     "model_calls": [epoch, ...]}
    Both written with the Phase G1 discipline: temp file +
    os.replace, the outgoing file snapshotted into memory/backups/
    first (rotation of 5), a file that fails to parse recovered from
    the newest good backup. "model_calls" is the notify_phone tool's
    rate-limit ledger — it lives here so a restart cannot reset the
    limit the way an in-process list would.
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional

# ── Optional push transport ──────────────────────────────────────────────────
# Guarded exactly like core/mcp_client.py's SDK import: absence
# degrades one sink, never the module.
try:
    from pywebpush import WebPushException, webpush as _webpush_send
except Exception:  # ImportError, or a half-installed closure
    _webpush_send = None
    WebPushException = None  # type: ignore[assignment]

try:
    from py_vapid import Vapid as _Vapid
except Exception:
    _Vapid = None

from memory import config_manager as _cm


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch these constants to tmp paths (the scheduler /
# pricecheck pattern).
SUBS_PATH = _base_dir() / "memory" / "push_subscriptions.json"
QUEUE_PATH = _base_dir() / "memory" / "notify_queue.json"

# The subject claim on every VAPID JWT: who is sending. A URL claim,
# per the Web Push spec's mailto:/https: choice — this one names the
# project, not a person.
VAPID_SUBJECT = "https://github.com/ABerger94/Mark-LVI-ARIA"

QUEUE_CAP = 50
DEDUPE_WINDOW_S = 3600.0        # identical (title, body): once an hour
SAME_BODY_WINDOW_S = 120.0      # same body, any title: the same event
BACKUP_KEEP = 5

_lock = threading.RLock()

# Tests replace this to control dedupe and rate-limit windows without
# sleeping; production callers never touch it.
clock = time.time


def _subs_path() -> Path:
    return SUBS_PATH


def _queue_path() -> Path:
    return QUEUE_PATH


# ── The stores: Phase G1's discipline, sized for two small files ────────────

def _parse(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def _backup_slots(path: Path) -> list[Path]:
    d = path.parent / "backups"
    return [d / f"{path.stem}.{i}.json" for i in range(1, BACKUP_KEEP + 1)]


def _snapshot(path: Path) -> None:
    """Copy the CURRENT live file into its backup rotation — only if
    it still parses. Never raises."""
    try:
        if not path.exists():
            return
        if _parse(path.read_text(encoding="utf-8")) is None:
            return
        slots = _backup_slots(path)
        slots[0].parent.mkdir(parents=True, exist_ok=True)
        for i in range(len(slots) - 1, 0, -1):
            if slots[i - 1].exists():
                os.replace(slots[i - 1], slots[i])
        shutil.copyfile(path, slots[0])
    except Exception as e:
        print(f"[Notify] ⚠️ Backup failed: {e}")


def _read_store(path: Path, default):
    """The parsed store, `default` when the file is missing — and,
    when it exists but cannot be parsed, the newest backup that can
    be. Recovery reads only; the next write heals the live file."""
    try:
        if not path.exists():
            return default
        data = _parse(path.read_text(encoding="utf-8"))
        if data is not None:
            return data
        for slot in _backup_slots(path):
            if slot.exists():
                data = _parse(slot.read_text(encoding="utf-8"))
                if data is not None:
                    print(f"[Notify] 🛟 {path.name} unreadable; "
                          f"recovered from {slot.name}")
                    return data
    except Exception as e:
        print(f"[Notify] ⚠️ Store read failed ({path.name}): {e}")
    return default


def _write_store(path: Path, data) -> None:
    """The one way a notify store reaches disk: snapshot the outgoing
    file, then temp-file + os.replace. A failed write leaves the
    previous valid store in place."""
    text = json.dumps(data, indent=2, ensure_ascii=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        _snapshot(path)
        tmp = path.with_name(path.name + ".tmp")
        try:
            tmp.write_text(text, encoding="utf-8")
            os.replace(tmp, path)
        except Exception:
            try:
                tmp.unlink(missing_ok=True)
            except Exception:
                pass
            raise


# ── VAPID identity ───────────────────────────────────────────────────────────

def webpush_available() -> bool:
    """Is the push transport installed at all? (pywebpush present;
    http-ece rides inside it.)"""
    return _webpush_send is not None


def ensure_vapid() -> tuple[str, str]:
    """(public, private) VAPID keys — generated once, on first use,
    stored through the config funnel, and reused forever after: a
    fresh keypair would orphan every subscription made under the
    old one. ('', '') when py_vapid is not installed."""
    pub, priv = _cm.get_vapid_keys()
    if pub and priv:
        return pub, priv
    if _Vapid is None:
        return "", ""
    try:
        from cryptography.hazmat.primitives import serialization
        vapid = _Vapid()
        vapid.generate_keys()
        # The uncompressed EC point (0x04 ‖ x ‖ y) — what the browser
        # expects as applicationServerKey, base64url without padding.
        raw = vapid.public_key.public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint)
        pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")
        priv = vapid.private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode("ascii")
    except Exception as e:
        print(f"[Notify] ⚠️ VAPID key generation failed: {e}")
        return "", ""
    _cm.save_vapid_keys(pub, priv)
    return pub, priv


def vapid_public_key() -> str:
    """The applicationServerKey for the dashboard page's subscribe
    call — the stored public key, generating the pair on first ask.
    '' when push cannot be offered at all."""
    if _Vapid is None:
        return ""
    pub, _priv = ensure_vapid()
    return pub


# ── Subscriptions ────────────────────────────────────────────────────────────

def _load_subs() -> list:
    data = _read_store(_subs_path(), [])
    return data if isinstance(data, list) else []


def _save_subs(subs: list) -> None:
    _write_store(_subs_path(), subs)


def list_subscriptions() -> list[dict]:
    return [dict(s) for s in _load_subs() if isinstance(s, dict)]


def subscription_count() -> int:
    return len(_load_subs())


def add_subscription(sub: dict) -> bool:
    """Store a browser PushSubscription (its toJSON shape:
    {endpoint, keys: {p256dh, auth}}). Deduped by endpoint — a phone
    re-subscribing refreshes its keys rather than stacking a second
    record. True when the subscription is stored when the call
    returns (new, refreshed, or already there); False only when the
    value is not a subscription at all."""
    if not isinstance(sub, dict):
        return False
    endpoint = str(sub.get("endpoint") or "").strip()
    keys = sub.get("keys")
    if not endpoint or not isinstance(keys, dict) \
            or not keys.get("p256dh") or not keys.get("auth"):
        return False
    record = {"endpoint": endpoint,
              "keys": {"p256dh": str(keys["p256dh"]),
                       "auth": str(keys["auth"])},
              "created": clock()}
    with _lock:
        subs = _load_subs()
        for i, s in enumerate(subs):
            if isinstance(s, dict) and s.get("endpoint") == endpoint:
                if s.get("keys") == record["keys"]:
                    return True          # already stored, as-is
                record["created"] = s.get("created", record["created"])
                subs[i] = record
                _save_subs(subs)
                return True
        subs.append(record)
        _save_subs(subs)
        return True


def remove_subscription(endpoint: str) -> bool:
    """Drop a subscription by endpoint. True when one was removed."""
    endpoint = str(endpoint or "").strip()
    if not endpoint:
        return False
    with _lock:
        subs = _load_subs()
        kept = [s for s in subs
                if not (isinstance(s, dict)
                        and s.get("endpoint") == endpoint)]
        if len(kept) == len(subs):
            return False
        _save_subs(kept)
        return True


# ── The missed queue + delivery records ──────────────────────────────────────

def _load_queue() -> dict:
    data = _read_store(_queue_path(), {})
    if not isinstance(data, dict):
        data = {}
    entries = data.get("entries")
    if not isinstance(entries, list):
        entries = []
    calls = data.get("model_calls")
    if not isinstance(calls, list):
        calls = []
    return {"entries": entries, "last": data.get("last"),
            "model_calls": [t for t in calls if isinstance(t, (int, float))]}


def _save_queue(data: dict) -> None:
    _write_store(_queue_path(), data)


def queued() -> list[dict]:
    """The missed-queue entries, oldest first — what the dashboard
    shows on load."""
    return [dict(e) for e in _load_queue()["entries"]
            if isinstance(e, dict)]


def dismiss(ids=None) -> int:
    """Clear queue entries: the given id (or list of ids), or ALL of
    them when called with nothing. Returns how many were removed."""
    with _lock:
        data = _load_queue()
        entries = data["entries"]
        if not ids:
            removed = len(entries)
            data["entries"] = []
        else:
            wanted = {ids} if isinstance(ids, str) else set(ids)
            kept = [e for e in entries
                    if not (isinstance(e, dict) and e.get("id") in wanted)]
            removed = len(entries) - len(kept)
            data["entries"] = kept
        if removed:
            _save_queue(data)
        return removed


def model_call_times() -> list[float]:
    """When the notify_phone tool last delivered, oldest first — the
    rate-limit ledger (see the module docstring for why it persists)."""
    return list(_load_queue()["model_calls"])


def record_model_call(ts: float) -> None:
    with _lock:
        data = _load_queue()
        calls = [t for t in data["model_calls"] if ts - t < 7200.0]
        calls.append(float(ts))
        data["model_calls"] = calls
        _save_queue(data)


# ── The toast sink: a bound broadcaster ──────────────────────────────────────

_broadcaster: Optional[Callable[[dict], None]] = None


def bind_broadcaster(fn) -> None:
    """Wire the dashboard's broadcast in (main.py owns the server;
    this module owns no event loop). fn(msg) is called synchronously
    and must not block — main.py's binding schedules the send."""
    global _broadcaster
    _broadcaster = fn


def _broadcast(msg: dict) -> bool:
    fn = _broadcaster
    if fn is None:
        return False
    try:
        fn(msg)
        return True
    except Exception as e:
        print(f"[Notify] ⚠️ Toast broadcast failed: {e}")
        return False


# ── The push sink ────────────────────────────────────────────────────────────

def _send_push(sub: dict, payload: str, priv: str) -> str:
    """One push attempt: "ok", "gone" (404/410 — prune it) or
    "error" (logged, subscription kept). The seam the tests stub;
    never raises."""
    try:
        _webpush_send(
            subscription_info=sub,
            data=payload,
            vapid_private_key=priv,
            vapid_claims={"sub": VAPID_SUBJECT},
            ttl=3600,
        )
        return "ok"
    except WebPushException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status in (404, 410):
            return "gone"
        print(f"[Notify] ⚠️ Push failed ({status}): {e}")
        return "error"
    except Exception as e:
        print(f"[Notify] ⚠️ Push failed: {e}")
        return "error"


def _push_all(title: str, body: str, tag: str) -> tuple[int, int]:
    """Push to every subscription. Returns (delivered, pruned)."""
    if not webpush_available():
        return 0, 0
    subs = list_subscriptions()
    if not subs:
        return 0, 0
    _pub, priv = ensure_vapid()
    if not priv:
        return 0, 0
    payload = json.dumps({"title": title, "body": body, "tag": tag})
    delivered = 0
    gone: list[str] = []
    for sub in subs:
        verdict = _send_push(sub, payload, priv)
        if verdict == "ok":
            delivered += 1
        elif verdict == "gone":
            gone.append(sub.get("endpoint", ""))
    for endpoint in gone:
        remove_subscription(endpoint)
    return delivered, len(gone)


# ── Status (the dashboard's diagnostics surface for this feature) ───────────

def status() -> dict:
    pub, priv = _cm.get_vapid_keys()
    return {
        "notify_enabled": bool(_cm.get_notify_enabled()),
        "webpush_available": webpush_available(),
        "vapid_configured": bool(pub and priv),
        "subscriptions": subscription_count(),
        "queued": len(_load_queue()["entries"]),
    }


# ── The one call ─────────────────────────────────────────────────────────────

# In-process mirror of the queue file's "last" record — the fast
# half of the dedupe check; the file is the durable half.
_recent: dict = {}


def _is_duplicate(title: str, body: str, now: float, last) -> bool:
    records = [r for r in (_recent.get("last"), last) if isinstance(r, dict)]
    for rec in records:
        try:
            age = now - float(rec.get("ts", 0))
        except (TypeError, ValueError):
            continue
        if rec.get("title") == title and rec.get("body") == body \
                and age < DEDUPE_WINDOW_S:
            return True
        if rec.get("body") == body and age < SAME_BODY_WINDOW_S:
            return True
    return False


def notify(title: str, body: str, source: str = "system",
           now: Optional[float] = None) -> dict:
    """Deliver a notification to all three sinks. Returns the facts:

        {ok, enabled, deduped, toast, pushed, pruned, queued,
         webpush_available}

    Never raises. `now` is the tests' clock seam (production callers
    leave it None and the module clock rules)."""
    title = str(title or "").strip()
    body = str(body or "").strip()
    result = {"ok": False, "enabled": True, "deduped": False,
              "toast": False, "pushed": 0, "pruned": 0, "queued": False,
              "webpush_available": webpush_available()}
    if not title and not body:
        result["reason"] = "empty"
        return result
    if not title:
        title = body[:48]
    try:
        enabled = bool(_cm.get_notify_enabled())
    except Exception:
        enabled = True
    if not enabled:
        result["enabled"] = False
        result["reason"] = "notify_enabled is off"
        return result
    ts = float(now if now is not None else clock())

    with _lock:
        data = _load_queue()
        if _is_duplicate(title, body, ts, data.get("last")):
            result["deduped"] = True
            result["ok"] = True
            return result
        entry = {"id": uuid.uuid4().hex[:12], "title": title,
                 "body": body, "source": str(source or "system"),
                 "ts": ts}
        entries = data["entries"]
        entries.append(entry)
        if len(entries) > QUEUE_CAP:
            del entries[:len(entries) - QUEUE_CAP]
        data["entries"] = entries
        data["last"] = {"title": title, "body": body, "ts": ts}
        try:
            _save_queue(data)
            result["queued"] = True
        except Exception as e:
            print(f"[Notify] ⚠️ Queue write failed: {e}")
        _recent["last"] = data["last"]

    result["toast"] = _broadcast(
        {"type": "notification", "title": title, "body": body,
         "source": entry["source"], "ts": ts})
    pushed, pruned = _push_all(title, body, entry["id"])
    result["pushed"] = pushed
    result["pruned"] = pruned
    result["ok"] = True
    return result
