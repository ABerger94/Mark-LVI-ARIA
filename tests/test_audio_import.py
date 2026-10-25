"""A machine with no working audio stack must not kill the app at import.

sounddevice initialises PortAudio at import on some builds; where no
audio server exists (a GitHub Actions runner has libportaudio but no
PulseAudio server) that raises PortAudioError. main.py and core/tts.py
imported sounddevice bare at module level, so `import main` itself
died — CI red on every push from 2026-10-23 while the identical
suites passed on every machine with working audio. The CI step's own
annotation named it: tests/test_identity.py, PortAudioError,
"PulseAudio_Initialize: Can't connect to server".

Pinned here: with a sounddevice whose import raises, `import main`
and `import core.tts` still succeed, and the error surfaces — clear
and honest — only when an audio path is actually touched.
"""
import subprocess
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

_FAKE_SD = textwrap.dedent(
    """
    class PortAudioError(Exception):
        pass

    raise PortAudioError("Error initializing PortAudio: "
                         "PulseAudio_Initialize: Can't connect to server")
    """
).lstrip()

_PROBE = textwrap.dedent(
    """
    import main
    import core.tts as tts

    for mod in (main, tts):
        try:
            mod.sd.InputStream
        except RuntimeError as e:
            assert "sounddevice unavailable" in str(e), str(e)
        else:
            raise AssertionError("the stub did not raise on audio use")
    print("IMPORT-OK")
    """
).lstrip()


def test_import_survives_broken_sounddevice(tmp_path):
    (tmp_path / "sounddevice.py").write_text(_FAKE_SD)
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": f"{tmp_path}:{ROOT}",
        "QT_QPA_PLATFORM": "offscreen",
        "HOME": str(tmp_path),
    }
    r = subprocess.run(
        [sys.executable, "-c", _PROBE],
        cwd=str(ROOT), env=env, capture_output=True, text=True, timeout=120,
    )
    assert r.returncode == 0, r.stderr[-3000:]
    assert "IMPORT-OK" in r.stdout


def test_real_sounddevice_still_imports():
    """The guard is a fallback, not a replacement — and which side
    of it a machine lands on is the machine's business, not this
    test's (the first version of this test asserted the real module
    unconditionally and failed on the CI runner for exactly that
    reason). The contract: where sounddevice works, main.sd IS the
    real module; where it cannot initialise, main.sd is the stub and
    touching it raises the one clear error. Either way, the import
    survived and the surface is honest."""
    import main

    try:
        main.sd.InputStream
    except RuntimeError as e:
        assert "sounddevice unavailable" in str(e)
    else:
        assert hasattr(main.sd, "RawOutputStream")
