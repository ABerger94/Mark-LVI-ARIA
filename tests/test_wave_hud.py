"""Waveform HUD centrepiece ("Resonance", Alek's ask, 2026-10-22) —
the third HUD style alongside the animated face and the reactor core.

What is pinned here, headlessly: "wave" is a valid persisted style
and invalid values still fall back to "face"; the Controls button
cycles face → core → wave → face, persisting each stop and labelling
the button for it; the canvas samples its smoothed level into a
capped rolling history every tick; emission rings spawn once per
upward 0.72 crossing, stay capped, and expire by phase age; and
_paint_wave_core / full paintEvent draw without error on an empty
history, on a filled history, speaking, thinking and muted. How it
LOOKS in motion is a witness item for Alek's eyes on his laptop — no
test here can prove that.
"""
import inspect

import pytest
from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

import ui as ui_module
from core import incidents
from memory import config_manager as cm

_APP = QApplication.instance() or QApplication([])


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    path = tmp_path / "api_keys.json"
    monkeypatch.setattr(cm, "CONFIG_FILE", path)
    monkeypatch.setattr(incidents, "INCIDENTS_PATH", tmp_path / "incidents.jsonl")
    monkeypatch.setattr(cm, "_recovery_note", None)
    yield path


def _canvas():
    c = ui_module.HudCanvas("", assistant_name="ARIA")
    c.hud_style = "wave"
    return c


def _drive_loud(c, ticks=8):
    for _ in range(ticks):
        c.set_audio_level(1.0)
        c._step()


# ── Config ─────────────────────────────────────────────────────────────

def test_wave_style_roundtrips_and_invalid_falls_back(cfg):
    assert cm.HUD_STYLES == ("face", "core", "wave")
    assert cm.get_hud_style() == "face"            # untouched config
    cm.save_hud_style("wave")
    assert cm.get_hud_style() == "wave"
    cm.save_hud_style("core")
    assert cm.get_hud_style() == "core"
    cm.save_hud_style("bogus")
    assert cm.get_hud_style() == "face"            # never persists junk
    cm.save_hud_style("wave")
    # A hand-corrupted stored value also reads back as the default.
    data = cm.load_api_keys()
    data["hud_style"] = "hologram"
    cm.write_config(data)
    assert cm.get_hud_style() == "face"


# ── The Controls button cycles all three ───────────────────────────────

class _Btn:
    def __init__(self):
        self.text = ""
        self.tip = ""

    def setText(self, t):
        self.text = t

    def setStyleSheet(self, _s):
        pass

    def setToolTip(self, t):
        self.tip = t


class _Hud:
    def __init__(self):
        self.hud_style = "face"
        self.updates = 0

    def update(self):
        self.updates += 1


class _Log:
    def __init__(self):
        self.lines = []

    def append_log(self, s):
        self.lines.append(s)


def _bare_window():
    # No __init__: a real MainWindow builds the whole app. The toggle and
    # the button refresh touch only _hud_btn / hud / _log, all stubbed.
    win = ui_module.MainWindow.__new__(ui_module.MainWindow)
    win._hud_btn = _Btn()
    win.hud = _Hud()
    win._log = _Log()
    return win


def test_toggle_cycles_face_core_wave_face(cfg):
    cm.save_hud_style("face")
    win = _bare_window()
    seen = []
    for _ in range(4):
        win._toggle_hud_style()
        seen.append((cm.get_hud_style(), win.hud.hud_style, win._hud_btn.text))
    assert [s[0] for s in seen] == ["core", "wave", "face", "core"]
    assert [s[1] for s in seen] == ["core", "wave", "face", "core"]
    assert "REACTOR CORE" in seen[0][2]
    assert "WAVEFORM CORE" in seen[1][2]
    assert "ANIMATED FACE" in seen[2][2]
    assert win._log.lines == [
        "SYS: HUD switched to the reactor core.",
        "SYS: HUD switched to the waveform core.",
        "SYS: HUD switched to the animated face.",
        "SYS: HUD switched to the reactor core.",
    ]


def test_refresh_button_labels_wave_and_names_next(cfg):
    cm.save_hud_style("wave")
    win = _bare_window()
    win._refresh_hud_btn()
    assert "WAVEFORM CORE" in win._hud_btn.text
    assert "animated head" in win._hud_btn.tip      # next in cycle is face
    cm.save_hud_style("core")
    win._refresh_hud_btn()
    assert "REACTOR CORE" in win._hud_btn.text
    assert "waveform core" in win._hud_btn.tip      # next is wave
    cm.save_hud_style("face")
    win._refresh_hud_btn()
    assert "ANIMATED FACE" in win._hud_btn.text
    assert "reactor core" in win._hud_btn.tip       # next is core


# ── Level history + emission rings ─────────────────────────────────────

def test_step_samples_history_and_caps_it():
    c = _canvas()
    assert len(c._amp_hist) == 0
    for _ in range(400):
        c._step()
    assert len(c._amp_hist) == 288                  # deque cap holds
    assert all(0.0 <= v <= 1.0 for v in c._amp_hist)


def test_no_emission_ring_at_low_levels():
    c = _canvas()
    for _ in range(30):
        c.set_audio_level(0.3)
        c._step()
    assert c._amp_disp < 0.72
    assert c._wave_rings == []


def test_emission_ring_spawns_once_per_crossing_and_expires():
    c = _canvas()
    _drive_loud(c)
    assert c._amp_disp > 0.72
    assert len(c._wave_rings) == 1                  # one crossing, one ring
    _drive_loud(c)                                  # still above: no respawn
    assert len(c._wave_rings) == 1
    # Fall silent, then go loud again: a second crossing, a second ring.
    for _ in range(40):
        c._step()
    assert c._amp_disp < 0.72
    _drive_loud(c)
    assert len(c._wave_rings) == 2
    # Expiry is by phase age (1.6 s of integrated phase).
    c._core_phase += 2.0
    c._step()
    assert c._wave_rings == []


def test_emission_rings_capped_at_six():
    c = _canvas()
    c._wave_rings = [c._core_phase] * 6
    _drive_loud(c)
    assert len(c._wave_rings) == 6


# ── Paint smoke ────────────────────────────────────────────────────────

def test_paint_wave_core_empty_history():
    c = _canvas()
    pm = QPixmap(900, 700)
    p = QPainter(pm)
    c._paint_wave_core(p, 450.0, 320.0, 240.0, 900.0, 640.0)
    p.end()


def test_paint_wave_core_speaking_thinking_muted():
    c = _canvas()
    _drive_loud(c, ticks=60)                        # fill some history loud
    pm = QPixmap(900, 700)
    for speaking, state, muted in ((True, "SPEAKING", False),
                                   (False, "THINKING", False),
                                   (False, "LISTENING", False),
                                   (False, "LISTENING", True)):
        c.speaking, c.state, c.muted = speaking, state, muted
        p = QPainter(pm)
        c._paint_wave_core(p, 450.0, 320.0, 240.0, 900.0, 640.0)
        p.end()


def test_paint_event_wave_branch_renders():
    c = _canvas()
    c.speaking = True
    _drive_loud(c, ticks=30)
    c.resize(900, 700)
    pm = QPixmap(900, 700)
    c.render(pm)                                    # full paintEvent path
    assert not pm.isNull()


def test_paint_event_has_wave_branch():
    src = inspect.getsource(ui_module.HudCanvas.paintEvent)
    assert 'self.hud_style == "wave"' in src
    assert "_paint_wave_core" in src
    # The neuron field is still painted before the wave centrepiece.
    assert src.index("_paint_neurons") < src.index("_paint_wave_core")
