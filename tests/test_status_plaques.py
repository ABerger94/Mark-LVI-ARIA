"""Live status plaques (Alek's pick, 2026-10-23) — the three static
donor plaques at the bottom of ARIA's left rail ("AI CORE / ACTIVE",
"SEC / CLEARED", "PROTOCOL / ARIA") were pure decoration: hardcoded
labels, never updated, reporting nothing. They are now real reads —
ENGINE (which brain holds the conversation, a failover marked),
STATE (the session state word), TASKS (what she is running in the
background) — rendered by pure builders in ui.py and refreshed from
the runtime: STATE on every state change, TASKS on the fleet tick,
ENGINE through the get_engine_info bridge JarvisLive installs.

Pinned here: the builders' wording for every case, the value
fitting, the source wiring on both sides of the ui/main seam, and
the engine-info provider's three answers (live / pipeline /
failed-over) on a bare JarvisLive shell.
"""
import inspect
from pathlib import Path

import main as main_module
import ui as ui_module

_UI_SRC = Path(ui_module.__file__).read_text(encoding="utf-8")
_MAIN_SRC = Path(main_module.__file__).read_text(encoding="utf-8")


# ── engine_plaque_text ──────────────────────────────────────────────

def test_engine_live_shows_the_model_in_use():
    assert ui_module.engine_plaque_text(
        "gemini", "models/gemini-2.5-flash", None) == ("ENGINE", "gemini-2.5-flash")


def test_engine_live_without_a_model_is_gemini_live():
    assert ui_module.engine_plaque_text(
        "gemini", None, None) == ("ENGINE", "GEMINI LIVE")
    assert ui_module.engine_plaque_text(
        "gemini", "", None) == ("ENGINE", "GEMINI LIVE")


def test_engine_pipeline_selected_is_plain():
    assert ui_module.engine_plaque_text(
        "pipeline", "Groq (llama-3.3-70b)", None) == ("ENGINE", "PIPELINE")


def test_engine_failed_over_is_marked():
    assert ui_module.engine_plaque_text(
        "pipeline", None, "Groq (llama-3.3-70b)") == ("ENGINE", "PIPELINE (FO)")


def test_engine_live_model_is_shorthand_that_fits():
    # The full id ("gemini-3.1-flash-live-preview") overflowed the
    # 148px rail and clipped at both ends on Alek's laptop; the
    # shorthand is the same information at a width that fits.
    assert ui_module.engine_plaque_text(
        "gemini", "models/gemini-3.1-flash-live-preview", None) == (
        "ENGINE", "3.1 FLASH LIVE")


def test_plaque_values_never_exceed_the_rail():
    for _title, value in (
        ui_module.engine_plaque_text(
            "gemini", "models/gemini-3.1-flash-live-preview", None),
        ui_module.engine_plaque_text("pipeline", None, "Some Provider (a-model)"),
        ui_module.tasks_plaque_text(5, "an_extremely_long_delegated_task_name"),
        ui_module.state_plaque_text("LISTENING"),
    ):
        assert len(value) <= 16


# ── state_plaque_text ───────────────────────────────────────────────

def test_state_passes_the_word_through():
    assert ui_module.state_plaque_text("LISTENING") == ("STATE", "LISTENING")
    assert ui_module.state_plaque_text("SPEAKING") == ("STATE", "SPEAKING")
    assert ui_module.state_plaque_text("SLEEPING") == ("STATE", "SLEEPING")


def test_state_empty_is_a_dash():
    assert ui_module.state_plaque_text("") == ("STATE", "—")
    assert ui_module.state_plaque_text(None) == ("STATE", "—")


# ── tasks_plaque_text ───────────────────────────────────────────────

def test_tasks_idle_when_nothing_runs():
    assert ui_module.tasks_plaque_text(0, "") == ("TASKS", "IDLE")
    assert ui_module.tasks_plaque_text(0, "run_shell_command") == ("TASKS", "IDLE")


def test_tasks_single_runner_is_named():
    assert ui_module.tasks_plaque_text(
        1, "spotify") == ("TASKS", "spotify")


def test_tasks_many_show_first_plus_the_rest():
    assert ui_module.tasks_plaque_text(
        3, "delegate_task") == ("TASKS", "delegate_task +2")


def test_tasks_long_name_truncates_but_keeps_the_suffix():
    title, value = ui_module.tasks_plaque_text(2, "a_very_long_tool_name_here")
    assert title == "TASKS"
    assert len(value) <= 16 and value.endswith("+1") and "…" in value


# ── the engine-info provider (JarvisLive side) ──────────────────────

def _bare_live():
    """A JarvisLive shell (the test_engine_failover pattern): the
    provider only touches the attributes set by hand here."""
    live = main_module.JarvisLive.__new__(main_module.JarvisLive)
    live._failed_over = False
    live._failover_notice = None
    return live


def test_engine_info_live_reports_the_gemini_model():
    mode, model, label = _bare_live()._engine_info_for_ui()
    assert mode == "gemini" and label is None
    assert isinstance(model, str) and model  # core.gemini.live_model()


def test_engine_info_failed_over_reports_the_notice():
    live = _bare_live()
    live._failed_over = True
    live._failover_notice = "Groq (llama-3.3-70b)"
    assert live._engine_info_for_ui() == (
        "pipeline", None, "Groq (llama-3.3-70b)")


def test_engine_info_pipeline_selected(monkeypatch):
    live = _bare_live()
    monkeypatch.setattr(live, "_engine_mode", lambda: "pipeline")
    monkeypatch.setattr(live, "_pipeline_failover_target",
                        lambda: (True, "Groq (llama-3.3-70b)"))
    mode, model, label = live._engine_info_for_ui()
    assert (mode, label) == ("pipeline", None)
    # …and the pair renders as the plain pipeline plaque, not (FO).
    assert ui_module.engine_plaque_text(mode, model, label) == (
        "ENGINE", "PIPELINE")


def test_engine_info_never_raises():
    live = _bare_live()

    def _boom():
        raise RuntimeError("config unreadable")

    live._engine_mode = _boom
    assert live._engine_info_for_ui() == ("gemini", None, None)


# ── source pins ─────────────────────────────────────────────────────

def test_static_donor_plaques_are_gone():
    assert "AI CORE" not in _UI_SRC
    assert "CLEARED" not in _UI_SRC
    assert '"PROTOCOL\\n"' not in _UI_SRC


def test_refresh_paths_reference_the_plaques():
    apply_src = inspect.getsource(ui_module.MainWindow._apply_state)
    assert "_plaque_state" in apply_src and "state_plaque_text" in apply_src
    fleet_src = inspect.getsource(ui_module.MainWindow._refresh_fleet_pill)
    assert "_plaque_tasks" in fleet_src and "tasks_plaque_text" in fleet_src
    tick_src = inspect.getsource(ui_module.MainWindow._tick_clock)
    assert "get_engine_info" in tick_src and "engine_plaque_text" in tick_src


def test_engine_bridge_exists_on_both_sides():
    # Window default, facade proxy, JarvisLive install + provider.
    assert "self.get_engine_info" in _UI_SRC
    assert "self._win.get_engine_info" in _UI_SRC
    assert "self.ui.get_engine_info = self._engine_info_for_ui" in _MAIN_SRC
    provider = inspect.getsource(main_module.JarvisLive._engine_info_for_ui)
    assert "_failed_over" in provider
    assert "_pipeline_failover_target" in provider
    assert "live_model" in provider
