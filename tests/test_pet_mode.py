"""Desktop pet mode (Alek's ask, 2026-10-22) — a small frameless pet
window that IS ARIA in miniature, in two switchable forms: the robot
character ("bot") and her waveform orb ("orb").

What is pinned here, headlessly: the pet window's flags (frameless,
always-on-top, Tool, translucent, never focus-stealing); the persisted
form/position/sticky-mode config (round-trips, junk falling back
safely); the right-click menu carrying BOTH checkable form actions,
with switching through the menu applying to the canvas and
persisting; the pure helpers (state priority, drag-vs-click
classifier, position clamp, idle repaint throttle); both painters
drawing every state without error; entering pet mode setting the
sticky flag and leaving it clearing it; the boot helper re-entering
only when the flag is set; and a pet construction failure leaving
the main window visible. How she LOOKS floating on a real desktop
is a witness item for Alek's eyes — no test here can prove that.
"""
import inspect

import pytest
from PyQt6.QtCore import Qt
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


class _HudStub:
    _amp_disp = 0.0


def _bare_main():
    """A MainWindow shell (the test_fleet pattern): the C++ object
    exists so show/hide work, but none of the heavyweight __init__
    ran — pet methods only touch the attributes set here."""
    win = ui_module.MainWindow.__new__(ui_module.MainWindow)
    ui_module.QMainWindow.__init__(win)
    win._pet_window = None
    win._confirm_overlay = None
    win._muted = False
    # What the window's own event handlers consult on show/resize
    # (an exception inside a Qt event handler is fatal, so the shell
    # carries the attributes and widgets they touch, all inert).
    win._overlay = None
    win._remote_overlay = None
    win._customize_overlay = None
    win._video_item = None
    win._video_widget = None
    win.setCentralWidget(ui_module.QWidget())
    win._cam_preview = ui_module.QWidget(win)
    return win


def _owner():
    win = _bare_main()
    win.hud = _HudStub()
    win._wake_state = lambda: {"enabled": False, "awake": True, "ready": False}
    win._tap_wake_manual = lambda: None
    return win


# ── Config ─────────────────────────────────────────────────────────

def test_pet_form_roundtrips_and_invalid_falls_back(cfg):
    assert cm.PET_FORMS == ("bot", "orb")
    assert cm.get_pet_form() == "bot"            # Alek's pick is the default
    cm.save_pet_form("orb")
    assert cm.get_pet_form() == "orb"
    cm.save_pet_form("bot")
    assert cm.get_pet_form() == "bot"
    cm.save_pet_form("junk")
    assert cm.get_pet_form() == "bot"            # never persists junk


def test_pet_pos_roundtrips_and_junk_is_ignored(cfg):
    assert cm.get_pet_pos() is None              # never parked yet
    cm.save_pet_pos([10, 20])
    assert cm.get_pet_pos() == [10, 20]
    cm.save_pet_pos(["x", "y"])
    assert cm.get_pet_pos() == [10, 20]          # junk write ignored
    cm.save_pet_pos(None)
    assert cm.get_pet_pos() == [10, 20]


def test_pet_mode_flag_roundtrips(cfg):
    assert cm.get_pet_mode_enabled() is False
    cm.save_pet_mode_enabled(True)
    assert cm.get_pet_mode_enabled() is True
    cm.save_pet_mode_enabled(False)
    assert cm.get_pet_mode_enabled() is False


# ── Pure helpers ───────────────────────────────────────────────────

def test_pet_state_priority():
    f = ui_module.pet_state_for
    assert f(alert=True, amp=1.0, busy=True, awake=False) == "alert"
    assert f(amp=0.5, busy=True, awake=False) == "speaking"
    assert f(amp=0.0, busy=True, awake=False) == "thinking"
    assert f(amp=0.0, busy=False, awake=False) == "sleepy"
    assert f(awake=True, happy=True) == "happy"
    assert f(awake=True) == "listening"
    # The speak threshold is the HUD's: at it, she is not yet speaking.
    assert f(amp=ui_module.PET_SPEAK_THRESHOLD, awake=True) == "listening"
    assert f(amp=ui_module.PET_SPEAK_THRESHOLD + 0.01, awake=True) == "speaking"


def test_drag_tracker_classifies_click_vs_drag():
    d = ui_module.PetDragTracker()
    d.press(100, 100)
    assert d.move(103, 102) is False
    assert d.release(103, 102) == "click"
    d.press(100, 100)
    assert d.move(120, 100) is True
    assert d.release(120, 100) == "drag"
    d.press(0, 0)
    assert d.release(50, 0) == "drag"            # far release, no move events


def test_clamp_pet_pos():
    f = ui_module.clamp_pet_pos
    assert f(10, 10, 240, 260, 1920, 1080) == (10, 10)
    assert f(-5, -50, 240, 260, 1920, 1080) == (0, 0)
    assert f(5000, 5000, 240, 260, 1920, 1080) == (1680, 820)
    assert f(100, 100, 240, 260, 200, 200) == (0, 0)   # window > screen


def test_idle_repaint_throttle():
    f = ui_module.pet_should_repaint
    assert f("speaking", 0.0, 0) is True
    assert f("thinking", 0.0, 0) is True
    assert f("alert", 0.0, 0) is True
    assert f("listening", 0.0, 1) is False
    assert f("listening", 0.0, ui_module.PET_IDLE_REPAINT_EVERY) is True
    assert f("listening", 0.5, 0) is True         # real level movement
    assert f("sleepy", 0.0, 1) is False


def test_canvas_advance_skip_counter():
    c = ui_module.PetCanvas()
    assert c.advance(0.0, "listening", False) is True    # first tick paints
    assert c.advance(0.0, "listening", False) is False
    assert c.skipped_frames == 1
    assert c.advance(0.0, "listening", False) is False
    assert c.advance(0.0, "listening", False) is True   # Nth tick repaints
    assert c.advance(0.0, "speaking", False) is True    # animated: always
    assert c.advance(0.0, "sleepy", False) is True      # state change paints


# ── The window ─────────────────────────────────────────────────────

def test_pet_window_flags_and_size(cfg):
    pet = ui_module.PetWindow(_owner())
    flags = pet.windowFlags()
    assert flags & Qt.WindowType.FramelessWindowHint
    assert flags & Qt.WindowType.WindowStaysOnTopHint
    assert flags & Qt.WindowType.Tool
    assert pet.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert pet.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert pet.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert (pet.width(), pet.height()) == ui_module.PET_SIZE
    assert pet.canvas.form == "bot"              # config default rides in


def test_pet_show_at_saved_pos(cfg):
    pet = ui_module.PetWindow(_owner())
    pet.show_at_saved_pos()
    assert pet.isVisible()
    scr = QApplication.primaryScreen().availableGeometry()
    assert scr.contains(pet.frameGeometry())     # fully on screen
    pet.hide()
    cm.save_pet_pos([12, 14])
    pet.show_at_saved_pos()
    assert (pet.x(), pet.y()) == (12, 14)
    pet.hide()


def test_canvas_paints_both_forms_all_states():
    c = ui_module.PetCanvas()
    c.resize(*ui_module.PET_SIZE)
    for form in ("bot", "orb"):
        c.form = form
        for st in ("listening", "speaking", "thinking",
                   "sleepy", "happy", "alert"):
            c.alert = st == "alert"
            for amp in (0.0, 0.5, 1.0):
                c.advance(amp, st, c.alert)
                pix = QPixmap(c.size())
                pix.fill(Qt.GlobalColor.transparent)
                p = QPainter(pix)
                if form == "orb":
                    c._paint_orb(p)
                else:
                    c._paint_bot(p)
                p.end()


# ── The right-click menu: form switching lives here ────────────────

def test_menu_carries_both_form_actions_and_switching_persists(cfg):
    pet = ui_module.PetWindow(_owner())
    menu = pet._build_menu()
    texts = [a.text() for a in menu.actions()]
    assert "Open ARIA" in texts
    assert "Pet form: Character" in texts
    assert "Pet form: Waveform orb" in texts
    assert "Hide pet" in texts
    # The current form is the checked one.
    assert pet._act_form_bot.isCheckable() and pet._act_form_bot.isChecked()
    assert pet._act_form_orb.isCheckable() and not pet._act_form_orb.isChecked()
    # Switching THROUGH THE MENU applies to the canvas and persists.
    pet._act_form_orb.trigger()
    assert pet.canvas.form == "orb"
    assert cm.get_pet_form() == "orb"
    menu2 = pet._build_menu()
    assert pet._act_form_orb.isChecked()
    assert not pet._act_form_bot.isChecked()
    pet._act_form_bot.trigger()
    assert pet.canvas.form == "bot"
    assert cm.get_pet_form() == "bot"


def test_set_form_persists_and_repaints(cfg):
    pet = ui_module.PetWindow(_owner())
    pet.set_form("orb")
    assert pet.canvas.form == "orb"
    assert cm.get_pet_form() == "orb"
    pet.set_form("nonsense")
    assert pet.canvas.form == "bot"              # junk falls back, like config


# ── Click behaviour ────────────────────────────────────────────────

def test_pet_click_asleep_wakes_via_wake_now_path(cfg):
    owner = _owner()
    calls = []
    owner._wake_state = lambda: {"enabled": True, "awake": False, "ready": True}
    owner._tap_wake_manual = lambda: calls.append("tap")
    pet = ui_module.PetWindow(owner)
    pet._handle_click()
    assert calls == ["tap"]                      # the WAKE NOW path itself


def test_pet_click_awake_raises_main_window_pet_stays(cfg):
    owner = _owner()
    pet = ui_module.PetWindow(owner)
    pet.show()
    pet._handle_click()
    assert owner.isVisible()                     # main window raised…
    assert pet.isVisible()                       # …and the pet stays put
    pet.hide()


# ── Entering / exiting pet mode, and the sticky flag ───────────────

class _FakePet:
    def __init__(self, owner):
        self.owner = owner
        self.shown = False
        self.hidden = False

    def show_at_saved_pos(self):
        self.shown = True

    def hide(self):
        self.hidden = True


def test_pet_mode_button_source_pin():
    src = inspect.getsource(ui_module.MainWindow._build_quick_drawer)
    assert "PET MODE" in src
    assert "_enter_pet_mode" in src
    assert src.index("AUTO-START") < src.index("PET MODE")   # right after it
    init_src = inspect.getsource(ui_module.MainWindow.__init__)
    assert "_maybe_enter_pet_at_boot" in init_src            # boot hook wired


def test_enter_exit_pet_mode_drives_sticky_flag(cfg, monkeypatch):
    monkeypatch.setattr(ui_module, "PetWindow", _FakePet)
    win = _bare_main()
    win.show()
    win._enter_pet_mode()
    assert isinstance(win._pet_window, _FakePet)
    assert win._pet_window.shown
    assert cm.get_pet_mode_enabled() is True     # sticky: set on entry
    assert win.isVisible() is False              # main window gave way
    win._exit_pet_mode()
    assert cm.get_pet_mode_enabled() is False    # …and cleared on exit
    assert win._pet_window.hidden
    assert win.isVisible() is True


def test_boot_helper_reenters_only_when_flag_set(cfg, monkeypatch):
    win = _bare_main()
    entered = []
    win._enter_pet_mode = lambda: entered.append(True)
    monkeypatch.setattr(cm, "get_pet_mode_enabled", lambda: True)
    win._maybe_enter_pet_at_boot()
    assert entered == [True]
    monkeypatch.setattr(cm, "get_pet_mode_enabled", lambda: False)
    win._maybe_enter_pet_at_boot()
    assert entered == [True]                     # not entered again


def test_pet_construction_failure_leaves_main_visible(cfg, monkeypatch):
    win = _bare_main()
    win.show()
    assert win.isVisible()

    def _boom(self, parent=None):
        raise RuntimeError("canvas exploded")

    monkeypatch.setattr(ui_module.PetCanvas, "__init__", _boom)
    win._enter_pet_mode()                        # must not raise
    assert win._pet_window is None
    assert win.isVisible()                       # never boot into nothing
