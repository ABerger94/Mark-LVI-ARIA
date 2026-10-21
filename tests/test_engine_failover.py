"""Phase G2 — automatic engine failover: Gemini Live -> pipeline.

The default engine is Gemini Live, and its own recovery is good: the
ladder steps down on quota, rejected resumption handles are dropped,
dropped sockets reconnect with backoff. What it cannot survive is
every rung resting at once, or a session failing faster than
reconnects can land. Those are the only two triggers. The switch is
not a second engine builder: the latch makes _resolve_engine_caps
resolve the pipeline — the manual panel selection's own construction
path — on the run loop's next pass.

The seam, as in the startup-briefing suite: JarvisLive is built with
__new__ (it cannot fully instantiate headless), the attributes the
failover methods touch are set by hand, config is a real tmp
api_keys.json, and validation runs against the real provider
registry — a "complete" pipeline here is one missing_fields accepts.
"""
import inspect
import time

import pytest

import main
from core import gemini as _gemini
from core import incidents
from memory import config_manager as cm
from providers import registry

GROQ_CFG = {"base_url": "https://api.groq.com/openai/v1",
            "model": "llama-3.3-70b-versatile",
            "api_key": "gsk-x",
            "catalog": "groq"}
LABEL = "Groq (llama-3.3-70b-versatile)"


@pytest.fixture()
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    return path


@pytest.fixture()
def complete_pipeline(cfg):
    cm.save_providers(
        mode="gemini",
        pipeline={"llm": "openai_compat", "stt": "", "tts": ""},
        creds={"openai_compat": dict(GROQ_CFG)},
    )
    return cfg


@pytest.fixture()
def incident_log(monkeypatch):
    seen = []
    monkeypatch.setattr(
        incidents, "record",
        lambda source, kind, message: seen.append((source, kind, message)))
    return seen


class _FakeUI:
    def __init__(self):
        self.logs = []

    def write_log(self, msg):
        self.logs.append(msg)


def _live():
    """A JarvisLive shell carrying only what the failover methods read."""
    live = main.JarvisLive.__new__(main.JarvisLive)
    live.ui = _FakeUI()
    live.session = None
    live._awake = True
    live._engine_caps = None
    live._conn_backoff = 3
    live._failed_over = False
    live._failover_dead = False
    live._failover_notice = None
    live._failover_outage_noted = False
    live._live_failure_times = []
    return live


# ── The ladder trigger's state (core/gemini) ────────────────────────────────

@pytest.fixture()
def clean_cooldown(monkeypatch):
    monkeypatch.setattr(_gemini, "_cooldown", {})
    return _gemini


def test_all_live_resting_only_when_every_rung_is_down(clean_cooldown):
    g = clean_cooldown
    assert g.all_live_resting() is False
    assert g.note_live_failure(g.LIVE_MODELS[0], "429 RESOURCE_EXHAUSTED")
    assert g.all_live_resting() is False          # one rung still stands
    assert g.live_model() == g.LIVE_MODELS[1]     # and the ladder steps to it
    assert g.note_live_failure(g.LIVE_MODELS[1], "429 RESOURCE_EXHAUSTED")
    assert g.all_live_resting() is True
    # live_model() still hands back the top rung — refusing to connect
    # at all was never the ladder's answer; failover is main.py's.
    assert g.live_model() == g.LIVE_MODELS[0]


def test_network_errors_do_not_rest_a_rung(clean_cooldown):
    g = clean_cooldown
    assert g.note_live_failure(g.LIVE_MODELS[0],
                               "getaddrinfo failed") is False
    assert g.all_live_resting() is False


# ── _pipeline_failover_target: startup's validation, verbatim ───────────────

def test_target_complete_pipeline(complete_pipeline):
    ready, label = _live()._pipeline_failover_target()
    assert ready is True
    assert label == LABEL


def test_target_label_falls_back_to_provider_id(cfg):
    cm.save_providers(
        pipeline={"llm": "openai_compat"},
        creds={"openai_compat": {"base_url": GROQ_CFG["base_url"],
                                 "model": GROQ_CFG["model"]}},
    )
    ready, label = _live()._pipeline_failover_target()
    assert ready is True
    assert label == "openai_compat (llama-3.3-70b-versatile)"


def test_target_no_provider_at_all(cfg):
    ready, why = _live()._pipeline_failover_target()
    assert ready is False
    assert "no language model" in why


def test_target_half_configured_is_no_provider(cfg):
    # Provider named for the slot, model never chosen: startup's
    # resolution refuses this too, so failover must (brief rule 6).
    cm.save_providers(
        pipeline={"llm": "openai_compat"},
        creds={"openai_compat": {"base_url": GROQ_CFG["base_url"]}},
    )
    ready, why = _live()._pipeline_failover_target()
    assert ready is False
    assert "Model" in why


# ── _maybe_failover: the latch and its gates ────────────────────────────────

def test_maybe_failover_latches_and_records(complete_pipeline, incident_log):
    live = _live()
    assert live._maybe_failover("every Live model is out of quota") is True
    assert live._failed_over is True
    assert live._failover_notice == LABEL
    assert incident_log == [
        ("engine", "engine_failover",
         f"every Live model is out of quota — switched to {LABEL}")]
    assert any("switching to" in line and LABEL in line
               for line in live.ui.logs)
    # Latched is latched: a second trigger changes nothing, records nothing.
    assert live._maybe_failover("again") is True
    assert len(incident_log) == 1


def test_maybe_failover_without_provider(cfg, incident_log):
    live = _live()
    assert live._maybe_failover("ladder exhausted") is False
    assert live._failed_over is False
    assert incident_log == []


def test_maybe_failover_half_configured(cfg, incident_log):
    cm.save_providers(
        pipeline={"llm": "openai_compat"},
        creds={"openai_compat": {"base_url": GROQ_CFG["base_url"]}},
    )
    live = _live()
    assert live._maybe_failover("ladder exhausted") is False
    assert live._failed_over is False
    assert incident_log == []


def test_maybe_failover_disabled_by_flag(complete_pipeline, incident_log):
    cm.save_engine_auto_failover(False)
    live = _live()
    assert live._maybe_failover("ladder exhausted") is False
    assert live._failed_over is False
    assert incident_log == []


def test_maybe_failover_already_pipeline(complete_pipeline, incident_log):
    cm.save_provider_mode("pipeline")
    live = _live()
    assert live._maybe_failover("ladder exhausted") is False
    assert live._failed_over is False
    assert incident_log == []


def test_maybe_failover_dead_target_is_never_retried(complete_pipeline,
                                                     incident_log):
    live = _live()
    live._failover_dead = True     # the pipeline would not build, earlier
    assert live._maybe_failover("ladder exhausted") is False
    assert live._failed_over is False
    assert incident_log == []


# ── The config flag (config_manager) ────────────────────────────────────────

def test_auto_failover_flag_defaults_on_and_round_trips(cfg):
    assert cm.get_engine_auto_failover() is True
    cm.save_engine_auto_failover(False)
    assert cm.get_engine_auto_failover() is False
    cm.save_engine_auto_failover(True)
    assert cm.get_engine_auto_failover() is True


# ── _note_live_failure: the window trigger ──────────────────────────────────

def test_single_transient_failure_never_fails_over(complete_pipeline,
                                                   incident_log):
    live = _live()
    live._note_live_failure()
    live._note_live_failure()
    assert live._failed_over is False
    assert len(live._live_failure_times) == 2
    assert incident_log == []


def test_repeated_failures_inside_window_fail_over(complete_pipeline,
                                                   incident_log):
    live = _live()
    for _ in range(main._FAILOVER_FAILURE_LIMIT):
        live._note_live_failure()
    assert live._failed_over is True
    assert live._live_failure_times == []        # consumed by the latch
    assert live._conn_backoff == 0               # the switch is immediate
    assert [k for _, k, _ in incident_log] == ["engine_failover"]


def test_failures_spread_beyond_window_never_fail_over(complete_pipeline,
                                                       monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    live = _live()
    for t in (1000.0, 1000.0 + main._FAILOVER_WINDOW_SECONDS + 80,
              1000.0 + 2 * (main._FAILOVER_WINDOW_SECONDS + 80)):
        clock[0] = t
        live._note_live_failure()
    assert live._failed_over is False
    assert len(live._live_failure_times) == 1    # only the newest is in-window


def test_window_ignores_pipeline_failures(complete_pipeline):
    live = _live()
    live._engine_caps = {}                       # the pipeline held the session
    for _ in range(main._FAILOVER_FAILURE_LIMIT + 2):
        live._note_live_failure()
    assert live._live_failure_times == []
    assert live._failed_over is False


def test_window_ignores_failures_after_latch(complete_pipeline):
    live = _live()
    live._failed_over = True
    live._note_live_failure()
    assert live._live_failure_times == []


def test_window_without_provider_notes_the_fix_once(cfg):
    live = _live()
    for _ in range(main._FAILOVER_FAILURE_LIMIT + 1):
        live._note_live_failure()
    assert live._failed_over is False
    notes = [line for line in live.ui.logs
             if "enables automatic failover" in line]
    assert len(notes) == 1


# ── _note_failover_unavailable: the honest report ───────────────────────────

def test_unavailable_note_names_the_fix_once(cfg):
    live = _live()
    live._note_failover_unavailable()
    live._note_failover_unavailable()
    notes = [line for line in live.ui.logs if "ENGINE & PROVIDERS" in line]
    assert len(notes) == 1
    assert "provider key" in notes[0]


def test_unavailable_note_when_flag_off(complete_pipeline):
    cm.save_engine_auto_failover(False)
    live = _live()
    live._note_failover_unavailable()
    assert any("switched off" in line for line in live.ui.logs)


# ── The spoken notice: exactly once, awake only ─────────────────────────────

def _live_with_notice():
    live = _live()
    live.session = object()                      # a session is up
    live._failover_notice = LABEL
    said = []
    live.plugin_say = said.append
    return live, said


def test_notice_spoken_exactly_once():
    live, said = _live_with_notice()
    live._deliver_failover_notice()
    live._deliver_failover_notice()
    assert len(said) == 1
    expected = f"My Gemini connection is down, so I've switched to {LABEL}."
    assert expected in said[0]
    assert "conversation has restarted" in said[0]


def test_notice_waits_while_asleep():
    live, said = _live_with_notice()
    live._awake = False
    live._deliver_failover_notice()
    assert said == []
    assert live._failover_notice == LABEL        # still owed, not lost
    live._awake = True                           # wake() retries delivery
    live._deliver_failover_notice()
    assert len(said) == 1


def test_notice_waits_without_a_session():
    live, said = _live_with_notice()
    live.session = None
    live._deliver_failover_notice()
    assert said == []
    assert live._failover_notice == LABEL


# ── The swap path: latched resolution IS the manual construction ────────────

class _FakeLLM:
    caps = {}


def test_resolve_engine_caps_forced_pipeline_when_latched(complete_pipeline,
                                                          monkeypatch):
    built = []

    def fake_build(pid, cfg):
        built.append(pid)
        return _FakeLLM()

    monkeypatch.setattr(registry, "build", fake_build)
    live = _live()
    assert live._engine_mode() == "gemini"
    assert live._resolve_engine_caps() is None       # manual mode: Live
    live._failed_over = True
    caps = live._resolve_engine_caps()
    assert caps is not None                          # forced: pipeline
    assert built == ["openai_compat"]                # the registry built it


def test_resolve_engine_caps_latched_but_unbuildable(complete_pipeline,
                                                     monkeypatch):
    def broken_build(pid, cfg):
        raise RuntimeError("boom")

    monkeypatch.setattr(registry, "build", broken_build)
    live = _live()
    live._failed_over = True
    # The existing contract: a pipeline that will not build resolves to
    # None (Live) with the reason logged — run() turns this exact
    # outcome into engine_failover_failed (pinned by source below).
    assert live._resolve_engine_caps() is None
    assert any("local engine unavailable" in line for line in live.ui.logs)


# ── Conversation state: the manual switch drops history; so does failover ──

def test_pipeline_engine_starts_with_empty_history():
    from providers.engine_pipeline import PipelineEngine
    engine = PipelineEngine(_FakeLLM(), None, None, config=None)
    assert engine._history == []


def test_on_engine_applied_clears_the_latch(complete_pipeline):
    live = _live()
    live._failed_over = True
    live._failover_dead = True
    live._failover_notice = LABEL
    live._on_engine_applied()
    assert live._failed_over is False
    assert live._failover_dead is False
    assert live._failover_notice is None


# ── Source pins: the run loop's wiring ──────────────────────────────────────

def test_run_loop_wiring():
    run_src = inspect.getsource(main.JarvisLive.run)
    # Trigger 1 is consulted before the plain retry line is written.
    assert "_gemini.all_live_resting()" in run_src
    assert (run_src.index("all_live_resting")
            < run_src.index("Every live model is rate-limited"))
    # A latched failover whose pipeline would not build is terminal.
    assert "engine_failover_failed" in run_src
    # The generic tail feeds the window trigger.
    assert "self._note_live_failure()" in run_src
    # Session-up delivers an owed notice; wake() retries it.
    assert "self._deliver_failover_notice()" in run_src
    assert "_deliver_failover_notice" in inspect.getsource(main.JarvisLive.wake)
    # Caps resolution is where the latch bites.
    assert "_failed_over" in inspect.getsource(main.JarvisLive._resolve_engine_caps)
