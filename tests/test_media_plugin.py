"""Media library plugin: matcher correctness against the real library shape.

The library is one folder per title and file names disagree with folder
names ("1917 (2017)" holds "1917 2019.mp4"), so the matcher is tested on
exactly those shapes. Playback (os.startfile) and the filesystem walk are
monkeypatched out; the scoring and picking logic runs for real.
"""
import importlib.util
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from core.plugin_loader import discover_plugins

_ROOT = os.path.join(os.path.dirname(__file__), "..")
_PLUGIN_PATH = os.path.join(_ROOT, "plugins", "media_player_search.py")

_spec = importlib.util.spec_from_file_location("media_player_search",
                                               _PLUGIN_PATH)
mps = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mps)

MOVIE = r"E:\media\movies\1917 (2017)\1917 2019.mp4"
LIBRARY = [
    (MOVIE, 2_100_000_000),
    (r"E:\media\movies\1917 (2017)\1917-trailer.mp4", 24_000_000),
    (r"E:\media\movies\Dune (2021)\Dune.2021.2160p.mp4", 9_400_000_000),
    (r"E:\media\movies\Dune (1984)\Dune (1984).mkv", 1_400_000_000),
    (r"E:\media\movies\It (2017)\It.2017.mp4", 1_800_000_000),
    (r"E:\media\movies\White Christmas (1954)\White Christmas.mp4",
     1_100_000_000),
    (r"E:\media\tv\Severance\Severance.S01E01.1080p.mkv", 2_600_000_000),
    (r"E:\media\tv\Severance\Severance.S01E02.1080p.mkv", 2_400_000_000),
]


def test_normalize():
    assert mps._normalize("1917 (2017)") == "1917 2017"
    assert mps._normalize("  Dune: Part-Two! ") == "dune part two"
    assert mps._normalize("") == ""


def test_pick_matches_folder_when_filename_disagrees():
    # The real 1917 case: folder says (2017), file says 2019.
    assert mps._pick(LIBRARY, "1917") == MOVIE
    assert mps._pick(LIBRARY, "1917 (2017)") == MOVIE


def test_pick_prefers_feature_over_trailer():
    assert mps._pick(LIBRARY, "1917") != LIBRARY[1][0]


def test_pick_short_title_does_not_match_inside_words():
    # "It" must find the It folder, not "White Christmas" (wh**it**e).
    assert mps._pick(LIBRARY, "It") == LIBRARY[4][0]


def test_pick_tv_show_by_series_name():
    got = mps._pick(LIBRARY, "Severance")
    assert got.startswith(r"E:\media\tv\Severance")


def test_pick_no_match_returns_empty():
    assert mps._pick(LIBRARY, "The Matrix") == ""
    assert mps._pick([], "1917") == ""


def test_run_plays_best_match_via_startfile(monkeypatch):
    opened = []
    monkeypatch.setattr(mps.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(mps, "_find_candidates", lambda roots: LIBRARY)
    monkeypatch.setattr(mps.os, "startfile",
                        lambda p: opened.append(p), raising=False)
    out = mps.run({"media_title": "1917"})
    assert opened == [MOVIE]
    assert "1917 2019.mp4" in out


def test_run_reports_no_match(monkeypatch):
    monkeypatch.setattr(mps.os.path, "isdir", lambda p: True)
    monkeypatch.setattr(mps, "_find_candidates", lambda roots: LIBRARY)
    out = mps.run({"media_title": "The Matrix"})
    assert "couldn't find" in out


def test_run_asks_when_title_missing():
    assert "Which movie or show" in mps.run({})
    assert "Which movie or show" in mps.run({"media_title": "  "})


def test_run_reports_unreachable_library(monkeypatch):
    monkeypatch.setattr(mps.os.path, "isdir", lambda p: False)
    out = mps.run({"media_title": "1917"})
    assert "can't reach" in out


def test_plugin_validates_through_real_loader(tmp_path):
    import shutil
    shutil.copy(_PLUGIN_PATH, tmp_path / "media_player_search.py")
    registry = discover_plugins(tmp_path, set(),
                                logger=lambda _msg: None)
    entry = next(e for e in registry.list_for_ui()
                 if e["file"] == "media_player_search.py")
    assert entry["valid"], entry["error"]
    assert entry["name"] == "media_player_search"
