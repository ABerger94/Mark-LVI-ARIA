"""Remote Access overlay (Alek's report, 2026-10-22) — two fixes pinned:

1. The connected line must use the assistant's actual name ("Phone
   connected — ARIA ready"), not the donor project's hardcoded JARVIS.
2. The Windows Firewall warning must hide the moment a phone connects:
   a connected phone disproves "your phone cannot reach this page", and
   Alek's bridge works while a stale block rule exists, so the warning
   and the CONNECTED banner were showing at the same time.

The warning still appears before any connection when block rules
genuinely exist — that failure (block rule, phone refused) is real and
invisible from inside the app, which is why the warning exists at all.
"""
import dashboard.server as server_module
from PyQt6.QtWidgets import QApplication

import ui as ui_module

_APP = QApplication.instance() or QApplication([])


def _overlay(monkey_rules=None, **kw):
    if monkey_rules is not None:
        orig = server_module.blocking_firewall_rules
        server_module.blocking_firewall_rules = lambda: monkey_rules
        try:
            return ui_module.RemoteKeyOverlay("https://example.test", "12345678", **kw)
        finally:
            server_module.blocking_firewall_rules = orig
    return ui_module.RemoteKeyOverlay("https://example.test", "12345678", **kw)


def test_no_block_rules_no_warning_and_connect_works():
    ov = _overlay(monkey_rules=[])
    assert ov._fw_warn is None
    ov.mark_connected()                      # must not raise without a warning
    assert ov._timer_lbl.text() == "Phone connected — ARIA ready"


def test_warning_shows_before_connect_and_hides_on_connect():
    ov = _overlay(monkey_rules=[("python.exe", "C:\\Python\\python.exe")])
    assert ov._fw_warn is not None
    assert not ov._fw_warn.isHidden()
    assert "Restart ARIA" in ov._fw_warn.text()
    ov.mark_connected()
    assert ov._fw_warn.isHidden()
    assert ov._timer_lbl.text() == "Phone connected — ARIA ready"


def test_connected_line_uses_configured_name():
    ov = _overlay(monkey_rules=[], assistant_name="MAX")
    ov.mark_connected()
    assert ov._timer_lbl.text() == "Phone connected — MAX ready"
