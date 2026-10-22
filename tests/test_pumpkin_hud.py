"""Jack-o'-lantern HUD centrepiece (Alek's ask, 2026-10-22) — the
fourth HUD style alongside the animated face, the reactor core and
the waveform core, ported from the approved prototype in
lvi-build/pumpkin_preview.py.

What is pinned here, headlessly: "pumpkin" is a valid persisted
style; the Controls button reaches it after "wave" and wraps to the
animated face, labelling the button for it and naming the next
style in its tooltip; the candle flicker is a clamped random walk
(0.80–1.05) that advances on every _step() tick — seeded, so this
is deterministic — for every style, not just while the pumpkin is
on screen; and _paint_pumpkin_core / full paintEvent draw without
error at silence and at full level, speaking, thinking and muted,
at full and small sizes. How it LOOKS in motion is a witness item
for Alek's eyes on his laptop — no test here can prove that.
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
    c.hud_style = "pumpkin"
    return c


def _drive_loud(c, ticks=8):
    for _ in range(ticks):
        c.set_audio_level(1.0)
        c._step()


# ── Config ─────────────────────────────────────────────────────────────

def test_pumpkin_style_roundtrips_and_invalid_falls_back(cfg):
    assert cm.HUD_STYLES == ("face", "core", "wave", "pumpkin")
    assert cm.get_hud_style() == "face"            # untouched config
    cm.save_hud_style("pumpkin")
    assert cm.get_hud_style() == "pumpkin"
    cm.save_hud_style("bogus")
    assert cm.get_hud_style() == "face"            # never persists junk


# ── The Controls button reaches it and wraps ───────────────────────────

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


def test_toggle_reaches_pumpkin_and_wraps_to_face(cfg):
    cm.save_hud_style("wave")
    win = _bare_window()
    win._toggle_hud_style()
    assert cm.get_hud_style() == "pumpkin"
    assert win.hud.hud_style == "pumpkin"
    assert "JACK-O'-LANTERN" in win._hud_btn.text
    win._toggle_hud_style()
    assert cm.get_hud_style() == "face"
    assert win.hud.hud_style == "face"
    assert win._log.lines == [
        "SYS: HUD switched to the jack-o'-lantern.",
        "SYS: HUD switched to the animated face.",
    ]


def test_refresh_button_labels_pumpkin_and_names_next(cfg):
    cm.save_hud_style("pumpkin")
    win = _bare_window()
    win._refresh_hud_btn()
    assert "JACK-O'-LANTERN" in win._hud_btn.text
    assert "🎃" in win._hud_btn.text
    assert "animated head" in win._hud_btn.tip      # next in cycle is face


# ── Candle flicker: state, stepped on the tick ─────────────────────────

def test_flicker_advances_on_step_and_stays_clamped():
    c = _canvas()
    assert c._pumpkin_flicker == 0.92               # seeded start
    seen = []
    for _ in range(120):
        c._step()
        seen.append(c._pumpkin_flicker)
    assert all(0.80 <= v <= 1.05 for v in seen)
    assert seen[0] != 0.92                          # the walk moved
    assert len(set(seen)) >= 20                     # and keeps moving


def test_flicker_clamp_holds_over_many_ticks():
    c = _canvas()
    for _ in range(3000):
        c._step()
    assert 0.80 <= c._pumpkin_flicker <= 1.05


def test_flicker_advances_for_other_styles_too():
    # Stepped for EVERY style (like the wave sampling): switching to
    # the pumpkin later never starts from a frozen candle.
    c = _canvas()
    c.hud_style = "core"
    vals = []
    for _ in range(60):
        c._step()
        vals.append(c._pumpkin_flicker)
    assert len(set(vals)) > 1
    assert all(0.80 <= v <= 1.05 for v in vals)


# ── Paint smoke ────────────────────────────────────────────────────────

def test_paint_pumpkin_core_at_silence():
    c = _canvas()
    assert c._amp_disp == 0.0                       # grin closed, glow low
    pm = QPixmap(900, 700)
    p = QPainter(pm)
    c._paint_pumpkin_core(p, 450.0, 320.0, 240.0, 900.0, 640.0)
    p.end()


def test_paint_pumpkin_core_at_full_level():
    c = _canvas()
    _drive_loud(c)
    assert c._amp_disp > 0.72                       # jaw wide open
    pm = QPixmap(900, 700)
    p = QPainter(pm)
    c._paint_pumpkin_core(p, 450.0, 320.0, 240.0, 900.0, 640.0)
    p.end()


def test_paint_pumpkin_core_speaking_thinking_muted():
    c = _canvas()
    _drive_loud(c, ticks=30)
    pm = QPixmap(900, 700)
    for speaking, state, muted in ((True, "SPEAKING", False),
                                   (False, "THINKING", False),
                                   (False, "LISTENING", False),
                                   (False, "LISTENING", True)):
        c.speaking, c.state, c.muted = speaking, state, muted
        p = QPainter(pm)
        c._paint_pumpkin_core(p, 450.0, 320.0, 240.0, 900.0, 640.0)
        p.end()


def test_paint_pumpkin_core_small_canvas():
    c = _canvas()
    pm = QPixmap(300, 300)
    p = QPainter(pm)
    c._paint_pumpkin_core(p, 150.0, 140.0, 90.0, 300.0, 260.0)
    p.end()


def test_paint_event_pumpkin_branch_renders():
    c = _canvas()
    c.speaking = True
    _drive_loud(c, ticks=30)
    c.resize(900, 700)
    pm = QPixmap(900, 700)
    c.render(pm)                                    # full paintEvent path
    assert not pm.isNull()


def test_paint_event_has_pumpkin_branch():
    src = inspect.getsource(ui_module.HudCanvas.paintEvent)
    assert 'self.hud_style == "pumpkin"' in src
    assert "_paint_pumpkin_core" in src
    # The neuron field is still painted before the pumpkin centrepiece.
    assert src.index("_paint_neurons") < src.index("_paint_pumpkin_core")
