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


# ── F3 cycles the HUD, same as the Controls button (Alek, 2026-10-22) ──

def test_f3_shortcut_wired_to_the_same_hud_toggle():
    # F4/F11 are QShortcuts built in MainWindow.__init__; F3 must ride the
    # SAME mechanism and land on the SAME callable the HUD button uses —
    # one cycle implementation, two triggers, nothing duplicated.
    init_src = inspect.getsource(ui_module.MainWindow.__init__)
    assert 'QShortcut(QKeySequence("F3"), self)' in init_src
    assert ".activated.connect(self._toggle_hud_style)" in init_src
    # The pre-existing shortcuts are untouched neighbours.
    assert 'QShortcut(QKeySequence("F4"), self)' in init_src
    assert 'QShortcut(QKeySequence("F11"), self)' in init_src

def test_f3_and_button_share_one_toggle_callable():
    # Both triggers name _toggle_hud_style; the cycle behaviour itself is
    # pinned by test_toggle_cycles_face_core_wave_face above.
    src = inspect.getsource(ui_module)
    assert src.count("connect(self._toggle_hud_style)") == 2


def test_footer_legend_lists_f3_with_f4_and_f11():
    from PyQt6.QtWidgets import QLabel
    win = ui_module.MainWindow.__new__(ui_module.MainWindow)
    footer = win._build_footer()
    texts = [lbl.text() for lbl in footer.findChildren(QLabel)]
    legend = [t for t in texts if "[F4]" in t]
    assert len(legend) == 1
    assert "[F3] HUD" in legend[0]
    assert "[F4] Mute" in legend[0]
    assert "[F11] Fullscreen" in legend[0]
    # Ascending key order, matching the order that was already there.
    assert legend[0].index("[F3]") < legend[0].index("[F4]") \
        < legend[0].index("[F11]")


# ── Comet heads on every rotating element (Alek, 2026-10-22) ───────
#
# Alek: "Add comet heads to all the rotating lines around the center
# [of] the waveform reactor hud display. Only one line has the comet
# heads its goofy looking." The rotating inventory of _paint_wave_core
# is: five sweeping-arc groups, the waveform ribbon (a closed loop —
# its head sits at the loop's angular origin), and the dual spike
# rings (one head per ring, at the first spike's angle). Emission
# rings only expand; the circles/graduations/name ring are static —
# no heads there. One shared helper draws every head.
#
# Alek's follow-up, same day: "There's one comet head that is going
# up and down, I don't like that. I just want them moving around the
# center." Every head therefore orbits on a FIXED radius — the arc's
# own radius, the ribbon's base radius, the spike rings' rest tip
# circles — and only its angle moves. The elements still bounce with
# the level; the heads never do. The amplitude-independence pin
# below is that ruling as a test.

import math

from PyQt6.QtGui import QColor

# The arc table as the painter holds it: (radius frac, span deg,
# direction, pen width). Duplicated here on purpose — the pin.
_ARC_TABLE = ((0.955, 118, +1, 2.0),
              (0.845, 82, -1, 1.3),
              (0.760, 150, +1, 1.6),
              (0.660, 64, -1, 1.1),
              (0.545, 128, +1, 1.2))


def _record_heads(c, monkeypatch):
    calls = []
    orig = ui_module.HudCanvas._draw_comet_head

    def spy(self, p, cx, cy, rad, phi, trail_dir, col, scale, mix):
        calls.append({"rad": rad, "phi": phi, "trail_dir": trail_dir,
                      "scale": scale})
        return orig(self, p, cx, cy, rad, phi, trail_dir, col, scale,
                    mix)

    monkeypatch.setattr(ui_module.HudCanvas, "_draw_comet_head", spy)
    pm = QPixmap(900, 700)
    p = QPainter(pm)
    c._paint_wave_core(p, 450.0, 320.0, 240.0, 900.0, 640.0)
    p.end()
    return calls


def test_every_rotating_element_gets_one_comet_head(monkeypatch):
    c = _canvas()
    c._core_phase = 2.0
    c.speaking, c.state, c.muted = True, "SPEAKING", False
    calls = _record_heads(c, monkeypatch)
    # 5 arc groups + ribbon + outer spike ring + inner spike ring.
    assert len(calls) == 8

    t, r = 2.0, 240.0
    rate = 1.0 + 1.2                                   # speaking
    for k, (rr, span, dirn, wid) in enumerate(_ARC_TABLE):
        call = calls[k]
        base = (t * rate * (9 + k * 6) * dirn) % 360.0
        assert call["rad"] == pytest.approx(r * rr)
        assert call["scale"] == pytest.approx(max(0.6, wid / 2.0))
        if dirn > 0:
            # Travels toward decreasing phi, leads with (start+span),
            # trail streams toward increasing phi — behind it.
            assert call["phi"] == pytest.approx(
                -math.radians(base + span))
            assert call["trail_dir"] == +1
        else:
            # Travels toward increasing phi, leads with its start,
            # trail streams toward decreasing phi — behind it.
            assert call["phi"] == pytest.approx(-math.radians(base))
            assert call["trail_dir"] == -1

    # Ribbon: head at the angular origin (phi == rot), rotating toward
    # increasing angle, so the trail streams toward decreasing angle.
    # Its radius is the ribbon's FIXED base radius — not _off(0), which
    # made it bob with the level (Alek's "going up and down").
    rot = t * 0.25
    ribbon = calls[5]
    assert ribbon["phi"] == pytest.approx(rot)
    assert ribbon["trail_dir"] == -1
    assert ribbon["scale"] == pytest.approx(0.85)
    assert ribbon["rad"] == pytest.approx(r * 0.46)

    # Spike rings: heads at the first spike's angle (phi == ring_off)
    # on each ring's FIXED rest tip circle — r0 + the zero-level spike
    # height (the idle constant 0.018, shimmer averaged out), r1 less
    # the same at the inner ring's 0.7 scale. Not bin 0's live height.
    # The rings rotate toward decreasing angle, so trails stream
    # toward increasing.
    ring_off = -rot * 0.6
    outer, inner = calls[6], calls[7]
    assert outer["phi"] == pytest.approx(ring_off)
    assert outer["trail_dir"] == +1
    assert outer["scale"] == pytest.approx(0.7)
    assert outer["rad"] == pytest.approx(
        r * 0.46 + r * (0.085 + 0.018))
    assert inner["phi"] == pytest.approx(ring_off)
    assert inner["trail_dir"] == +1
    assert inner["scale"] == pytest.approx(0.6)
    assert inner["rad"] == pytest.approx(
        r * 0.46 - r * (0.085 + 0.018 * 0.7))


def test_no_lone_head_condition_left_in_painter():
    src = inspect.getsource(ui_module.HudCanvas._paint_wave_core)
    assert "k == 0 and sgm == 0" not in src
    # One helper call site per element family: arcs, ribbon, 2 rings.
    assert src.count("self._draw_comet_head") == 4
    # The amplitude couplings Alek rejected stay out: no head radius
    # may be built from the live waveform offset or a live spike tip.
    assert "base_r + _off(0.0)" not in src
    assert "tip_out = r0 + h_out" not in src
    assert "tip_in = r1 - h_in" not in src
    # The pinned rest tip circles are what's there instead.
    assert "tip_out = base_r + r * (0.085 + 0.018)" in src
    assert "tip_in = base_r - r * (0.085 + 0.018 * 0.7)" in src


def test_comet_head_radii_ignore_amplitude(monkeypatch):
    # Alek's ruling as a test: the SAME angular state painted silent
    # and loud must put every one of the 8 heads on the SAME radius —
    # heads orbit, they never ride the level up and down. Angles are
    # free to differ; radii are not.
    r = 240.0

    def _state(phase):
        c = _canvas()
        c._core_phase = phase
        c.speaking, c.state, c.muted = True, "SPEAKING", False
        return c

    # One spy for all three paints (a second _record_heads spy would
    # wrap the first and double-count); each paint gets its own list.
    paints = []
    orig = ui_module.HudCanvas._draw_comet_head

    def spy(self, p, cx, cy, rad, phi, trail_dir, col, scale, mix):
        paints[-1].append({"rad": rad, "phi": phi,
                           "trail_dir": trail_dir, "scale": scale})
        return orig(self, p, cx, cy, rad, phi, trail_dir, col, scale,
                    mix)

    monkeypatch.setattr(ui_module.HudCanvas, "_draw_comet_head", spy)

    def paint(c):
        paints.append([])
        pm = QPixmap(900, 700)
        p = QPainter(pm)
        c._paint_wave_core(p, 450.0, 320.0, 240.0, 900.0, 640.0)
        p.end()
        return paints[-1]

    silent = _state(2.0)                               # empty history
    loud = _state(2.0)
    loud._amp_hist.extend(
        0.15 + 0.8 * abs(math.sin(i * 0.37)) for i in range(288))
    loud._amp_disp = 0.9

    quiet_calls = paint(silent)
    loud_calls = paint(loud)
    assert len(quiet_calls) == len(loud_calls) == 8
    for q, l in zip(quiet_calls, loud_calls):
        assert l["rad"] == pytest.approx(q["rad"])

    # And each radius is its fixed nominal value, at any phase.
    expected = [r * rr for rr, _s, _d, _w in _ARC_TABLE]
    expected += [r * 0.46,
                 r * 0.46 + r * (0.085 + 0.018),
                 r * 0.46 - r * (0.085 + 0.018 * 0.7)]
    for call, want in zip(loud_calls, expected):
        assert call["rad"] == pytest.approx(want)
    later = _state(5.0)                                # a later angle
    later._amp_hist.extend(loud._amp_hist)
    later._amp_disp = 0.9
    later_calls = paint(later)
    for call, want in zip(later_calls, expected):
        assert call["rad"] == pytest.approx(want)
    # The angle really did move — the pin above is about radii only.
    assert later_calls[5]["phi"] != pytest.approx(loud_calls[5]["phi"])


class _RecPainter:
    """Records what the comet helper draws: (pen width, pen alpha,
    p1, p2) per line and (centre, rx, brush alpha) per ellipse."""

    def __init__(self):
        self.lines = []
        self.ellipses = []
        self._pen = None
        self._brush = None

    def setPen(self, pen):
        self._pen = pen

    def setBrush(self, brush):
        self._brush = brush

    def drawLine(self, p1, p2):
        self.lines.append((self._pen.widthF(),
                           self._pen.color().alphaF(), p1, p2))

    def drawEllipse(self, center, rx, ry):
        self.ellipses.append((center, rx, self._brush.alphaF()))


def _mix(col, a):
    c = QColor(col)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


@pytest.mark.parametrize("trail_dir", (+1, -1))
def test_comet_head_trail_streams_behind_and_tapers(trail_dir):
    c = _canvas()
    rec = _RecPainter()
    phi = 0.3
    c._draw_comet_head(rec, 100.0, 100.0, 50.0, phi, trail_dir,
                       QColor(0, 212, 255), 1.0, _mix)
    assert len(rec.lines) == 7
    span = math.radians(30.0) * (0.55 + 0.45 * 1.0)
    angles = []
    for _w, _a, p1, p2 in rec.lines:
        for pt in (p1, p2):
            ang = math.atan2(pt.y() - 100.0, pt.x() - 100.0)
            # Every trail point lies on the trail_dir side of the head
            # and within one trail span of it — never ahead of it.
            rel = (ang - phi) * trail_dir
            assert -1e-9 <= rel <= span + 1e-9
            angles.append(ang)
    # The trail reaches (nearly) the full span behind the head.
    reach = max((a - phi) * trail_dir for a in angles)
    assert reach == pytest.approx(span, abs=1e-6)
    # Taper: widths and alphas strictly decrease away from the head.
    widths = [w for w, _a, _p1, _p2 in rec.lines]
    alphas = [a for _w, a, _p1, _p2 in rec.lines]
    assert all(x > y for x, y in zip(widths, widths[1:]))
    assert all(x > y for x, y in zip(alphas, alphas[1:]))
    # Glow, then the bright core dot, both centred on the head point.
    assert len(rec.ellipses) == 2
    hx, hy = 100.0 + 50.0 * math.cos(phi), 100.0 + 50.0 * math.sin(phi)
    (gc, grx, ga), (cc, crx, ca) = rec.ellipses
    assert (gc.x(), gc.y()) == (pytest.approx(hx), pytest.approx(hy))
    assert (cc.x(), cc.y()) == (pytest.approx(hx), pytest.approx(hy))
    assert grx == pytest.approx(8.0)
    assert ga == pytest.approx(0.16, abs=0.01)   # QColor alpha quantises
    assert crx == pytest.approx(3.6)
    assert ca == pytest.approx(0.95, abs=0.01)
