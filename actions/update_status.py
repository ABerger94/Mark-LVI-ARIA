"""
actions/update_status.py — what the code watcher knows.

READ-ONLY, BY DESIGN
    Her code is watched in-process now (core/codewatch.py — the
    old ARIA's design, ported 2026-10-22 when the updater sidecar
    was retired): she snapshots her own source tree at boot and
    re-checks it after every turn; a change is validated, broken
    files are restored from git, and a good change is loaded by a
    restart she announces and performs herself. There is
    deliberately NO model tool that triggers a restart — but she
    can be ASKED about the watcher's state, and this is the
    asking. Everything reported here comes from the watcher's
    ledger (memory/codewatch.json) and one read-only
    `git rev-parse`; this tool changes nothing, fetches nothing,
    starts nothing.

    Delegated tasks never see it (main.py's _DELEGATED_EXCLUDE):
    a background task has no business reporting on — or being
    mistaken about — the code state of the app it runs inside.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

from core import outcome


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


def _read_json(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _git_head(root: Path):
    try:
        r = subprocess.run(["git", "-C", str(root), "rev-parse",
                            "HEAD"],
                           capture_output=True, text=True, timeout=15)
        sha = (r.stdout or "").strip()
        if r.returncode == 0 and sha:
            return sha
    except Exception:
        pass
    return None


def _short(sha) -> str:
    return sha[:12] if sha else "unknown"


def _ago(ts, now: float) -> str:
    """A ledger timestamp as a rough age — the ledger's times are
    evidence, not a stopwatch, so whole units are honest."""
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


def render_status(ledger, head, now: float = None) -> str:
    """The prose half of the answer, from plain data — kept pure
    so the tests can drive it without a repo."""
    now = time.time() if now is None else now
    lines = []
    lines.append(
        "Code watching is in-process: I check my own source after "
        "every turn and restart myself on a change — there is no "
        "sidecar and nothing polls in the background.")
    if head:
        lines.append(f"Code on disk: {_short(head)}.")
    if not ledger:
        lines.append("The code watcher has left no record yet — "
                     "it writes its ledger at startup.")
        return "\n".join(lines)
    boot = ledger.get("boot") or {}
    if boot.get("head"):
        lines.append(f"Code she booted with: {_short(boot['head'])}"
                     + (" — the disk is ahead of her running code; "
                        "the change is picked up at the next turn's "
                        "check."
                        if head and boot["head"] != head else "."))
    if boot.get("watched"):
        lines.append(f"Files watched: {boot['watched']}.")
    lines.append(f"Turn-end checks so far: {ledger.get('checks', 0)}"
                 f" (last {_ago(ledger.get('last_check_ts'), now)}).")
    if ledger.get("restart_pending"):
        lines.append("A restart is armed — she is waiting for "
                     "speech to finish before relaunching."
                     + (f" (Armed by {ledger['armed_by']}.)"
                        if ledger.get("armed_by") else ""))
    events = ledger.get("events") or []
    if events:
        last = events[-1]
        lines.append(f"Most recent watcher event: "
                     f"{last.get('kind', '?')}"
                     + (f" — {last['detail']}"
                        if last.get("detail") else "")
                     + f" ({_ago(last.get('ts'), now)}).")
    return "\n".join(lines)


def update_status(parameters: dict = None, player=None,
                  session_memory=None) -> str:
    root = get_base_dir()
    head = _git_head(root)
    ledger = _read_json(root / "memory" / "codewatch.json")
    facts = {
        "watching": bool(ledger),
        "head": _short(head),
    }
    if ledger:
        boot = ledger.get("boot") or {}
        if boot.get("head"):
            facts["booted_with"] = _short(boot["head"])
            facts["disk_ahead_of_boot"] = bool(head) \
                and boot["head"] != head
        if boot.get("watched"):
            facts["watched_files"] = boot["watched"]
        facts["checks"] = ledger.get("checks", 0)
        facts["restart_pending"] = bool(ledger.get("restart_pending"))
        events = ledger.get("events") or []
        if events:
            facts["last_event"] = events[-1].get("kind", "?")
    return outcome.done("update_status", **facts) + "\n" + \
        render_status(ledger, head)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "update_status",
    "description": (
        "Report the in-process code watcher's state: the code "
        "version on disk, the version she booted with, how many "
        "files are watched, how many turn-end checks have run, "
        "whether a restart is armed, and the most recent watcher "
        "event (change, restore, restart). Read-only — it cannot "
        "start, stop or trigger a restart."
    ),
    "brief": "Report the code watcher's state (read-only).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": update_status,
}
