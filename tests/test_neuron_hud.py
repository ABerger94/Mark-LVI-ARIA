"""Neuron HUD field (Alek's ask, 2026-10-22) — animated "neurons
firing" lines behind the reactor core / face display.

What is pinned here, headlessly: the graph geometry is precomputed,
deterministic, and keeps out of the centrepiece's disc; pulses spawn
sparsely under a hard cap, travel, and die; the paint path draws
without error; and paintEvent draws the field BEFORE the centrepiece
branch, which is what "behind" means on this canvas (the head and
the reactor are two branches of the same HudCanvas.paintEvent, so
one early draw sits behind both). How it LOOKS is a witness item for
Alek's eyes on his laptop — no test here can prove that.
"""
import inspect
import math

from PyQt6.QtGui import QPainter, QPixmap
from PyQt6.QtWidgets import QApplication

import ui as ui_module

_APP = QApplication.instance() or QApplication([])


def _canvas():
    return ui_module.HudCanvas("", assistant_name="ARIA")


def test_geometry_is_deterministic_and_keeps_out_of_the_centre():
    c = _canvas()
    a = c._make_neurons(1200, 800, 600.0, 360.0, 240.0)
    b = c._make_neurons(1200, 800, 600.0, 360.0, 240.0)
    assert a[0] == b[0] and a[1] == b[1]          # same network per size
    nodes, edges, lines = a
    assert len(nodes) >= 30
    assert edges and len(lines) == len(edges)
    for (x, y) in nodes:                          # outside the disc
        assert math.hypot(x - 600.0, y - 360.0) >= 240.0 - 1e-6
    for i, j, d in edges:                         # edges are local
        assert d <= min(1200, 800) * 0.30 + 1e-6
        assert math.isclose(
            d, math.hypot(nodes[i][0] - nodes[j][0],
                          nodes[i][1] - nodes[j][1]))


def test_paint_neurons_draws_and_caches():
    c = _canvas()
    pm = QPixmap(1200, 800)
    p = QPainter(pm)
    c._paint_neurons(p, 1200.0, 800.0, 600.0, 360.0, 240.0)
    p.end()
    assert c._neuron_key == (1200.0, 800.0)
    assert c._neuron_cache is not None
    first = c._neuron_cache
    p = QPainter(pm)
    c._paint_neurons(p, 1200.0, 800.0, 600.0, 360.0, 240.0)
    p.end()
    assert c._neuron_cache is first               # same size: no rebuild


def test_pulses_spawn_under_cap_travel_and_die():
    c = _canvas()
    c._neuron_cache = c._make_neurons(1200, 800, 600.0, 360.0, 240.0)
    c._neuron_key = (1200, 800)
    seen_any = False
    for _ in range(400):
        c._step_neurons(0.016)
        assert len(c._pulses) <= 7                # the cap holds
        seen_any = seen_any or bool(c._pulses)
    assert seen_any                               # firing actually happens
    # A pulse near the end of its edge dies on the next steps.
    c._pulses = [[0, 0.95, 1.0]]
    c._step_neurons(0.10)
    assert all(pl[1] < 1.0 for pl in c._pulses)
    c._step_neurons(0.10)
    assert [0, 0.95, 1.0] not in c._pulses
    # Muted tightens the cap.
    c.muted = True
    c._pulses = [[0, 0.1, 0.5]] * 3
    for _ in range(50):
        c._step_neurons(0.016)
        assert len(c._pulses) <= 3


def test_paint_event_draws_field_before_centrepiece():
    src = inspect.getsource(ui_module.HudCanvas.paintEvent)
    assert "_paint_neurons" in src
    assert src.index("_paint_neurons") < src.index("_paint_core")
    assert src.index("_paint_neurons") < src.index("_avatar.paint")
