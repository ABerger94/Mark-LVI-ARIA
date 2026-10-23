"""The desktop shortcut icon is the resonance waveform orb.

2026-10-23: Alek approved swapping the donor arc-reactor icon for the
waveform orb / resonance reactor. The shortcut builder renders the icon
itself (config/aria.ico, multi-resolution) and only when the file is
missing — so the old committed jarvis.ico both had to go and the name had
to change, or the new drawing code would never run on his machine."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ui

ROOT = Path(__file__).resolve().parent.parent


def test_build_aria_icon_writes_multires_ico(tmp_path):
    pytest = __import__("pytest")
    PIL = pytest.importorskip("PIL.Image")
    out = tmp_path / "aria.ico"
    assert ui.MainWindow._build_aria_icon(out) is True
    assert out.exists() and out.stat().st_size > 10_000
    im = PIL.open(out)
    sizes = set(im.info.get("sizes", []))
    for want in ((256, 256), (128, 128), (48, 48), (32, 32), (16, 16)):
        assert want in sizes, (want, sizes)
    frame = im.ico.getimage((256, 256))
    assert frame.size == (256, 256)


def test_build_aria_icon_is_deterministic(tmp_path):
    a, b = tmp_path / "a.ico", tmp_path / "b.ico"
    assert ui.MainWindow._build_aria_icon(a) is True
    assert ui.MainWindow._build_aria_icon(b) is True
    assert a.read_bytes() == b.read_bytes()


def test_shortcut_points_at_aria_ico_and_old_icon_is_gone():
    src = (ROOT / "ui.py").read_text(encoding="utf-8")
    assert '"config" / "aria.ico"' in src
    assert "_build_aria_icon" in src
    assert "jarvis.ico" not in src
    assert "_build_jarvis_icon" not in src
    assert not (ROOT / "config" / "jarvis.ico").exists()
