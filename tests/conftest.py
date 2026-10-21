"""Pytest scaffolding for the LVI (ARIA) test floor.

Two jobs, both boring and both load-bearing:

  * Put the repo root on sys.path. The app is a flat tree of top-level
    packages (core/, memory/, providers/, ui.py, main.py) with no packaging
    metadata, so tests import them exactly the way main.py does — from the
    root. Without this, collection works only when pytest happens to be
    invoked from the right directory.

  * Force Qt offscreen BEFORE anything imports PyQt6. Several suites build
    real widgets (the providers self-test drives the real settings overlay);
    on a headless box Qt otherwise aborts at import/display time. setdefault
    so a developer with a real display can still override it.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
