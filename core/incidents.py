"""
core/incidents.py — the durable record of what went wrong.

WHY THIS EXISTS
    This app recovers well and remembers none of it. A tool fails, the model
    is told, the user hears a sentence — and the evidence is a print() that
    scrolls off a console, or nothing at all under pythonw.exe. When the same
    failure comes back at 2 AM there is no way to answer the only questions
    that matter: when did this start, how often, and what exactly did it say?

    ARIA (the assistant this build succeeds) kept a classified incident log
    for exactly that reason, and it is the part of her failure machinery
    worth porting whole: not the recovery — this app's recovery is better —
    the RECORD.

WHAT IT IS
    One JSON object per line in memory/incidents.jsonl:

        {"ts": "...", "source": "tool:open_app", "kind": "tool_failed",
         "message": "status=failed action=open_app reason=..."}

    `kind` is the classification: tool_failed / tool_unverified for tool
    outcomes (recorded where outcomes are produced, in core/outcome.py),
    an exception's class name for crashes, or whatever short label a caller
    passes to record().

THE TWO RULES
    1. NEVER RAISE. This module sits inside error paths — core/outcome.py's
       whole design rule is that the error path must not crash — so every
       public function swallows its own failures whole. A full disk or a
       locked file costs a record, never an assistant.
    2. STAY SMALL. The file is capped: past MAX_BYTES it is rewritten
       keeping the newest KEEP_LINES records. A log that grows forever is a
       disk problem wearing a diagnostic's clothes, and the recent failures
       are the ones anybody reads.
"""
from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path


def _base_dir() -> Path:
    # Same rule as memory/memory_manager.get_base_dir: beside the executable
    # when frozen, the repo root when running from source.
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


INCIDENTS_PATH = _base_dir() / "memory" / "incidents.jsonl"

MAX_BYTES = 512 * 1024   # rewrite threshold — see rule 2 above
KEEP_LINES = 1000        # records kept across a rewrite
_MAX_MESSAGE = 500       # one record is a headline, not a transcript

_lock = threading.Lock()


def _line(source: str, kind: str, message: str) -> str:
    rec = {
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "source": str(source or "unknown")[:80],
        "kind": str(kind or "failure")[:40],
        "message": str(message or "")[:_MAX_MESSAGE],
    }
    return json.dumps(rec, ensure_ascii=False)


def _append(line: str) -> None:
    with _lock:
        INCIDENTS_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(INCIDENTS_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        try:
            if INCIDENTS_PATH.stat().st_size > MAX_BYTES:
                lines = INCIDENTS_PATH.read_text(
                    encoding="utf-8", errors="replace").splitlines()
                INCIDENTS_PATH.write_text(
                    "\n".join(lines[-KEEP_LINES:]) + "\n", encoding="utf-8")
        except Exception:
            pass   # a failed rotation loses nothing but the rotation


def record(source: str, kind: str, message: str) -> None:
    """Append one classified failure record. Never raises — see rule 1."""
    try:
        _append(_line(source, kind, message))
    except Exception:
        pass


def record_outcome(status: str, action: str, rendered: str) -> None:
    """Record a tool outcome, called by core/outcome.py as outcomes are made.

    Only the two problem statuses are recorded: `failed` (it did not happen)
    and `unverified` (it was asked and could not be confirmed). `done` is the
    ordinary course of a day and would drown the file; `ask` is a question,
    not a failure.
    """
    kind = {"failed": "tool_failed", "unverified": "tool_unverified"}.get(status)
    if kind is None:
        return
    record(f"tool:{action}", kind, rendered)


def record_exception(source: str, exc: BaseException) -> None:
    """Record a caught exception, classified by its class name."""
    record(source, type(exc).__name__, f"{type(exc).__name__}: {exc}")


def recent(n: int = 50) -> list[dict]:
    """The newest `n` records, oldest first — for a future panel or a human
    reading the file's tail without loading all of it."""
    try:
        lines = INCIDENTS_PATH.read_text(
            encoding="utf-8", errors="replace").splitlines()
        out = []
        for ln in lines[-max(1, n):]:
            try:
                out.append(json.loads(ln))
            except Exception:
                continue
        return out
    except Exception:
        return []
