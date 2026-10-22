"""
core/selfheal.py — the bounded repair path behind diagnostics.

WHY THIS EXISTS, AND WHY IT IS A SEPARATE MODULE
    core/diagnostics.py is read-only by design, and its docstring
    says why: a probe that quietly fixes what it finds destroys the
    evidence that anything was ever wrong. But a report that ends
    at "the config file does not parse" leaves the user holding a
    sentence when the remedy is already designed, tested and
    sitting in the loaders. So the repair lives HERE, as its own
    act with its own name: diagnostics finds, self-heal fixes —
    separately invoked, separately recorded, so the evidence of
    what was broken survives in the incident log even after the
    fix.

THE BOUND — heal only what has a designed, reversible remedy:

  * Configuration does not parse → memory.config_manager's own
    recovery (load_api_keys): restore from the newest readable
    backup, or keep the wreck aside and reset. The healed state is
    verified by re-parsing the file afterwards — a heal is claimed
    from the re-read, never from the call returning.
  * Memory store does not parse → memory.memory_manager's own
    recovery (load_memory), the same rule, verified the same way.
  * Command registry rejections → REPORT ONLY. A file that failed
    to load is code a human wrote or installed; deleting or moving
    it from a voice command would be destruction wearing a repair's
    clothes. The report names the files; a human decides.
  * Everything else diagnostics checks (COM audio, the dashboard's
    certificate, Spotify's parser, engine readiness) → REPORT
    ONLY. There is no designed remedy for them in this app, and
    inventing one at heal time is how assistants break machines
    confidently.

    One wrinkle, stated plainly: the diagnostics pass itself
    already performs the config recovery when it finds the file
    unparseable (its one sanctioned exception — the recovery
    happens witnessed rather than at some later silent load). So a
    heal that follows a diagnostics pass will often find the config
    already readable. That is reported as what it is — "readable
    now; no heal action was needed" — never re-claimed as this
    module's work.

WHY NO CONFIRMATION GATE
    The gate (core/confirm.py) exists for irreversible acts the
    model could talk the app into. Self-heal's only acts are the
    loaders' designed recoveries of files that are ALREADY broken —
    the same recoveries an ordinary load performs unbidden every
    day, whose "damage" is bounded by the backups the recovery
    restores FROM. Gating them would not add safety; it would make
    recovery wait for a banner while the app runs on a broken
    store. Rejected files — the one class where a destructive
    "remedy" is imaginable — are never touched at all.

RECORD
    Every heal action is recorded as an incident (kind
    "self_heal_restored") in addition to the loader's own recovery
    incident, and the delivery states what was broken, what was
    restored and what was left for a human. Report-only findings
    change nothing and record nothing beyond the diagnostics
    pass's own failure incident.
"""

from __future__ import annotations

HEALED = "healed"            # a recovery ran and the re-read verifies it
RECOVERED = "recovered"      # broken per diagnostics, readable now — an
                             # earlier recovery (usually the diagnostics
                             # pass's own) had already done the work
REPORT_ONLY = "report-only"  # a real finding with no designed remedy
CLEAN = "clean"              # nothing was wrong with this check

# Check names, as diagnostics.CHECKS names them.
_CONFIG_CHECK = "Configuration"
_MEMORY_CHECK = "Memory store"
_REGISTRY_CHECK = "Command registry"


# ── Seams (tests replace these; production code never does) ────────────────

def _rejected_files() -> list[str]:
    """The registry walk's rejected tool files, by name — the same
    construction diagnostics' probe 6 performs."""
    from core import diagnostics
    _rows, rejected = diagnostics._command_rows()
    return [f for f, _err in rejected]


def _config_parses() -> bool:
    from memory import config_manager as cm
    if not cm.CONFIG_FILE.exists():
        return True   # a missing file is a fresh install, not damage
    try:
        return cm._parse_config(
            cm.CONFIG_FILE.read_text(encoding="utf-8")) is not None
    except Exception:
        return False


def _memory_parses() -> bool:
    from memory import memory_manager as mm
    if not mm.MEMORY_PATH.exists():
        return True
    try:
        return mm._parse_store(
            mm.MEMORY_PATH.read_text(encoding="utf-8")) is not None
    except Exception:
        return False


# ── The heal ───────────────────────────────────────────────────────────────

def _heal_store(check: str, parses, load, pop_note, label: str) -> dict:
    """One store's heal. `parses()` is checked first: readable now
    means an earlier recovery already ran (reported, not claimed).
    Unreadable means run the loader — its designed recovery — and
    claim the heal only from the re-read."""
    if parses():
        return {"check": check, "action": RECOVERED,
                "detail": (f"{label} is readable now — an earlier "
                           f"recovery already restored it; no heal "
                           f"action was needed")}
    load()
    note = ""
    try:
        note = pop_note() or ""
    except Exception:
        note = ""
    if parses():
        try:
            from core import incidents
            incidents.record("self_heal", "self_heal_restored",
                             f"{label}: loader recovery run by "
                             f"self-heal; verified readable on re-read")
        except Exception:
            pass
        detail = f"{label} restored via its loader's recovery"
        if note:
            detail += f" — {note}"
        return {"check": check, "action": HEALED, "detail": detail}
    return {"check": check, "action": REPORT_ONLY,
            "detail": (f"{label} still does not parse after its "
                       f"loader's recovery ran — the file needs a "
                       f"human, not another heal")}


def heal(results: list[dict] | None = None) -> list[dict]:
    """Heal what a diagnostics pass found. With no results, a fresh
    pass runs first (the tool's standalone shape). Returns one entry
    per FAILED check: {check, action, detail}, action one of HEALED
    / RECOVERED / REPORT_ONLY. Passing and skipped checks produce
    no entries — there is nothing to say about them."""
    if results is None:
        from core import diagnostics
        results = diagnostics.run_all()
    entries: list[dict] = []
    for r in results:
        if r.get("status") != "fail":
            continue
        name = r.get("name", "")
        if name == _CONFIG_CHECK:
            from memory import config_manager as cm
            entries.append(_heal_store(
                name, _config_parses, cm.load_api_keys,
                cm.pop_recovery_note, "api_keys.json"))
        elif name == _MEMORY_CHECK:
            from memory import memory_manager as mm
            entries.append(_heal_store(
                name, _memory_parses, mm.load_memory,
                mm.pop_recovery_note, "long_term.json"))
        elif name == _REGISTRY_CHECK:
            try:
                files = _rejected_files()
            except Exception:
                files = []
            named = (", ".join(files) if files
                     else "the file(s) named in the diagnostics report")
            entries.append({
                "check": name, "action": REPORT_ONLY,
                "detail": (f"tool file(s) failed to load: {named}. "
                           f"Self-heal does not delete or move code — "
                           f"a human decides what happens to them")})
        else:
            entries.append({
                "check": name, "action": REPORT_ONLY,
                "detail": (f"no designed remedy — reported, not "
                           f"touched ({r.get('detail', '')})")})
    return entries


# ── Rendering + delivery (diagnostics.deliver's shape) ─────────────────────

_MARKS = {HEALED: "✓", RECOVERED: "✓", REPORT_ONLY: "•"}


def render_report(entries: list[dict]) -> str:
    if not entries:
        return "Nothing to heal — every check passed, or found " \
               "nothing with a remedy."
    lines = [f"{_MARKS[e['action']]} {e['check']} — {e['detail']}"
             for e in entries]
    healed = sum(1 for e in entries if e["action"] == HEALED)
    left = sum(1 for e in entries if e["action"] == REPORT_ONLY)
    tally = []
    if healed:
        tally.append(f"{healed} healed")
    if left:
        tally.append(f"{left} left for a human")
    lines.append((", ".join(tally) or "no action taken") + ".")
    return "\n".join(lines)


def spoken_summary(entries: list[dict]) -> str:
    """The one sentence (or two) Alek hears: what was restored, and
    the NAMES of anything left for him — never the detail."""
    if not entries:
        return "Self-heal: nothing to fix."
    healed = [e["check"] for e in entries if e["action"] == HEALED]
    left = [e["check"] for e in entries if e["action"] == REPORT_ONLY]
    parts = []
    if healed:
        parts.append(f"restored {', '.join(healed)}")
    if left:
        parts.append(f"{', '.join(left)} need"
                     f"{'s' if len(left) == 1 else ''} a human look")
    if not parts:
        parts.append("everything broken was already recovered")
    return "Self-heal: " + "; ".join(parts) + "."


def deliver(entries: list[dict], show_content=None, write_log=None,
            announce=None) -> None:
    """The diagnostics delivery shape, for the heal report: the
    lines to the content panel (title SELF-HEAL) and the activity
    log, the spoken summary through the caller's channel. Every
    sink optional and guarded, as in diagnostics.deliver."""
    report = render_report(entries)
    if show_content is not None:
        try:
            show_content("SELF-HEAL", report)
        except Exception:
            pass
    if write_log is not None:
        try:
            for line in report.splitlines():
                write_log(f"[SelfHeal] {line}")
        except Exception:
            pass
    if announce is not None:
        try:
            announce(spoken_summary(entries))
        except Exception:
            pass
