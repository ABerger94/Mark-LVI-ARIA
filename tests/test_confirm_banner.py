"""Confirm banner readability (Alek's report, 2026-10-24): he could not
read all of the text on the confirmation windows ARIA pops up when she
needs a task approved.

Two defects pinned here:

1. The JarvisUI facade truncated the text on its way to the screen --
   title cut at 120 characters, detail at 300 -- so a long command,
   path or message was silently sliced mid-sentence before the banner
   ever saw it. The facade must pass the text through whole; the
   banner owns presentation.

2. The banner was a fixed 430px wide with no height cap and no
   scrolling: a long detail in 8pt type either wrapped into a banner
   taller than the window (buttons pushed off the bottom) or was
   simply hard to read. Now the banner follows the window's size,
   the detail is 10pt, and title+detail live in a scroll area so the
   header and the CONFIRM/CANCEL buttons always stay on screen.

How it LOOKS on a real desktop is a witness item for Alek's eyes;
what is pinned here is that every character reaches the banner and
the banner never outgrows its window.
"""
import types

from PyQt6.QtWidgets import QApplication, QPushButton, QWidget

import ui as ui_module

_APP = QApplication.instance() or QApplication([])

LONG_DETAIL = ("Command: python deploy.py --target production "
               "--config C:\\Users\\alek\\very\\long\\path\\settings.json\n"
               "Working directory: E:\\ARIA\n"
               "Timeout: 120s\n"
               "This runs with the signed-in user's full permissions.\n"
               * 12) + "TAIL-MARKER-EVERY-CHARACTER-COUNTS"


_KEEP = []


def _parent(w=980, h=700):
    # Kept alive module-side: a parent whose Python wrapper dies takes
    # the banner (its child) with it, and the label checks then hit
    # deleted C++ objects instead of the widget under test.
    p = QWidget()
    p.resize(w, h)
    _KEEP.append(p)
    return p


class _Recorder:
    def __init__(self):
        self.calls = []

    def emit(self, *args):
        self.calls.append(args)


def test_facade_passes_full_text_through():
    facade = ui_module.JarvisUI.__new__(ui_module.JarvisUI)
    rec = _Recorder()
    facade._win = types.SimpleNamespace(_confirm_sig=rec)
    title = "T" * 200                      # old facade cut this at 120
    detail = "D" * 5000                    # old facade cut this at 300
    facade.show_confirm(title, detail)
    assert rec.calls == [(title, detail)]


def test_banner_keeps_whole_detail_and_title():
    banner = ui_module.ConfirmBanner("Approve this", LONG_DETAIL,
                                     parent=_parent())
    assert banner._detail_label is not None
    assert banner._detail_label.text() == LONG_DETAIL
    assert banner._detail_label.wordWrap()
    long_title = "A very long title " * 12
    banner2 = ui_module.ConfirmBanner(long_title, "short", parent=_parent())
    assert banner2._title_label.text() == long_title


def test_detail_font_is_readable_size():
    banner = ui_module.ConfirmBanner("t", "some detail", parent=_parent())
    assert banner._detail_label.font().pointSize() >= 10


def test_banner_never_outgrows_small_window():
    parent = _parent(820, 580)             # the HUD's minimum size
    banner = ui_module.ConfirmBanner("Run shell command", LONG_DETAIL,
                                     parent=parent)
    parent.show()
    banner.adjustSize()
    banner.show()
    _APP.processEvents()
    assert banner.width() <= parent.width()
    assert banner.height() <= parent.height()
    # The buttons live inside the banner's geometry, not below it.
    buttons = banner.findChildren(QPushButton)
    assert len(buttons) == 2
    for btn in buttons:
        assert btn.geometry().bottom() < banner.height()


def test_long_detail_scrolls_instead_of_clipping():
    parent = _parent(820, 580)
    banner = ui_module.ConfirmBanner("Run shell command", LONG_DETAIL,
                                     parent=parent)
    parent.show()
    banner.adjustSize()
    banner.show()
    _APP.processEvents()
    bar = banner._detail_scroll.verticalScrollBar()
    assert bar.maximum() > 0               # there is more text, reachable


def test_short_detail_banner_stays_compact():
    banner = ui_module.ConfirmBanner("Delete file?", "Path: E:\\a.txt",
                                     parent=_parent())
    banner.adjustSize()
    assert banner._detail_label.text() == "Path: E:\\a.txt"
    assert banner.height() < 300


def test_empty_detail_still_works():
    banner = ui_module.ConfirmBanner("Restart the computer?", "",
                                     parent=_parent())
    banner.adjustSize()
    assert banner._detail_label is None
    assert banner._title_label.text() == "Restart the computer?"
