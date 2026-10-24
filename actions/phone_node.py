"""
actions/phone_node.py — ARIA's hands on the user's Android phone.

WHAT THIS IS
    The handlers behind the phone_* tools. The phone runs a small
    node app that connects OUT to her dashboard (core/node_hub.py
    holds this side of the channel); every handler here is one
    send_command to that node and one rendered result. This file is
    a shared helper module — core/action_loader.py discovers one
    TOOL per file, so each phone_* tool's declaration lives in its
    own thin actions/phone_<tool>.py beside this one, importing its
    handler from here (the actions.file_controller pattern).

READ vs CONTROL — NO CONFIRMATION GATE (user's call, 2026-10-24)
    Nothing on the phone is confirmation-gated. Until 2026-10-24,
    tap, swipe, type and key parked behind a HUD confirmation banner
    (core/confirm.py, shared key "phone_control") — one banner per
    step, which broke multi-step phone tasks into a stop-start
    shuffle. The user removed it, in his words: "I don't need her to
    ask permission on my phone." Control handlers now validate their
    arguments and send immediately, so ARIA can chain a whole task —
    read, tap, read, type, key — without pausing for approval.
    One refusal survives, and it is not a permission: inside a
    trusted routine replay a phone control is still refused, because
    a pre-approved step list cannot describe whatever screen the
    phone happens to be showing when the replay fires. That refusal
    is about stale context, not consent — a live call always runs.

SCREEN DISCIPLINE (the descriptions say it to the model too)
    Refs come from phone_read_screen's snapshot and are only valid
    for that screen. Read, act, read again — the handlers are
    stateless, so the discipline lives in the tool descriptions and
    in the result text (the screen render ends by saying how taps
    address it).

NO NODE IS A SENTENCE, NOT AN ERROR SHAPE
    Every handler answers the same plain string when no phone is
    connected, so the model tells the user the one useful thing —
    open the node app and check it says connected — instead of
    dressing a missing phone up as a failed command.
"""

from __future__ import annotations

import time

from core import confirm, node_hub, outcome

NO_NODE = (
    "No phone node is connected. The ARIA Node app on the user's "
    "Android phone has to be running and paired before the phone "
    "can be used — ask the user to open the node app on the phone "
    "and check it shows as connected, then try again.")

# A rendered list longer than this is noise to say out loud; the
# tail note says it was cut rather than pretending it was whole.
LIST_LIMIT = 6000

# The app list gets its own, much larger cap (fixed 2026-10-24 at
# Alek's ask): since the phone node now reports ALL installed apps
# (200-300 packages on a typical phone, ~50 chars a line), the
# 6,000-char cap cut the list at roughly 120 apps and ARIA presented
# the truncated head as if it were the whole phone. 30,000 chars
# covers a full inventory with headroom; anything beyond it still
# gets the honest "[list truncated]" tail note.
APPS_LIST_LIMIT = 30000

_KEY_NAMES = ("back", "home", "recents", "enter")


def _send(tool: str, action: str, args: dict):
    """One command to the node. (data, None) on success, or
    (None, rendered) where rendered is NO_NODE for the no-phone
    case and an outcome.failed line for every other failure."""
    try:
        data = node_hub.HUB.send_command(None, action, args)
        return data, None
    except node_hub.NodeHubError as e:
        if e.code == "no_node":
            return None, NO_NODE
        return None, outcome.failed(tool, str(e))
    except Exception as e:
        return None, outcome.failed(tool, f"the phone command failed: {e}")


def _cap(text: str, limit: int = LIST_LIMIT) -> str:
    if len(text) > limit:
        return text[:limit] + "\n[list truncated]"
    return text


def _ago(secs: float) -> str:
    secs = max(0, int(secs))
    if secs < 5:
        return "just now"
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    return f"{secs // 3600}h {secs % 3600 // 60}m ago"


# ── Read side (ungated) ─────────────────────────────────────────────────────

def phone_status(parameters: dict = None, player=None,
                 session_memory=None) -> str:
    nodes = node_hub.HUB.nodes()
    if not nodes:
        return NO_NODE
    now = time.time()
    lines = []
    for n in nodes:
        caps = ", ".join(str(c) for c in n["capabilities"]) or "none declared"
        lines.append(
            f"Phone node '{n['name']}' ({n['kind'] or 'unknown kind'}) — "
            f"connected {_ago(now - n['connected_at'])}. "
            f"Capabilities: {caps}.")
    return "\n".join(lines)


def phone_list_apps(parameters: dict = None, player=None,
                    session_memory=None) -> str:
    data, err = _send("phone_list_apps", "list_apps", {})
    if err:
        return err
    apps = data.get("apps") or []
    if not apps:
        return "The phone reports no installed apps."
    lines = [f"Apps on the phone ({len(apps)}):"]
    for a in apps:
        if not isinstance(a, dict):
            continue
        label = str(a.get("label") or "").strip()
        pkg = str(a.get("package") or "").strip()
        lines.append(f"- {label} ({pkg})" if label else f"- {pkg}")
    return _cap("\n".join(lines), limit=APPS_LIST_LIMIT)


def phone_open_app(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    params = parameters or {}
    label = str(params.get("label") or "").strip()
    package = str(params.get("package") or "").strip()
    if not label and not package:
        return outcome.failed(
            "phone_open_app",
            "neither an app label nor a package name was given — "
            "name the app to open")
    args = {}
    if package:
        args["package"] = package
    if label:
        args["label"] = label
    data, err = _send("phone_open_app", "open_app", args)
    if err:
        return err
    opened = str(data.get("label") or label or package)
    opened_pkg = str(data.get("package") or package)
    return f"Opened {opened} ({opened_pkg}) on the phone."


def _fmt_screen_node(n: dict) -> str:
    cls = str(n.get("class") or "").rsplit(".", 1)[-1] or "view"
    label = (str(n.get("text") or "").strip()
             or str(n.get("desc") or "").strip())
    flags = [name for name in ("clickable", "editable", "scrollable")
             if n.get(name)]
    bounds = n.get("bounds") or []
    loc = ""
    if isinstance(bounds, (list, tuple)) and len(bounds) == 4:
        try:
            loc = " @ [" + ",".join(str(int(v)) for v in bounds) + "]"
        except (TypeError, ValueError):
            loc = ""
    out = f"{n.get('ref')}: {cls}"
    if label:
        out += f' "{label}"'
    if flags:
        out += f" ({', '.join(flags)})"
    return out + loc


def phone_read_screen(parameters: dict = None, player=None,
                      session_memory=None) -> str:
    data, err = _send("phone_read_screen", "read_screen", {})
    if err:
        return err
    nodes = [n for n in (data.get("nodes") or []) if isinstance(n, dict)]
    pkg = str(data.get("package") or "unknown app")
    head = f"Screen: {pkg} — {len(nodes)} elements"
    if data.get("truncated"):
        head += " (the phone truncated this list)"
    head += "."
    if not nodes:
        return head + " Nothing readable on this screen."
    body = "\n".join(_fmt_screen_node(n) for n in nodes)
    return _cap(head + "\n" + body +
                "\nTo tap an element, pass its line's number to "
                "phone_tap as 'ref'.")


def _fmt_ts(ts) -> str:
    try:
        t = float(ts)
    except (TypeError, ValueError):
        return ""
    if t > 1e12:            # Android reports epoch milliseconds
        t /= 1000.0
    if t <= 0:
        return ""
    try:
        return time.strftime("%a %H:%M", time.localtime(t))
    except (OverflowError, OSError, ValueError):
        return ""


def phone_notifications(parameters: dict = None, player=None,
                        session_memory=None) -> str:
    params = parameters or {}
    try:
        limit = int(float(params.get("limit") or 10))
    except (TypeError, ValueError):
        limit = 10
    limit = max(1, min(50, limit))
    data, err = _send("phone_notifications", "notifications",
                      {"limit": limit})
    if err:
        return err
    items = [i for i in (data.get("items") or []) if isinstance(i, dict)]
    if not items:
        return "No notifications on the phone."
    lines = [f"Notifications on the phone ({len(items)}):"]
    for it in items:
        app = str(it.get("app") or it.get("package") or "unknown app")
        title = str(it.get("title") or "").strip()
        text = str(it.get("text") or "").strip()
        when = _fmt_ts(it.get("ts"))
        line = f"- [{app}] {title}"
        if text:
            line += f" — {text}"
        if when:
            line += f" ({when})"
        lines.append(line)
    return _cap("\n".join(lines))


# ── Control side (ungated since 2026-10-24, at the user's word) ────────────

def _run_control(tool: str, run) -> str:
    """Run a phone control NOW. No confirmation banner — the user
    removed the phone_control gate on 2026-10-24 because per-step
    approval broke multi-step phone tasks. The one surviving check
    is the trusted-scope refusal: a routine replay fires blind, on
    whatever screen the phone happens to show, so it is refused
    rather than run (it is never parked for confirmation either —
    it simply does not run from a replay)."""
    if confirm.in_trusted_scope():
        return outcome.failed(
            tool,
            "phone control never runs inside a routine replay — a "
            "tap, swipe, key or typed text acts on whatever happens "
            "to be on the phone's screen at that moment, which no "
            "pre-approved step list can describe. Ask for it again "
            "as a live call. Nothing was sent to the phone")
    return run()


def _int_param(params: dict, key: str):
    try:
        return int(float(params.get(key)))
    except (TypeError, ValueError):
        return None


def phone_tap(parameters: dict = None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    ref = str(params.get("ref") or "").strip()
    args: dict = {}
    if ref:
        args["ref"] = ref
        target = f"element ref '{ref}'"
    else:
        x = _int_param(params, "x")
        y = _int_param(params, "y")
        if x is None or y is None:
            return outcome.failed(
                "phone_tap",
                "no target was given — pass a 'ref' from "
                "phone_read_screen, or both 'x' and 'y'")
        args = {"x": x, "y": y}
        target = f"coordinates ({x}, {y})"

    def _do() -> str:
        data, err = _send("phone_tap", "tap", args)
        return err if err else f"Tapped {target} on the phone."

    return _run_control("phone_tap", _do)


def phone_swipe(parameters: dict = None, player=None,
                session_memory=None) -> str:
    params = parameters or {}
    coords = {k: _int_param(params, k) for k in ("x1", "y1", "x2", "y2")}
    if any(v is None for v in coords.values()):
        return outcome.failed(
            "phone_swipe",
            "a swipe needs all four coordinates — 'x1', 'y1', 'x2', "
            "'y2' — as numbers")
    duration = _int_param(params, "duration_ms")
    duration = 300 if duration is None else max(50, min(2000, duration))
    args = dict(coords, duration_ms=duration)
    path = (f"({coords['x1']}, {coords['y1']}) → "
            f"({coords['x2']}, {coords['y2']})")

    def _do() -> str:
        data, err = _send("phone_swipe", "swipe", args)
        return err if err else f"Swiped {path} on the phone."

    return _run_control("phone_swipe", _do)


def phone_type(parameters: dict = None, player=None,
               session_memory=None) -> str:
    params = parameters or {}
    text = str(params.get("text") or "")
    if not text:
        return outcome.failed("phone_type", "no text was given to type")
    ref = str(params.get("ref") or "").strip()
    args: dict = {"text": text}
    if ref:
        args["ref"] = ref
    where = f"into element ref '{ref}'" if ref else "into the focused field"

    def _do() -> str:
        data, err = _send("phone_type", "type", args)
        return err if err else f"Typed {where} on the phone."

    return _run_control("phone_type", _do)


def phone_key(parameters: dict = None, player=None,
              session_memory=None) -> str:
    params = parameters or {}
    key = str(params.get("key") or "").strip().lower()
    if key not in _KEY_NAMES:
        return outcome.failed(
            "phone_key",
            f"'{key or '(none)'}' is not a key this tool can send — "
            f"use one of: {', '.join(_KEY_NAMES)}")

    def _do() -> str:
        data, err = _send("phone_key", "key", {"key": key})
        return err if err else f"Pressed {key} on the phone."

    return _run_control("phone_key", _do)
