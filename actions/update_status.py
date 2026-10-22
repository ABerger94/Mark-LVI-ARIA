"""
actions/update_status.py — what the self-update sidecar knows.

READ-ONLY, BY DESIGN
    The updater (updater.py) is a sidecar process that pulls new
    code and restarts her on it: announce-first, clean-tree gate,
    smoke-import, rollback to the last known-good commit. In v1 the
    sidecar and Alek's word own updating — there is deliberately NO
    model tool that triggers one — but she can be ASKED about it,
    and this is the asking. Everything reported here comes from the
    sidecar's record (memory/updater_state.json), the boot stamp
    (memory/boot_stamp.json) and one read-only `git rev-parse`;
    this tool changes nothing, fetches nothing, starts nothing.

    Delegated tasks never see it (main.py's _DELEGATED_EXCLUDE):
    a background task has no business reporting on — or being
    mistaken about — the update state of the app it runs inside.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from core import outcome
from memory.config_manager import get_auto_update_enabled


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


def render_status(state, stamp, head, enabled: bool) -> str:
    """The prose half of the answer, from plain data — kept pure
    so the tests can drive it without a repo."""
    lines = []
    lines.append(
        "Self-updates are " + ("on." if enabled else
        "OFF — the sidecar idles and nothing is pulled."))
    if head:
        lines.append(f"Code on disk: {_short(head)}.")
    if stamp and stamp.get("head"):
        lines.append(f"Code she booted with: "
                     f"{_short(stamp['head'])}"
                     + (" — the disk is ahead of her running code."
                        if head and stamp["head"] != head else "."))
    if not state:
        lines.append("The updater sidecar has left no record yet — "
                     "it either hasn't run or found nothing to do.")
        return "\n".join(lines)
    remote = state.get("last_seen_remote")
    if remote:
        lines.append(f"GitHub main, last seen: {_short(remote)}"
                     + (" — an update is waiting to be applied."
                        if head and remote != head else "."))
    lkg = (state.get("last_known_good") or {}).get("commit")
    if lkg:
        lines.append(f"Last known-good version (the rollback "
                     f"target): {_short(lkg)}.")
    pending = state.get("pending")
    if pending:
        lines.append(f"An update is in flight: "
                     f"{_short(pending.get('from'))} → "
                     f"{_short(pending.get('to'))} "
                     f"({pending.get('kind', 'remote')}).")
    history = state.get("history") or []
    if history:
        last = history[-1]
        lines.append(f"Most recent updater event: "
                     f"{last.get('event', '?')}"
                     + (f" — {last['detail']}" if last.get("detail")
                        else "") + ".")
    return "\n".join(lines)


def update_status(parameters: dict = None, player=None,
                  session_memory=None) -> str:
    root = get_base_dir()
    enabled = get_auto_update_enabled()
    head = _git_head(root)
    state = _read_json(root / "memory" / "updater_state.json")
    stamp = _read_json(root / "memory" / "boot_stamp.json")
    facts = {
        "enabled": enabled,
        "head": _short(head),
    }
    if state and state.get("last_seen_remote"):
        facts["remote"] = _short(state["last_seen_remote"])
        facts["update_waiting"] = bool(head) \
            and state["last_seen_remote"] != head
    lkg = (state or {}).get("last_known_good") or {}
    if lkg.get("commit"):
        facts["last_known_good"] = _short(lkg["commit"])
    return outcome.done("update_status", **facts) + "\n" + \
        render_status(state, stamp, head, enabled)


# ── Tool declaration (auto-discovered by core/action_loader.py) ──────────────
TOOL = {
    "name": "update_status",
    "description": (
        "Report the self-update sidecar's state: whether automatic "
        "updates are on, the code version on disk, the version she "
        "booted with, the latest version seen on GitHub, the "
        "last known-good version an update would roll back to, "
        "whether an update is waiting or in flight, and the most "
        "recent thing the updater did. Read-only — it cannot start, "
        "stop or trigger an update."
    ),
    "brief": "Report the self-updater's state (read-only).",
    "tier": "extra",
    "parameters": {
        "type": "OBJECT",
        "properties": {},
        "required": [],
    },
    "handler": update_status,
}
