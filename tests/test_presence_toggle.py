"""Presence v1.1 — the SETUP drawer on/off toggle.

Scope is the toggle only: presence_enabled gains a drawer button so it
no longer needs a hand-edit of config/api_keys.json, and the change
applies live through JarvisLive._ui_presence_toggle. The numeric knobs
(idle / greet-after / quiet hours) stay config-file only.

Pinned here: the config save round-trips without disturbing other
keys; the drawer's PRESENCE button exists, its label mirrors config,
and clicking flips config + label both ways; a wired callback receives
the new bool exactly once per click; and the JarvisLive side builds /
registers on enable, unregisters on disable (fail-open registry),
restores a napped HUD without ever greeting, and still logs the off
line when there was no monitor to stop.
"""
import json

import pytest
from PyQt6.QtWidgets import QApplication

import ui as ui_module
from core import incidents, presence
from memory import config_manager as cm

_APP = QApplication.instance() or QApplication([])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    monkeypatch.setattr(cm, "_recovery_note", None)
    yield path


@pytest.fixture(autouse=True)
def _registry_clean():
    presence.set_monitor(None)
    yield
    presence.set_monitor(None)


def _drawer_window():
    """A MainWindow shell (the test_fleet/test_pet_mode pattern) with
    only what _build_quick_drawer touches: a central widget to parent
    the drawer and the callback slot the real __init__ would set."""
    win = ui_module.MainWindow.__new__(ui_module.MainWindow)
    ui_module.QMainWindow.__init__(win)
    win.on_presence_toggle = None
    win.setCentralWidget(ui_module.QWidget())
    win._quick_drawer = win._build_quick_drawer()
    return win


# ── Config save ───────────────────────────────────────────────────────────

def test_save_presence_enabled_roundtrips_and_preserves_other_keys(cfg):
    cfg.write_text(json.dumps({"gemini_api_key": "key-1",
                               "presence_idle_seconds": 42}),
                   encoding="utf-8")
    cm.save_presence_enabled(False)
    settings = cm.get_presence_settings()
    assert settings["presence_enabled"] is False
    assert settings["presence_idle_seconds"] == 42   # knobs untouched
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["gemini_api_key"] == "key-1"          # other keys intact
    assert data["presence_enabled"] is False
    cm.save_presence_enabled(True)
    assert cm.get_presence_settings()["presence_enabled"] is True


# ── The drawer button ─────────────────────────────────────────────────────

def test_drawer_has_presence_button_labelled_from_config(cfg):
    win = _drawer_window()
    assert win._presence_btn.text() == "◉  PRESENCE: ON"   # default: on


def test_drawer_button_starts_off_when_config_says_off(cfg):
    cm.save_presence_enabled(False)
    win = _drawer_window()
    assert win._presence_btn.text() == "◉  PRESENCE: OFF"


def test_clicking_button_flips_config_and_label_both_ways(cfg):
    win = _drawer_window()
    win._presence_btn.click()
    assert cm.get_presence_settings()["presence_enabled"] is False
    assert win._presence_btn.text() == "◉  PRESENCE: OFF"
    win._presence_btn.click()
    assert cm.get_presence_settings()["presence_enabled"] is True
    assert win._presence_btn.text() == "◉  PRESENCE: ON"


def test_click_delivers_new_bool_to_callback_exactly_once(cfg):
    win = _drawer_window()
    seen = []
    win.on_presence_toggle = seen.append
    win._presence_btn.click()
    assert seen == [False]
    win._presence_btn.click()
    assert seen == [False, True]


# ── JarvisLive side: live apply ───────────────────────────────────────────

class _UiStub:
    def __init__(self):
        self.muted = False
        self.logs = []
        self.states = []

    def write_log(self, text):
        self.logs.append(text)

    def set_state(self, state):
        self.states.append(state)


def _live():
    import main
    live = main.JarvisLive.__new__(main.JarvisLive)
    live.ui = _UiStub()
    live._presence_monitor = None
    live.session = None
    return live


def test_toggle_on_builds_registers_and_reflects_state(cfg):
    live = _live()
    live._ui_presence_toggle(True)
    assert live._presence_monitor is not None
    assert presence.get_monitor() is live._presence_monitor
    assert presence.is_present() is True
    live._presence_monitor.present = False       # an absence lands
    assert presence.is_present() is False         # registry reflects it
    assert any("Presence on" in line for line in live.ui.logs)


def test_toggle_off_unregisters_restores_hud_and_logs(cfg):
    live = _live()
    live._ui_presence_toggle(True)
    live._presence_monitor.present = False       # away: HUD was napped
    live._ui_presence_toggle(False)
    assert live._presence_monitor is None
    assert presence.get_monitor() is None
    assert presence.is_present() is True          # fail-open, no monitor
    assert live.ui.states == ["LISTENING"]        # napped HUD restored
    assert any("Presence off" in line for line in live.ui.logs)


def test_toggle_off_while_muted_keeps_hud_but_still_unregisters(cfg):
    live = _live()
    live._ui_presence_toggle(True)
    live._presence_monitor.present = False
    live.ui.muted = True
    live._ui_presence_toggle(False)
    assert live._presence_monitor is None
    assert live.ui.states == []                   # muted: HUD untouched
    assert any("Presence off" in line for line in live.ui.logs)


def test_toggle_off_with_no_monitor_still_logs(cfg):
    live = _live()
    live._ui_presence_toggle(False)
    assert live._presence_monitor is None
    assert any("Presence off" in line for line in live.ui.logs)


def test_toggle_on_with_existing_monitor_reenables_it(cfg):
    live = _live()
    mon = presence.PresenceMonitor(enabled=False)
    presence.set_monitor(mon)
    live._presence_monitor = mon
    live._ui_presence_toggle(True)
    assert mon.enabled is True
    assert any(line == "SYS: Presence on." for line in live.ui.logs)


def test_busy_probe_counts_a_live_session_as_present_evidence(cfg):
    """Voice input never resets the Windows idle clock, so a live
    conversation session must veto an idle absence on its own —
    without one, she napped mid-conversation (live witness
    2026-10-24)."""
    import threading
    live = _live()
    live._speaking_lock = threading.Lock()
    live._is_speaking = False
    assert live._presence_busy() is False
    live.session = object()      # he is mid-conversation with her
    assert live._presence_busy() is True
