"""
core/fleet.py — the Fleet view: what is actually running.

THE GOVERNING PRINCIPLE
    Alek asked (2026-10-22) for the old ARIA's Tasks view back:
    a surface where he can tell whether she is really working in
    the background — looking at code, running a delegated task —
    or only saying she is. So every row this module produces is
    rendered from runtime state: a registry in this process, a
    store file the owning module wrote, a live connection table.
    Nothing here is composed from anything the model said. If a
    piece of work is not in the data, it does not appear — and
    the view says that plainly in its empty state, because the
    empty state IS the feature: "Nothing running. If she says
    she's working in the background, it will appear here."

THE ACTIVITY REGISTRY
    Tool calls are the app's unit of work in flight, and the app
    has exactly one execution funnel — JarvisLive._execute_tool
    in main.py, which the Live session, the providers pipeline,
    routine replays and delegated tasks all pass through. The
    funnel registers each call here (begin) and closes it out
    (finish) from a finally, so the registry holds:
      * current() — the calls in flight right now, with elapsed;
      * recent()  — the newest finished calls (cap RECENT_CAP),
        each with its duration and whether it failed — so "did
        she actually open that file?" stays answerable after the
        fact, not only live.
    The registry is in-process and thread-safe (the funnel runs
    on the session loop; delegated workers reach it from their
    own threads), and its functions never raise: a status
    surface must never be able to break the work it watches.

THE SNAPSHOT
    snapshot() composes the whole picture from the owning
    modules, each source guarded on its own — one broken source
    costs its section, never the view:
      * delegated tasks — core/delegation's store records
        (goal, status, started/finished). Queued, running and
        blocked float to the top. The store does not persist a
        per-task turn count, so none is shown; elapsed is what
        the record actually holds.
      * in-flight + recent tool calls — the registry above.
        A shell call carries its command line as the detail, so
        "she's reading code" is visible as the actual command.
      * watches — actions/background_monitor's topics and
        core/screenwatch's watches.
      * the schedule — core/scheduler's rows: the next few,
        plus the total count.
      * routines — core/routines: how many are saved, and the
        live replay state (name + since) the module records.
      * MCP — core/mcp_client.status_rows(): the configured
        servers and their live connection state.
      * the loops — the code watcher's ledger
        (memory/codewatch.json) and the watchdog's
        (memory/watchdog.json restarts, plus its live
        heartbeats read in-process).

    running_count() is the header pill's number — delegated
    tasks in flight (queued / running / blocked) plus tool
    calls in flight — and snapshot() computes its own count
    from the same rows, so the pill and the tab cannot
    disagree. render_text() paints the Fleet tab from a
    snapshot; render_summary() is the prose twin update_status
    speaks. One snapshot, one story, every surface.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import deque
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


# Tests monkeypatch these constants to tmp paths (the stores' idiom).
CODEWATCH_PATH = _base_dir() / "memory" / "codewatch.json"
WATCHDOG_PATH = _base_dir() / "memory" / "watchdog.json"

RECENT_CAP = 30          # finished calls kept in the registry tail
DETAIL_LIMIT = 120       # a shell command's visible length
GOAL_LIMIT = 60          # a delegated goal's visible length

# Blocked tasks count as active here in the attention sense, not
# the worker sense: they hold no thread, but they are unfinished
# business Alek may need to see (Phase 2's blocked_model /
# blocked_input / blocked_unknown join the original gate block).
_ACTIVE_DELEGATED = ("queued", "running", "blocked",
                     "blocked_model", "blocked_input",
                     "blocked_unknown")


# ── The activity registry ────────────────────────────────────────────────

_lock = threading.Lock()
_inflight: dict[int, dict] = {}
_recent: deque = deque(maxlen=RECENT_CAP)
_next_token = 0


def begin(tool, detail: str = ""):
    """Register an in-flight tool call; returns an opaque token
    for finish(). Never raises — a token of None simply means
    the call goes unrecorded."""
    global _next_token
    try:
        with _lock:
            _next_token += 1
            token = _next_token
            _inflight[token] = {
                "tool": str(tool or "?"),
                "detail": str(detail or ""),
                "started": time.time(),
            }
        return token
    except Exception:
        return None


def finish(token, ok: bool = True) -> None:
    """Close out a call begun with begin(): it leaves the
    in-flight set and joins the recent tail with its duration.
    An unknown or None token is a no-op. Never raises."""
    try:
        if token is None:
            return
        with _lock:
            row = _inflight.pop(token, None)
            if row is None:
                return
            now = time.time()
            _recent.append({
                "tool": row["tool"],
                "detail": row["detail"],
                "started": row["started"],
                "finished": now,
                "duration": max(0.0, now - row["started"]),
                "ok": bool(ok),
            })
    except Exception:
        pass


def current(now: float = None) -> list[dict]:
    """The calls in flight right now, oldest first, each with
    its elapsed seconds. Never raises."""
    now = time.time() if now is None else now
    try:
        with _lock:
            rows = [dict(r) for r in _inflight.values()]
    except Exception:
        return []
    for r in rows:
        r["elapsed"] = max(0.0, now - r["started"])
    rows.sort(key=lambda r: r["started"])
    return rows


def recent(n: int = None) -> list[dict]:
    """The finished tail, newest first. Never raises."""
    try:
        with _lock:
            rows = [dict(r) for r in _recent]
    except Exception:
        return []
    rows.reverse()
    if n is not None:
        rows = rows[: max(0, int(n))]
    return rows


def _inflight_count() -> int:
    try:
        with _lock:
            return len(_inflight)
    except Exception:
        return 0


def reset_for_tests() -> None:
    global _next_token
    with _lock:
        _inflight.clear()
        _recent.clear()
        _next_token = 0


# ── What the funnel records ──────────────────────────────────────────────

def detail_for(name, args) -> str:
    """The human-meaningful detail of a call — what makes a
    registry row evidence rather than a label. Only the shell
    tool has a natural one-liner: the command itself, which is
    exactly the "is she really looking at code?" case. Other
    tools record no detail rather than an invented summary."""
    try:
        if name == "run_shell_command":
            cmd = str((args or {}).get("command") or "").strip()
            return cmd[:DETAIL_LIMIT]
    except Exception:
        pass
    return ""


def result_ok(result) -> bool:
    """Did a finished call fail? Read off the result text the
    funnel itself produced: the outcome vocabulary's failure
    line (status=failed), the funnel's own crash line
    ("Tool 'x' failed: ..."), or an unknown tool. Anything
    else — including unverified — is not a failure."""
    try:
        text = str(result or "")
    except Exception:
        return True
    if text.startswith("status=failed"):
        return False
    if text.startswith("Unknown tool:"):
        return False
    if text.startswith("Tool '") and " failed: " in text[:120]:
        return False
    return True


# ── Sources (each guarded: a broken source costs its section) ────────────

def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _delegated_rows(now: float) -> list[dict]:
    """Delegation's records, normalised: active (queued /
    running / blocked) first, then newest first within each
    group. Elapsed is now-minus-start for in-flight tasks
    (created when never started), duration for finished ones."""
    try:
        from core import delegation
        raw = delegation.list_tasks(limit=50)
    except Exception:
        return []
    rows = []
    for t in raw or []:
        try:
            status = str(t.get("status") or "?")
            started = t.get("started") or t.get("created")
            finished = t.get("finished")
            if status in _ACTIVE_DELEGATED:
                base = started or t.get("created") or now
                elapsed = max(0.0, now - float(base))
            elif finished and started:
                elapsed = max(0.0, float(finished) - float(started))
            else:
                elapsed = 0.0
            verification = t.get("verification")
            rows.append({
                "id": t.get("id"),
                "goal": str(t.get("prompt") or ""),
                "status": status,
                "active": status in _ACTIVE_DELEGATED,
                "created": t.get("created"),
                "started": t.get("started"),
                "finished": finished,
                "elapsed": elapsed,
                # Phase 2 engine state, as the record carries it
                # (legacy records simply leave these empty).
                "progress": delegation._progress_text(t),
                "blocked_reason":
                    str(t.get("blocked_reason") or "")[:120],
                "verify_status":
                    str(verification.get("status") or "")
                    if isinstance(verification, dict) else "",
                "checkpoint_at": t.get("checkpoint_at"),
            })
        except Exception:
            continue
    rows.sort(key=lambda r: (0 if r["active"] else 1,
                             -(r["id"] or 0)))
    return rows


def _monitor_topics() -> list[str]:
    try:
        from actions import background_monitor
        return [str(t) for t in background_monitor.list_monitors() or []]
    except Exception:
        return []


def _screen_watches() -> list[dict]:
    try:
        from core import screenwatch
        watches = screenwatch.list_watches() or {}
        return [{"name": name,
                 "interval_s": w.get("interval_s"),
                 "last_checked": w.get("last_checked"),
                 "question": str(w.get("question") or "")}
                for name, w in watches.items()]
    except Exception:
        return []


def _schedule() -> dict:
    try:
        from core import scheduler
        rows = scheduler.list_rows() or []
        return {"total": len(rows),
                "next": [{"id": t.get("id"),
                          "kind": str(t.get("kind") or ""),
                          "prompt": str(t.get("prompt") or ""),
                          "next_run": t.get("next_run")}
                         for t in rows[:5]]}
    except Exception:
        return {"total": 0, "next": []}


def _routines() -> dict:
    out = {"saved": 0, "replaying": None, "recording": False}
    try:
        from core import routines
        out["saved"] = len(routines.routine_names() or [])
        state = routines.replay_state() or {}
        if state.get("replaying"):
            out["replaying"] = {"name": state.get("name"),
                                "since": state.get("since")}
        out["recording"] = bool(routines.is_recording())
    except Exception:
        pass
    return out


def _mcp() -> dict:
    out = {"connected": [], "not_connected": [], "failed": []}
    try:
        from core import mcp_client
        for row in mcp_client.status_rows() or []:
            state = row.get("state")
            if state == "connected":
                out["connected"].append(
                    {"name": row.get("name"),
                     "tools": int(row.get("tools") or 0)})
            elif state == "failed":
                out["failed"].append(
                    {"name": row.get("name"),
                     "error": str(row.get("error") or "")})
            else:
                out["not_connected"].append(row.get("name"))
    except Exception:
        pass
    return out


def _codewatch() -> dict | None:
    ledger = _read_json(CODEWATCH_PATH)
    if not ledger:
        return None
    boot = ledger.get("boot") or {}
    events = ledger.get("events") or []
    return {
        "boot_head": boot.get("head"),
        "checks": ledger.get("checks", 0),
        "last_check_ts": ledger.get("last_check_ts"),
        "restart_pending": bool(ledger.get("restart_pending")),
        "last_event": ({"kind": events[-1].get("kind", "?"),
                        "detail": events[-1].get("detail", ""),
                        "ts": events[-1].get("ts")}
                       if events else None),
    }


def _watchdog(now: float) -> dict:
    data = _read_json(WATCHDOG_PATH) or {}
    stamps = [float(t) for t in (data.get("restarts") or [])
              if isinstance(t, (int, float))]
    out = {
        "restarts_last_hour": sum(1 for t in stamps
                                  if now - t < 3600.0),
        "last_restart_ts": max(stamps) if stamps else None,
        "beats": {},
        "enabled": None,
    }
    try:
        from core import watchdog
        mono = time.monotonic()
        for source in watchdog.SOURCES:
            beat = watchdog.last_beat(source)
            if beat is not None:
                out["beats"][source] = max(0.0, mono - beat)
    except Exception:
        pass
    try:
        from memory.config_manager import get_watchdog_enabled
        out["enabled"] = bool(get_watchdog_enabled())
    except Exception:
        pass
    return out


# ── The snapshot ─────────────────────────────────────────────────────────

def snapshot(now: float = None) -> dict:
    """The whole fleet picture, from the runtime sources.
    Never raises: any source that fails contributes its empty
    shape, so the view degrades section by section."""
    now = time.time() if now is None else now
    delegated = _delegated_rows(now)
    inflight = current(now)
    active = sum(1 for r in delegated if r["active"])
    return {
        "now": now,
        "running_count": active + len(inflight),
        "delegated": delegated,
        "delegated_active": active,
        "inflight": inflight,
        "shell": [r for r in inflight
                  if r["tool"] == "run_shell_command"],
        "recent": recent(),
        "monitors": _monitor_topics(),
        "screen_watches": _screen_watches(),
        "schedule": _schedule(),
        "routines": _routines(),
        "mcp": _mcp(),
        "codewatch": _codewatch(),
        "watchdog": _watchdog(now),
    }


def running_count(now: float = None) -> int:
    """The header pill's number, from the same two sources the
    snapshot counts — delegated tasks in flight plus tool
    calls in flight — so pill and tab cannot disagree."""
    now = time.time() if now is None else now
    active = sum(1 for r in _delegated_rows(now) if r["active"])
    return active + _inflight_count()


# ── Rendering ────────────────────────────────────────────────────────────

def _fmt_elapsed(secs) -> str:
    secs = max(0, int(secs or 0))
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60:02d}s"
    return f"{secs // 3600}h {(secs % 3600) // 60:02d}m"


def _fmt_duration(secs) -> str:
    secs = max(0.0, float(secs or 0))
    if secs < 1.0:
        return f"{secs:.1f}s"
    return _fmt_elapsed(secs)


def _ago(ts, now: float) -> str:
    if not ts:
        return "never"
    secs = max(0, int(now - float(ts)))
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m ago"
    if secs < 86400:
        return f"{secs // 3600}h ago"
    return f"{secs // 86400}d ago"


def _short(sha) -> str:
    return str(sha)[:12] if sha else "unknown"


def _fmt_clock(epoch) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S",
                             time.localtime(float(epoch)))
    except Exception:
        return str(epoch)


def render_text(snap: dict = None) -> str:
    """The Fleet tab, painted from a snapshot (a fresh one when
    none is passed — the tab's provider). Plain lines in the
    content panel's own idiom; sections in snapshot order."""
    snap = snapshot() if snap is None else snap
    now = snap["now"]
    lines: list[str] = []

    # ── RUNNING ──────────────────────────────────────────────
    lines.append("RUNNING")
    active = [r for r in snap["delegated"] if r["active"]]
    finished = [r for r in snap["delegated"] if not r["active"]]
    if not active and not snap["inflight"]:
        lines.append("  Nothing running. If she says she's "
                     "working in the background, it will "
                     "appear here.")
    for r in active:
        line = (f"  #{r['id']} [{r['status']}] "
                f"{r['goal'][:GOAL_LIMIT]}"
                f" — {_fmt_elapsed(r['elapsed'])}")
        if r.get("progress"):
            line += f" — {r['progress']}"
        if r.get("blocked_reason"):
            line += f" — blocked: {r['blocked_reason']}"
        lines.append(line)
    for r in snap["inflight"]:
        detail = f" — {r['detail']}" if r["detail"] else ""
        lines.append(f"  {r['tool']}{detail}"
                     f" — {_fmt_elapsed(r['elapsed'])}")
    if finished:
        lines.append("")
        lines.append("  Recent background tasks:")
        for r in finished[:3]:
            line = (f"  #{r['id']} [{r['status']}] "
                    f"{r['goal'][:GOAL_LIMIT]}"
                    f" — {_fmt_duration(r['elapsed'])}")
            if r.get("verify_status"):
                line += f" — verified: {r['verify_status']}"
            lines.append(line)

    # ── SHELL ────────────────────────────────────────────────
    if snap["shell"]:
        lines.append("")
        lines.append("SHELL")
        for r in snap["shell"]:
            lines.append(f"  {r['detail']}"
                         f" — {_fmt_elapsed(r['elapsed'])}")

    # ── WATCHES & SCHEDULE ───────────────────────────────────
    lines.append("")
    lines.append("WATCHES & SCHEDULE")
    if snap["monitors"]:
        lines.append("  Monitoring: " + ", ".join(snap["monitors"]))
    else:
        lines.append("  Monitoring: nothing")
    if snap["screen_watches"]:
        for w in snap["screen_watches"]:
            last = w["last_checked"] or "never checked"
            lines.append(f"  Screen watch '{w['name']}' — every "
                         f"{w['interval_s']}s, last checked {last}")
    else:
        lines.append("  Screen watches: none")
    sched = snap["schedule"]
    if sched["total"]:
        lines.append(f"  Scheduled: {sched['total']} item"
                     + ("s" if sched["total"] != 1 else ""))
        for t in sched["next"]:
            lines.append(f"    #{t['id']} [{t['kind']}] "
                         f"{t['prompt'][:48]}"
                         f" — next {_fmt_clock(t['next_run'])}")
    else:
        lines.append("  Scheduled: nothing")

    # ── SYSTEM ───────────────────────────────────────────────
    lines.append("")
    lines.append("SYSTEM")
    cw = snap["codewatch"]
    if cw is None:
        lines.append("  Code watcher: no record yet")
    else:
        line = (f"  Code watcher: booted {_short(cw['boot_head'])}, "
                f"{cw['checks']} checks, last "
                f"{_ago(cw['last_check_ts'], now)}")
        if cw["restart_pending"]:
            line += " — restart armed"
        lines.append(line)
    wd = snap["watchdog"]
    wd_line = (f"  Watchdog: {wd['restarts_last_hour']} restart"
               + ("s" if wd["restarts_last_hour"] != 1 else "")
               + " in the last hour")
    if wd["last_restart_ts"]:
        wd_line += f", last {_ago(wd['last_restart_ts'], now)}"
    if wd["beats"]:
        beats = ", ".join(f"{k} {_fmt_elapsed(v)}"
                          for k, v in sorted(wd["beats"].items()))
        wd_line += f" — heartbeats: {beats}"
    if wd["enabled"] is False:
        wd_line += " (disabled)"
    lines.append(wd_line)
    mcp = snap["mcp"]
    if mcp["connected"]:
        for c in mcp["connected"]:
            lines.append(f"  MCP: '{c['name']}' connected "
                         f"({c['tools']} tools)")
    if mcp["failed"]:
        for f in mcp["failed"]:
            lines.append(f"  MCP: '{f['name']}' failed"
                         + (f" — {f['error']}" if f["error"] else ""))
    if mcp["not_connected"]:
        lines.append("  MCP: configured, not connected: "
                     + ", ".join(str(n) for n in mcp["not_connected"]))
    if not (mcp["connected"] or mcp["failed"] or mcp["not_connected"]):
        lines.append("  MCP: no servers configured")
    rt = snap["routines"]
    rt_line = (f"  Routines: {rt['saved']} saved"
               + ("s" if rt["saved"] != 1 else ""))
    if rt["replaying"]:
        since = rt["replaying"].get("since")
        rt_line += (f" — replaying '{rt['replaying'].get('name')}'"
                    + (f" for {_fmt_elapsed(now - since)}"
                       if since else ""))
    if rt["recording"]:
        rt_line += " — recording a new routine"
    lines.append(rt_line)

    # ── RECENT ───────────────────────────────────────────────
    lines.append("")
    lines.append("RECENT")
    if snap["recent"]:
        for r in snap["recent"][:12]:
            detail = f" — {r['detail']}" if r["detail"] else ""
            mark = "✓" if r["ok"] else "✗ failed"
            lines.append(f"  {r['tool']}{detail}"
                         f" — {_fmt_duration(r['duration'])} {mark}")
    else:
        lines.append("  No tool calls recorded yet this session.")
    return "\n".join(lines)


def render_summary(snap: dict = None) -> str:
    """The spoken twin (update_status): the same snapshot as
    prose — what is running, in the registry's words, plus the
    most recent finished call and the next scheduled item."""
    snap = snapshot() if snap is None else snap
    n = snap["running_count"]
    if n == 0:
        lines = ["Fleet: nothing is running right now — no "
                 "delegated tasks, no tool calls in flight. "
                 "Background work appears in the Fleet tab the "
                 "moment it is registered."]
    else:
        parts = []
        for r in snap["delegated"]:
            if r["active"]:
                parts.append(
                    f"delegated task #{r['id']} "
                    f"({r['goal'][:GOAL_LIMIT]}, {r['status']}, "
                    f"{_fmt_elapsed(r['elapsed'])})")
        for r in snap["inflight"]:
            detail = f" ({r['detail']})" if r["detail"] else ""
            parts.append(f"{r['tool']}{detail}, "
                         f"{_fmt_elapsed(r['elapsed'])} in")
        lines = [f"Fleet: {n} running — " + "; ".join(parts) + "."]
    if snap["recent"]:
        r = snap["recent"][0]
        detail = f" ({r['detail']})" if r["detail"] else ""
        lines.append(
            f"Most recent finished call: {r['tool']}{detail} — "
            f"{_fmt_duration(r['duration'])}, "
            f"{'ok' if r['ok'] else 'failed'}.")
    sched = snap["schedule"]
    if sched["total"] and sched["next"]:
        t = sched["next"][0]
        lines.append(f"Next scheduled item: "
                     f"[{t['kind']}] {t['prompt'][:48]} at "
                     f"{_fmt_clock(t['next_run'])} "
                     f"({sched['total']} scheduled in all).")
    return "\n".join(lines)
