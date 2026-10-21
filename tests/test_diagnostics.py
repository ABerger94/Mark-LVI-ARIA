"""Phase G3 — diagnostics: the self-test pass (core/diagnostics.py),
its tool (actions/diagnostics.py), and its panel button.

What is being pinned shut:

  * every probe's verdict at its seam — the COM volume read, the
    dashboard's own status surfaces, Spotify's title parser, the two
    stores parsed DIRECTLY (a corrupt memory store fails the check
    and is left byte-for-byte untouched: the probe reports, it does
    not recover), the registry walk, and engine readiness in both
    modes, including the half-configured pipeline;
  * the one sanctioned exception to read-only: a corrupt CONFIG
    file triggers load_api_keys()' designed recovery inside the
    check, and the detail says so;
  * the runner's bounds — a hanging probe is FAIL "timed out", a
    raising probe is FAIL with the exception named, and the pass
    always completes;
  * the spoken summaries, word for word: all-pass, and failures as
    count + names only;
  * delivery: panel + log + incident ONLY on failure + the announce
    seam, and no secret VALUE anywhere in any rendered output.
"""
import json
import time
from pathlib import Path

import pytest

import core.diagnostics as diag
from core import incidents


# ── helpers ────────────────────────────────────────────────────────────────

@pytest.fixture()
def incident_log(monkeypatch):
    seen = []
    monkeypatch.setattr(
        incidents, "record",
        lambda source, kind, message: seen.append((source, kind, message)))
    return seen


def _result(name, status, detail=""):
    return {"name": name, "status": status, "detail": detail}


# ── probe 1: volume / COM ──────────────────────────────────────────────────

def test_volume_skipped_off_windows(monkeypatch):
    monkeypatch.setattr(diag, "_platform_system", lambda: "Linux")
    status, detail = diag.check_volume()
    assert status == diag.SKIPPED
    assert "Windows" in detail


def test_volume_passes_on_a_master_read(monkeypatch):
    monkeypatch.setattr(diag, "_platform_system", lambda: "Windows")
    monkeypatch.setattr(diag, "_master_volume", lambda: 43)
    monkeypatch.setattr(diag, "_spotify_volume", lambda: None)
    status, detail = diag.check_volume()
    assert status == diag.PASS
    assert "43%" in detail


def test_volume_passes_on_a_spotify_session_read(monkeypatch):
    monkeypatch.setattr(diag, "_platform_system", lambda: "Windows")
    monkeypatch.setattr(diag, "_master_volume", lambda: None)
    monkeypatch.setattr(diag, "_spotify_volume", lambda: 70)
    status, detail = diag.check_volume()
    assert status == diag.PASS
    assert "70%" in detail


def test_volume_fails_when_com_answers_nothing(monkeypatch):
    """The old assistant's failure mode: the audio stack is dead and
    every read comes back empty. That is a FAIL, said plainly."""
    monkeypatch.setattr(diag, "_platform_system", lambda: "Windows")
    monkeypatch.setattr(diag, "_master_volume", lambda: None)
    monkeypatch.setattr(diag, "_spotify_volume", lambda: None)
    status, detail = diag.check_volume()
    assert status == diag.FAIL
    assert "no volume could be read" in detail


# ── probe 2: dashboard ─────────────────────────────────────────────────────

@pytest.fixture()
def dash_base(tmp_path, monkeypatch):
    from dashboard import server as dash
    monkeypatch.setattr(dash, "BASE_DIR", tmp_path)
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "api_keys.json").write_text("{}", encoding="utf-8")
    return tmp_path


def test_dashboard_cert_missing_fails(dash_base):
    status, detail = diag.check_dashboard()
    assert status == diag.FAIL
    assert "certificate" in detail


def test_dashboard_https_passes_with_a_real_pair(dash_base):
    from dashboard import server as dash
    assert dash._ensure_certs() is True      # the app's own generator
    status, detail = diag.check_dashboard()
    assert status == diag.PASS
    assert "8001" in detail and "8000" in detail
    assert "pairing key 8 characters" in detail
    assert "valid until" in detail


def test_dashboard_plain_http_by_choice_passes(dash_base):
    (dash_base / "config" / "api_keys.json").write_text(
        json.dumps({"dashboard_https": False}), encoding="utf-8")
    status, detail = diag.check_dashboard()
    assert status == diag.PASS
    assert "plain HTTP by choice" in detail


def test_dashboard_short_pairing_key_fails(dash_base, monkeypatch):
    from dashboard import server as dash
    monkeypatch.setattr(dash, "PAIRING_KEY_LENGTH", 6)
    status, detail = diag.check_dashboard()
    assert status == diag.FAIL
    assert "pairing key" in detail


def test_dashboard_unparseable_cert_fails(dash_base):
    certs = dash_base / "config" / "certs"
    certs.mkdir()
    (certs / "jarvis.crt").write_bytes(b"not a certificate")
    (certs / "jarvis.key").write_bytes(b"not a key")
    status, detail = diag.check_dashboard()
    assert status == diag.FAIL
    assert "does not parse" in detail


# ── probe 3: Spotify ───────────────────────────────────────────────────────

def test_spotify_not_running_passes(monkeypatch):
    import core.spotify as sp
    monkeypatch.setattr(sp, "window_titles", lambda: [])
    status, detail = diag.check_spotify()
    assert status == diag.PASS
    assert "not running" in detail


def test_spotify_playing_passes_with_the_title(monkeypatch):
    import core.spotify as sp
    monkeypatch.setattr(sp, "window_titles",
                        lambda: ["Daft Punk - One More Time"])
    status, detail = diag.check_spotify()
    assert status == diag.PASS
    assert "One More Time" in detail


def test_spotify_parser_exception_fails(monkeypatch):
    import core.spotify as sp
    monkeypatch.setattr(sp, "window_titles", lambda: ["Anything"])
    def boom(titles=None):
        raise RuntimeError("parser exploded")
    monkeypatch.setattr(sp, "now_playing", boom)
    status, detail = diag.check_spotify()
    assert status == diag.FAIL
    assert "parser failed" in detail


# ── probe 4: memory store ──────────────────────────────────────────────────

@pytest.fixture()
def mem_path(tmp_path, monkeypatch):
    from memory import memory_manager as mm
    path = tmp_path / "long_term.json"
    monkeypatch.setattr(mm, "MEMORY_PATH", path)
    return path


def test_memory_fresh_install_passes(mem_path):
    status, detail = diag.check_memory()
    assert status == diag.PASS
    assert "fresh install" in detail


def test_memory_valid_store_passes_with_counts(mem_path):
    mem_path.write_text(json.dumps(
        {"identity": {"name": {"value": "Alek"}},
         "preferences": {"a": 1, "b": 2}, "sessions": [1, 2]}),
        encoding="utf-8")
    status, detail = diag.check_memory()
    assert status == diag.PASS
    assert "3 categories, 5 entries" in detail


def test_memory_corrupt_store_fails_and_is_not_touched(mem_path):
    wreck = b"{ this is not json"
    mem_path.write_bytes(wreck)
    status, detail = diag.check_memory()
    assert status == diag.FAIL
    assert "does not parse" in detail
    # read-only: the probe must NOT have run the recovering load —
    # the wreck is byte-identical and no backup rotation appeared.
    assert mem_path.read_bytes() == wreck
    assert not (mem_path.parent / "backups").exists()


def test_memory_corrupt_newest_backup_fails(mem_path):
    mem_path.write_text(json.dumps({"identity": {}}), encoding="utf-8")
    backups = mem_path.parent / "backups"
    backups.mkdir()
    (backups / "long_term.1.json").write_bytes(b"nope")
    status, detail = diag.check_memory()
    assert status == diag.FAIL
    assert "newest backup" in detail


def test_memory_corrupt_sidecar_fails(mem_path):
    mem_path.write_text(json.dumps({"identity": {}}), encoding="utf-8")
    (mem_path.parent / "embeddings.json").write_bytes(b"nope")
    status, detail = diag.check_memory()
    assert status == diag.FAIL
    assert "sidecar" in detail


# ── probe 5: config ────────────────────────────────────────────────────────

@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    from memory import config_manager as cm
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    return path


def test_config_valid_passes_and_never_shows_values(cfg_path, incident_log):
    cfg_path.write_text(json.dumps(
        {"gemini_api_key": "SECRET-VALUE-123", "user_name": "Alek"}),
        encoding="utf-8")
    status, detail = diag.check_config()
    assert status == diag.PASS
    assert "2 settings/keys" in detail
    assert "SECRET-VALUE-123" not in detail
    assert incident_log == []        # a healthy file triggers nothing


def test_config_corrupt_recovers_from_backup_inside_the_check(
        cfg_path, incident_log):
    """The one sanctioned write: the loader's own recovery, run
    deliberately and reported in the detail line."""
    from memory import config_manager as cm
    cfg_path.write_bytes(b"{ torn")
    backups = cfg_path.parent / "backups"
    backups.mkdir()
    (backups / "api_keys.1.json").write_text(
        json.dumps({"gemini_api_key": "k", "user_name": "Alek"}),
        encoding="utf-8")
    status, detail = diag.check_config()
    assert status == diag.FAIL
    assert "restored it from the newest good backup" in detail
    # and the recovery really happened — the live file parses now
    assert cm._parse_config(cfg_path.read_text(encoding="utf-8")) is not None
    assert any(kind == "config_recovered" for _, kind, _ in incident_log)


def test_config_corrupt_without_backup_fails_and_keeps_the_wreck(
        cfg_path, incident_log):
    cfg_path.write_bytes(b"{ torn")
    status, detail = diag.check_config()
    assert status == diag.FAIL
    assert "no backup could restore it" in detail
    asides = list(cfg_path.parent.glob("api_keys.json.corrupt-*"))
    assert len(asides) == 1 and asides[0].read_bytes() == b"{ torn"


# ── probe 6: registry walk ─────────────────────────────────────────────────

def test_registry_walk_constructs(monkeypatch):
    rows, rejected = diag._command_rows()
    assert len(rows) >= 70           # the live registries, this tree
    assert isinstance(rejected, list)


def test_registry_passes_on_a_clean_walk(monkeypatch):
    monkeypatch.setattr(diag, "_command_rows",
                        lambda: ([{"name": "x"}] * 72, []))
    status, detail = diag.check_registry()
    assert status == diag.PASS
    assert "72 commands" in detail


def test_registry_fails_naming_the_broken_file(monkeypatch):
    monkeypatch.setattr(
        diag, "_command_rows",
        lambda: ([], [("broken_action.py", "Failed to load: boom")]))
    status, detail = diag.check_registry()
    assert status == diag.FAIL
    assert "broken_action.py" in detail


def test_registry_walk_raising_fails_the_check(monkeypatch):
    def boom():
        raise RuntimeError("walk died")
    monkeypatch.setattr(diag, "_command_rows", boom)
    status, detail = diag.check_registry()
    assert status == diag.FAIL
    assert "registry walk itself failed" in detail


# ── probe 7: engine ────────────────────────────────────────────────────────

GROQ_CFG = {"base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "api_key": "gsk-x",
            "catalog": "groq"}


def test_engine_live_ready_passes(cfg_path, monkeypatch):
    from core import gemini as _gemini
    cfg_path.write_text(json.dumps({"gemini_api_key": "k" * 20}),
                        encoding="utf-8")
    monkeypatch.setattr(_gemini, "all_live_resting", lambda: False)
    monkeypatch.setattr(_gemini, "live_model",
                        lambda: "models/gemini-3.1-flash-live-preview")
    status, detail = diag.check_engine()
    assert status == diag.PASS
    assert "Gemini Live ready" in detail
    assert "gemini-3.1-flash-live-preview" in detail


def test_engine_live_without_a_key_fails(cfg_path):
    cfg_path.write_text("{}", encoding="utf-8")
    status, detail = diag.check_engine()
    assert status == diag.FAIL
    assert "no Gemini key" in detail


def test_engine_live_all_rungs_resting_fails(cfg_path, monkeypatch):
    from core import gemini as _gemini
    cfg_path.write_text(json.dumps({"gemini_api_key": "k" * 20}),
                        encoding="utf-8")
    monkeypatch.setattr(_gemini, "all_live_resting", lambda: True)
    status, detail = diag.check_engine()
    assert status == diag.FAIL
    assert "every Gemini Live rung is resting" in detail


def test_engine_pipeline_ready_passes(cfg_path):
    from memory import config_manager as cm
    cm.save_providers(
        mode="pipeline",
        pipeline={"llm": "openai_compat", "stt": "", "tts": ""},
        creds={"openai_compat": dict(GROQ_CFG)})
    status, detail = diag.check_engine()
    assert status == diag.PASS
    assert "Groq (llama-3.3-70b-versatile)" in detail


def test_engine_pipeline_half_configured_fails(cfg_path):
    from memory import config_manager as cm
    bad = dict(GROQ_CFG)
    del bad["model"]     # Address + Model are the required fields;
                         # the API key is optional (local servers)
    cm.save_providers(
        mode="pipeline",
        pipeline={"llm": "openai_compat", "stt": "", "tts": ""},
        creds={"openai_compat": bad})
    status, detail = diag.check_engine()
    assert status == diag.FAIL
    assert "needs:" in detail


def test_engine_live_mode_reports_the_failover_target(cfg_path, monkeypatch):
    """A complete pipeline sitting behind Live is readiness
    information, spoken before the night it is needed."""
    from core import gemini as _gemini
    from memory import config_manager as cm
    cm.save_providers(
        mode="gemini",
        pipeline={"llm": "openai_compat", "stt": "", "tts": ""},
        creds={"openai_compat": dict(GROQ_CFG)})
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["gemini_api_key"] = "k" * 20
    cfg_path.write_text(json.dumps(data), encoding="utf-8")
    monkeypatch.setattr(_gemini, "all_live_resting", lambda: False)
    status, detail = diag.check_engine()
    assert status == diag.PASS
    assert "failover target ready (Groq" in detail


# ── the runner ─────────────────────────────────────────────────────────────

def test_runner_bounds_a_hanging_probe():
    def slow():
        time.sleep(5)
        return diag.PASS, "eventually"
    started = time.monotonic()
    results = diag.run_all(timeout=0.2, checks=[("Slow", slow)])
    elapsed = time.monotonic() - started
    assert results[0]["status"] == diag.FAIL
    assert "timed out" in results[0]["detail"]
    assert elapsed < 3        # the pass completed; it did not wait


def test_runner_turns_a_raise_into_a_named_fail():
    def boom():
        raise ValueError("bad things")
    results = diag.run_all(checks=[("Boom", boom)])
    assert results[0]["status"] == diag.FAIL
    assert "ValueError" in results[0]["detail"]


def test_runner_keeps_order_and_shape():
    results = diag.run_all(checks=[
        ("A", lambda: (diag.PASS, "fine")),
        ("B", lambda: (diag.SKIPPED, "not here")),
    ])
    assert [r["name"] for r in results] == ["A", "B"]
    assert results[1]["status"] == diag.SKIPPED


# ── summary + rendering ────────────────────────────────────────────────────

def test_spoken_summary_all_pass_is_the_fixed_sentence():
    results = [_result("A", diag.PASS), _result("B", diag.PASS),
               _result("C", diag.PASS)]
    assert diag.spoken_summary(results) == \
        "Diagnostics: everything checks out — 3 checks passed."


def test_spoken_summary_names_failures_without_detail():
    results = [_result("Volume control", diag.PASS),
               _result("Memory store", diag.FAIL, "secret-ish detail"),
               _result("Engine", diag.FAIL, "more detail")]
    text = diag.spoken_summary(results)
    assert text == ("Diagnostics: 2 of 3 checks failed — "
                    "Memory store, Engine.")
    assert "detail" not in text


def test_render_report_carries_marks_and_tally():
    results = [_result("A", diag.PASS, "ok"),
               _result("B", diag.FAIL, "broken"),
               _result("C", diag.SKIPPED, "n/a")]
    report = diag.render_report(results)
    assert "✓ A — ok" in report
    assert "✗ B — broken" in report
    assert "⊘ C — n/a" in report
    assert report.splitlines()[-1] == "1 passed, 1 failed, 1 skipped."


# ── delivery ───────────────────────────────────────────────────────────────

def test_deliver_all_pass_no_incident(incident_log):
    shown, logged, said = [], [], []
    results = [_result("A", diag.PASS, "ok")]
    diag.deliver(results,
                 show_content=lambda t, x: shown.append((t, x)),
                 write_log=logged.append,
                 announce=said.append)
    assert shown and shown[0][0] == "DIAGNOSTICS"
    assert any(line.startswith("[Diagnostics]") for line in logged)
    assert said == ["Diagnostics: everything checks out — 1 checks passed."]
    assert incident_log == []


def test_deliver_failure_records_one_incident(incident_log):
    said = []
    results = [_result("A", diag.PASS), _result("Memory store", diag.FAIL, "x"),
               _result("Engine", diag.FAIL, "y")]
    diag.deliver(results, announce=said.append)
    assert said == ["Diagnostics: 2 of 3 checks failed — Memory store, Engine."]
    assert len(incident_log) == 1
    source, kind, message = incident_log[0]
    assert source == "diagnostics" and kind == "diagnostics_failed"
    assert "Memory store" in message and "Engine" in message


def test_deliver_survives_broken_sinks(incident_log):
    def boom(*a, **k):
        raise RuntimeError("sink down")
    results = [_result("A", diag.FAIL, "x")]
    diag.deliver(results, show_content=boom, write_log=boom, announce=boom)
    assert len(incident_log) == 1      # the incident still landed


def test_no_secret_value_anywhere_in_the_full_output(cfg_path, incident_log):
    """End to end: a config full of secrets goes through its probe,
    the render and the delivery — the values appear in none of it."""
    cfg_path.write_text(json.dumps({
        "gemini_api_key": "SECRET-VALUE-123",
        "gmail_app_password": "SECRET-VALUE-456",
        "github_token": "SECRET-VALUE-789",
    }), encoding="utf-8")
    shown, logged, said = [], [], []
    results = [diag.check_config()]
    diag.deliver([{"name": "Configuration", "status": results[0][0],
                   "detail": results[0][1]}],
                 show_content=lambda t, x: shown.append(x),
                 write_log=logged.append, announce=said.append)
    blob = "\n".join(shown + logged + said)
    assert "SECRET-VALUE" not in blob


# ── the tool ───────────────────────────────────────────────────────────────

class _FakePlayer:
    def __init__(self):
        self.shown, self.logged, self.said = [], [], []

    def show_content(self, title, text):
        self.shown.append((title, text))

    def write_log(self, text):
        self.logged.append(text)

    def request_say(self, instruction):
        self.said.append(instruction)


def test_tool_delivers_and_announces(monkeypatch):
    from actions import diagnostics as action
    canned = [_result("Volume control", diag.PASS, "ok"),
              _result("Engine", diag.FAIL, "down")]
    monkeypatch.setattr(diag, "run_all", lambda: canned)
    player = _FakePlayer()
    out = action.run_diagnostics(player=player)
    assert player.shown and player.shown[0][0] == "DIAGNOSTICS"
    assert len(player.said) == 1
    assert "Diagnostics: 1 of 2 checks failed — Engine." in player.said[0]
    assert "Diagnostics: 1 of 2 checks failed — Engine." in out
    assert action.TOOL["scheduling"] == "SILENT"
    assert action.TOOL["name"] == "run_diagnostics"


def test_tool_without_a_player_still_reports(monkeypatch):
    from actions import diagnostics as action
    monkeypatch.setattr(diag, "run_all",
                        lambda: [_result("A", diag.PASS, "ok")])
    out = action.run_diagnostics()
    assert "everything checks out" in out


# ── the panel button: wiring pins ──────────────────────────────────────────

def test_ui_and_main_wiring_is_present():
    root = Path(__file__).resolve().parent.parent
    ui_src = (root / "ui.py").read_text(encoding="utf-8")
    main_src = (root / "main.py").read_text(encoding="utf-8")
    assert "DIAGNOSTICS" in ui_src and "_run_diagnostics" in ui_src
    assert "on_run_diagnostics" in ui_src      # window attr + facade property
    assert "self.ui.on_run_diagnostics = self._diagnostics_for_ui" in main_src
    assert "_diagnostics_for_ui" in main_src
