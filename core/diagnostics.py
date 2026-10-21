"""
core/diagnostics.py — the self-test she can run on her own machine.

WHY THIS EXISTS
    This app's worst failures have been invisible ones: a COM stack
    dying inside a helper whose stderr nobody read, a config file
    silently reset by one torn write, a stale install missing the fix
    everybody thought had landed. Each one was eventually found — by
    Alek noticing something was off and somebody reading code. That is
    backwards. She runs on his machine, next to the evidence; she
    should be able to check herself and say, in one sentence, what is
    wrong: "volume control fails, everything else passes."

WHAT IT IS
    Seven read-only probes, one per subsystem whose failure used to
    hide: the COM audio round-trip, the phone dashboard (TLS pair,
    port plan, pairing key), Spotify's window/title telemetry, the
    memory store and its backups, the config file and its backups,
    the command registry the Commands screen is built from, and
    engine readiness (Gemini Live's ladder, or the pipeline's
    completeness — the same validation the failover checks).

    run_all() runs them concurrently, each bounded by a timeout: a
    hanging probe reports FAIL "timed out" instead of hanging the
    pass. A probe that cannot run on this platform reports SKIPPED
    with the reason — never a fake pass.

THE RULES
    * READ-ONLY. No probe changes a volume, launches a process,
      pairs a device, or writes a file. Two narrow exceptions, both
      disclosed in the probe's own detail line: the memory and
      config probes PARSE the live files directly and never call the
      recovering loaders — EXCEPT when the config file is already
      unparseable, when the config probe calls load_api_keys() once,
      deliberately, so the sanctioned recovery (restore from backup,
      incident, briefing note) happens under a witness instead of at
      some later silent load. Healing a file that is already broken
      is the loaders' designed behaviour; the probe just refuses to
      pretend it didn't happen.
    * NO SECRET VALUES, EVER. Config and engine probes report key
      NAMES and counts, and whether a key exists — never a value.
      A diagnostics report is spoken aloud and shown on a panel; it
      is the worst imaginable place for a credential to leak.
    * A FAILING PROBE IS DATA, NOT AN EXCEPTION. Every probe is
      wrapped: a raise becomes FAIL with the exception named, so one
      broken subsystem can never take the whole report down with it.

DELIVERY
    deliver() is the one way a finished pass reaches Alek: the
    checklist into the content panel (title DIAGNOSTICS), the same
    lines as a [Diagnostics] block in the activity log, an incident
    (kind "diagnostics_failed") ONLY when something failed, and one
    spoken sentence through the caller's announcement channel —
    everything checked out, or the count and the failing names.
    Detail lives in the panel and the log; speech stays one line.
"""

from __future__ import annotations

import json
import platform
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout
from datetime import datetime, timezone
from pathlib import Path

PASS, FAIL, SKIPPED = "pass", "fail", "skipped"

CHECK_TIMEOUT = 10.0   # seconds per probe — see run_all()


# ── Small seams (tests replace these; production code never does) ──────────

def _platform_system() -> str:
    return platform.system()


def _master_volume():
    """The master endpoint's volume via the app's own reader
    (actions/computer_settings.volume_get — the pycaw/COM round-trip
    every volume feature in the app stands on). Deferred import:
    actions import core, and this module must not close that loop at
    import time."""
    from actions.computer_settings import volume_get
    return volume_get()


def _spotify_volume():
    from core import spotify as _sp
    return _sp.spotify_volume()


def _short_model(model: str) -> str:
    return str(model or "").replace("models/", "")


def _clip(text, limit: int = 220) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


# ── Probe 1: the COM audio round-trip ──────────────────────────────────────

def check_volume() -> tuple[str, str]:
    """Read a volume back through the COM audio stack and get a
    number. This is the probe that would have caught the old
    assistant's COM death in seconds: her audio calls went into a
    helper process and failed where nobody could see; here the read
    either answers or the check says so.

    Windows only — the COM stack does not exist elsewhere, so the
    probe is SKIPPED there rather than passed on a technicality."""
    if _platform_system() != "Windows":
        return SKIPPED, ("Windows only — the COM audio stack this "
                         "probes does not exist on this platform")
    master = _master_volume()
    if master is not None:
        return PASS, f"COM audio answers — master volume reads {master}%"
    spot = _spotify_volume()
    if spot is not None:
        return PASS, (f"COM audio answers — Spotify's session volume "
                      f"reads {spot}% (the master endpoint itself did "
                      f"not answer)")
    return FAIL, ("no volume could be read back — neither the master "
                  "endpoint nor a Spotify session answered through "
                  "pycaw/COM")


# ── Probe 2: the phone dashboard ───────────────────────────────────────────

def check_dashboard() -> tuple[str, str]:
    """The bridge, from its own status surfaces: DashboardServer's
    _https_wanted()/_ssl_enabled() decide what serve() will do, so
    the probe asks them instead of re-deriving the rules. The
    certificate pair is only ever READ (existence + parse + key
    match + expiry) — generating one is serve()'s job, never a
    probe's."""
    from dashboard import server as dash

    key_len = dash.PAIRING_KEY_LENGTH
    if key_len < 8:
        return FAIL, (f"the pairing key is {key_len} characters — "
                      f"below the 8-character floor the login page "
                      f"and the key generator are built around")
    key_note = f"pairing key {key_len} characters"
    plan = (f"app on port {dash.SECURE_PORT}, port {dash.PORT} "
            f"redirects to it")

    if not dash.DashboardServer._https_wanted():
        return PASS, (f"plain HTTP by choice (\"dashboard_https\": "
                      f"false) — the app serves on port {dash.PORT} "
                      f"with no redirector; {key_note}")

    certs = dash.BASE_DIR / "config" / "certs"
    key_p, crt_p = certs / "jarvis.key", certs / "jarvis.crt"
    if not (key_p.exists() and crt_p.exists()):
        return FAIL, ("HTTPS is on but no certificate pair exists in "
                      "config/certs — one is generated at dashboard "
                      "startup; without it the dashboard serves plain "
                      "HTTP and says so")
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import serialization
        cert = x509.load_pem_x509_certificate(crt_p.read_bytes())
        pkey = serialization.load_pem_private_key(
            key_p.read_bytes(), password=None)
    except Exception as e:
        return FAIL, (f"the certificate pair in config/certs does "
                      f"not parse — {_clip(e)}")
    try:
        fmt = serialization.Encoding.PEM
        pub = serialization.PublicFormat.SubjectPublicKeyInfo
        if (cert.public_key().public_bytes(fmt, pub)
                != pkey.public_key().public_bytes(fmt, pub)):
            return FAIL, ("the certificate and private key in "
                          "config/certs do not match — TLS would "
                          "fail when the dashboard starts")
        try:
            expiry = cert.not_valid_after_utc
        except Exception:
            expiry = cert.not_valid_after.replace(tzinfo=timezone.utc)
    except Exception as e:
        return FAIL, f"the certificate pair could not be verified — {_clip(e)}"
    if expiry < datetime.now(timezone.utc):
        return FAIL, (f"the dashboard certificate expired on "
                      f"{expiry:%Y-%m-%d} — the phone will refuse it")
    return PASS, (f"HTTPS on {plan}; {key_note}; certificate parses, "
                  f"matches its key, valid until {expiry:%Y-%m-%d}")


# ── Probe 3: Spotify ───────────────────────────────────────────────────────

def check_spotify() -> tuple[str, str]:
    """Spotify's telemetry is its window title. Absence is not a
    failure — the app is simply not running — so a quiet machine
    passes with that note. What fails is the parser itself: a title
    in hand that now_playing() cannot read."""
    from core import spotify as sp
    titles = sp.window_titles()
    try:
        info = sp.now_playing(titles)
    except Exception as e:
        return FAIL, f"the window-title parser failed — {_clip(e)}"
    if not info["running"]:
        return PASS, "Spotify is not running"
    if info["playing"]:
        what = info["title"] or info["track"]
        return PASS, f"Spotify is running and playing — {_clip(what, 80)}"
    return PASS, "Spotify is running, idle"


# ── Probes 4 + 5: the two stores ───────────────────────────────────────────

def _backup_note(slots, parse) -> tuple[bool, str]:
    """(ok, note) for a store's backup rotation: no backups yet is a
    note, not a failure (a fresh store has had nothing to snapshot);
    a newest backup that does not parse IS a failure — the rotation
    only ever snapshots files that parse, so a corrupt newest backup
    means the damage happened after the snapshot, on disk."""
    existing = [p for p in slots if p.exists()]
    if not existing:
        return True, "no backups yet"
    newest = existing[0]
    try:
        good = parse(newest.read_text(encoding="utf-8")) is not None
    except Exception:
        good = False
    if not good:
        return False, (f"the newest backup ({newest.name}) does not "
                       f"parse — the recovery rotation is damaged")
    return True, f"{len(existing)} backup(s), newest parses"


def check_memory() -> tuple[str, str]:
    """The long-term store, parsed directly — load_memory() is NOT
    called: it recovers (and writes) when the store is unreadable,
    and a probe's job is to report the state, not change it. The
    detail says what the next real load would do."""
    from memory import embeddings, memory_manager as mm

    path = mm.MEMORY_PATH
    if not path.exists():
        return PASS, "no memory store yet — fresh install, nothing stored"
    try:
        data = mm._parse_store(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if data is None:
        return FAIL, ("long_term.json does not parse — the next load "
                      "would restore it from the backups (that "
                      "recovery has NOT been run by this check)")
    entries = sum(len(v) for v in data.values()
                  if isinstance(v, (dict, list)))
    ok, backup_note = _backup_note(mm._backup_slots(), mm._parse_store)
    if not ok:
        return FAIL, backup_note
    sidecar_note = "no embeddings sidecar yet"
    sidecar = embeddings._sidecar_path()
    if sidecar.exists():
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            good = isinstance(raw, dict) \
                and isinstance(raw.get("entries"), dict)
        except Exception:
            good, raw = False, {}
        if not good:
            return FAIL, ("the embeddings sidecar does not parse — "
                          "the facts themselves are intact; semantic "
                          "recall rebuilds the sidecar")
        sidecar_note = f"{len(raw['entries'])} embedded fact(s)"
    return PASS, (f"{len(data)} categories, {entries} entries; "
                  f"{backup_note}; {sidecar_note}")


def check_config() -> tuple[str, str]:
    """The config file, parsed directly so a healthy file is only
    ever read. When it does NOT parse, this probe deliberately calls
    load_api_keys() once: that loader IS the sanctioned recovery
    (restore from backups, keep the wreck, record an incident), and
    running it here means the recovery happens witnessed and
    reported instead of silently at the next load. Key VALUES are
    never read into any output — counts and names only."""
    from memory import config_manager as cm

    path = cm.CONFIG_FILE
    if not path.exists():
        return PASS, "no config file yet — fresh install, nothing stored"
    try:
        data = cm._parse_config(path.read_text(encoding="utf-8"))
    except Exception:
        data = None
    if data is None:
        asides_before = {p.name for p in
                         path.parent.glob(path.name + ".corrupt-*")}
        cm.load_api_keys()
        cm.pop_recovery_note()   # this delivery reports it instead
        try:
            restored = cm._parse_config(
                path.read_text(encoding="utf-8")) is not None
        except Exception:
            restored = False
        asides_after = {p.name for p in
                        path.parent.glob(path.name + ".corrupt-*")}
        if restored and asides_after == asides_before:
            return FAIL, ("api_keys.json did not parse — "
                          "load_api_keys() restored it from the "
                          "newest good backup during this check")
        return FAIL, ("api_keys.json did not parse and no backup "
                      "could restore it — the damaged file was kept "
                      "aside and the settings were reset")
    ok, backup_note = _backup_note(cm._backup_slots(), cm._parse_config)
    if not ok:
        return FAIL, backup_note
    return PASS, (f"{len(data)} settings/keys stored (names only — "
                  f"values are never shown); {backup_note}")


# ── Probe 6: the command registry walk ─────────────────────────────────────

def _command_rows():
    """The Commands screen's rows, built the way main.py builds
    them (_commands_for_ui): the inline declarations, a FRESH action
    discovery over the real actions/ directory with the inline names
    reserved, and the plugins'. A fresh discovery is the point: it
    re-imports every action file the way the running process did at
    startup, so a file broken since then is caught here, by name.
    Returns (rows, rejected) where rejected is [(file, error)]."""
    import main as main_mod
    from core.action_loader import discover_actions
    from core.commands import build_command_list
    from core.plugin_loader import discover_plugins

    base = Path(__file__).resolve().parent.parent
    inline = main_mod.TOOL_DECLARATIONS
    inline_names = {t["name"] for t in inline}
    actions = discover_actions(actions_dir=base / "actions",
                               reserved_names=inline_names,
                               logger=lambda _msg: None)
    plugins = discover_plugins(
        plugins_dir=base / "plugins",
        core_tool_names=inline_names | actions.names(),
        logger=lambda _msg: None)
    rows = build_command_list(
        inline, actions.get_tool_declarations(),
        plugins.get_tool_declarations())
    rejected = [(r.file, r.error)
                for r in getattr(actions, "_all_records", [])
                if not r.valid]
    rejected += [(r.file, r.error)
                 for r in getattr(plugins, "_all_records", [])
                 if not getattr(r, "valid", True)]
    return rows, rejected


def check_registry() -> tuple[str, str]:
    try:
        rows, rejected = _command_rows()
    except Exception as e:
        return FAIL, (f"the command registry walk itself failed — "
                      f"{type(e).__name__}: {_clip(e)}")
    if rejected:
        named = "; ".join(f"{f} ({_clip(err, 80)})"
                          for f, err in rejected[:3])
        more = (f" (+{len(rejected) - 3} more)"
                if len(rejected) > 3 else "")
        return FAIL, f"tool file(s) failed to load: {named}{more}"
    return PASS, (f"{len(rows)} commands build cleanly — the "
                  f"Commands screen's own construction")


# ── Probe 7: engine readiness ──────────────────────────────────────────────

def _pipeline_status() -> tuple[bool, str]:
    """(ready, label-or-reason) for the providers pipeline — main.py's
    _pipeline_failover_target validation, mirrored: a language model
    chosen for the slot, and registry.missing_fields empty for it.
    Half-configured reads as not ready, with the missing fields
    named. The label is the catalog name + model, as the failover
    notice speaks it."""
    try:
        from memory.config_manager import get_pipeline, get_provider_config
        from providers import registry
        pipe = get_pipeline()
        llm_id = str(pipe.get("llm") or "").strip()
        if not llm_id:
            return False, "no language model is chosen for the pipeline"
        cfg = get_provider_config(llm_id)
        missing = registry.missing_fields(llm_id, cfg)
        if missing:
            return False, f"{llm_id} needs: {', '.join(missing)}"
        name = llm_id
        catalog_id = str(cfg.get("catalog") or "").strip()
        if catalog_id:
            decl = registry.get_declaration(llm_id) or {}
            for entry in decl.get("catalog", ()):
                if entry.get("id") == catalog_id:
                    name = entry.get("label") or llm_id
                    break
        model = str(cfg.get("model") or "").strip()
        return True, f"{name} ({model})" if model else name
    except Exception as e:
        return False, f"pipeline check failed: {_clip(e)}"


def check_engine() -> tuple[str, str]:
    """Which engine holds the conversation, and whether it could.
    Live: a key exists (its value is never read into output) and at
    least one ladder rung is not resting. Pipeline: the same
    validation the startup path and the failover use. In Live mode
    the failover target's readiness rides along as a note — a
    half-configured target is exactly what Alek wants to learn
    BEFORE the night Gemini goes down, not during it."""
    from memory import config_manager as cm

    if cm.get_provider_mode() == "pipeline":
        ok, label = _pipeline_status()
        if not ok:
            return FAIL, (f"the providers pipeline is the selected "
                          f"engine but is not ready — {label}")
        return PASS, f"providers pipeline selected and ready ({label})"

    if not cm.get_gemini_key():
        return FAIL, ("no Gemini key in the config — Gemini Live "
                      "cannot connect")
    from core import gemini as _gemini
    if _gemini.all_live_resting():
        return FAIL, ("every Gemini Live rung is resting (quota or "
                      "access) — Live cannot connect right now")
    ok, label = _pipeline_status()
    failover = (f"failover target ready ({label})" if ok
                else f"no failover target configured ({label})")
    return PASS, (f"Gemini Live ready — {_short_model(_gemini.live_model())} "
                  f"heads the ladder; {failover}")


# ── The pass ───────────────────────────────────────────────────────────────

CHECKS = (
    ("Volume control", check_volume),
    ("Dashboard bridge", check_dashboard),
    ("Spotify", check_spotify),
    ("Memory store", check_memory),
    ("Configuration", check_config),
    ("Command registry", check_registry),
    ("Engine", check_engine),
)


def run_all(timeout: float = CHECK_TIMEOUT, checks=None) -> list[dict]:
    """Run every probe concurrently, each bounded by `timeout`
    seconds. Returns [{name, status, detail}] in CHECKS order. A
    probe that raises becomes FAIL with the exception named; one
    that overruns becomes FAIL "timed out" — the pass always
    completes, because a diagnostics run that hangs is worse than
    none. The executor is shut down without waiting: a wedged probe
    thread is left to die on its own rather than wedging the caller."""
    probes = list(checks if checks is not None else CHECKS)
    results: list[dict] = []
    executor = ThreadPoolExecutor(max_workers=max(1, len(probes)),
                                  thread_name_prefix="diagnostics")
    try:
        futures = [(name, executor.submit(fn)) for name, fn in probes]
        for name, future in futures:
            try:
                status, detail = future.result(timeout=timeout)
            except _FutureTimeout:
                status, detail = FAIL, f"timed out after {timeout:g}s"
            except Exception as e:
                status, detail = FAIL, f"{type(e).__name__}: {_clip(e)}"
            if status not in (PASS, FAIL, SKIPPED):
                status, detail = FAIL, f"probe returned {status!r}: {_clip(detail)}"
            results.append({"name": name, "status": status,
                            "detail": str(detail)})
    finally:
        executor.shutdown(wait=False)
    return results


# ── Rendering + the spoken sentence ────────────────────────────────────────

_MARKS = {PASS: "✓", FAIL: "✗", SKIPPED: "⊘"}


def render_report(results: list[dict]) -> str:
    """The checklist as panel/log text: one line per probe, then the
    tally. Detail lines carry the evidence; speech never does."""
    lines = [f"{_MARKS[r['status']]} {r['name']} — {r['detail']}"
             for r in results]
    passed = sum(1 for r in results if r["status"] == PASS)
    failed = sum(1 for r in results if r["status"] == FAIL)
    skipped = sum(1 for r in results if r["status"] == SKIPPED)
    tally = f"{passed} passed"
    if failed:
        tally += f", {failed} failed"
    if skipped:
        tally += f", {skipped} skipped"
    lines.append(tally + ".")
    return "\n".join(lines)


def spoken_summary(results: list[dict]) -> str:
    """The one sentence Alek hears. All-pass is said as the brief
    fixed it; failures are the count and the NAMES — never the
    detail, which lives in the panel and the log."""
    failed = [r["name"] for r in results if r["status"] == FAIL]
    if not failed:
        passed = sum(1 for r in results if r["status"] == PASS)
        skipped = sum(1 for r in results if r["status"] == SKIPPED)
        text = (f"Diagnostics: everything checks out — "
                f"{passed} checks passed.")
        if skipped:
            text += f" {skipped} skipped."
        return text
    return (f"Diagnostics: {len(failed)} of {len(results)} checks "
            f"failed — {', '.join(failed)}.")


def deliver(results: list[dict], show_content=None, write_log=None,
            announce=None) -> None:
    """The one delivery path, shared by the tool and the panel
    button: checklist to the content panel, the same lines as a
    [Diagnostics] log block, an incident ONLY on failure, and the
    spoken sentence through `announce` (the caller's announcement
    channel — plugin_say / request_say — which phrases the fixed
    sentence in the user's language). Every sink is optional and
    guarded: a broken panel must never cost the incident, and a
    broken log must never cost the speech."""
    report = render_report(results)
    if show_content is not None:
        try:
            show_content("DIAGNOSTICS", report)
        except Exception:
            pass
    if write_log is not None:
        try:
            for line in report.splitlines():
                write_log(f"[Diagnostics] {line}")
        except Exception:
            pass
    failed = [r["name"] for r in results if r["status"] == FAIL]
    if failed:
        try:
            from core import incidents
            incidents.record("diagnostics", "diagnostics_failed",
                             "failed checks: " + ", ".join(failed))
        except Exception:
            pass
    if announce is not None:
        try:
            announce(spoken_summary(results))
        except Exception:
            pass
