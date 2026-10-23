"""tests/test_power_intent.py — the computer power actions fire only on
the user's direct command.

The bug: after every code-watcher restart announcement ("My code changed
— restarting now"), the model called computer_settings' restart action
and the user had to cancel a "Restart this computer?" banner. The arm
gate in core/power_intent.py is what separates a command from a
coincidence of vocabulary: only the user's own typed, spoken or
phone-sent words can arm, computer_settings spends the arm before it
even asks for confirmation, and without an arm the answer is a refusal.
"""
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import actions.computer_settings as cs
from core import confirm, incidents, power_intent


@pytest.fixture(autouse=True)
def _clean_latch():
    power_intent._reset()
    yield
    power_intent._reset()


# ── The detector: whole phrases, the computer as the object ─────────────────

@pytest.mark.parametrize("text", [
    "restart my computer",
    "Restart the computer",
    "restart my pc",
    "restart the pc",
    "reboot my computer",
    "reboot the computer",
    "reboot my pc",
    "restart computer",
    "please restart my computer now",
    "can you restart the computer for me?",
])
def test_detect_restart(text):
    assert power_intent.detect_power_intent(text) == "restart"


@pytest.mark.parametrize("text", [
    "shut down my computer",
    "shutdown my computer",
    "shut down the computer",
    "shut my computer down",
    "shut down my pc",
    "turn off my computer",
    "turn off the computer",
    "turn my computer off",
    "power off my computer",
    "power off",
])
def test_detect_shutdown(text):
    assert power_intent.detect_power_intent(text) == "shutdown"


@pytest.mark.parametrize("text", [
    "restart yourself",                       # ARIA restarting is normal
    "restart the app",
    "restart your program",                   # -> restart_program, not the PC
    "restart your app",
    "restart yourself please",
    "restart your program now",
    "restart aria",
    "restart the program",
    "the restart worked",                     # 'restart' as a noun
    "restarting now",
    "restarting now to load the new version",
    "My code changed — restarting now to load the new version.",
    "restart the download",
    "restart the router",
    "turn off the lights",
    "shut down jarvis",                       # ends the session, not the PC
    "",
    "what time is it",
])
def test_detect_negative(text):
    assert power_intent.detect_power_intent(text) is None


# ── The latch: one-shot, expiring, per-action ────────────────────────────────

def test_consume_without_arm_is_false():
    assert power_intent.consume("restart") is False
    assert power_intent.armed("restart") is False


def test_arm_is_one_shot():
    power_intent.arm("restart")
    assert power_intent.armed("restart") is True
    assert power_intent.consume("restart") is True
    assert power_intent.consume("restart") is False
    assert power_intent.armed("restart") is False


def test_armed_peek_does_not_spend():
    power_intent.arm("shutdown")
    assert power_intent.armed("shutdown") is True
    assert power_intent.armed("shutdown") is True
    assert power_intent.consume("shutdown") is True


def test_ttl_expiry_with_injected_clock():
    power_intent.arm("restart", ttl_s=120, now=1000.0)
    assert power_intent.armed("restart", now=1119.0) is True
    assert power_intent.armed("restart", now=1121.0) is False
    assert power_intent.consume("restart", now=1121.0) is False


def test_actions_are_independent():
    power_intent.arm("restart")
    assert power_intent.consume("shutdown") is False
    assert power_intent.armed("restart") is True
    assert power_intent.consume("restart") is True


def test_unknown_action_is_ignored():
    power_intent.arm("toggle_wifi")
    assert power_intent.armed("toggle_wifi") is False
    assert power_intent.consume("toggle_wifi") is False


def test_note_user_text_arms_and_reports():
    assert power_intent.note_user_text("restart my computer") == "restart"
    assert power_intent.armed("restart") is True
    assert power_intent.note_user_text("the restart worked") is None
    assert power_intent.armed("shutdown") is False


def test_thread_safety_smoke():
    errors = []

    def hammer():
        try:
            for _ in range(200):
                power_intent.arm("restart")
                power_intent.consume("restart")
                power_intent.armed("shutdown")
                power_intent.note_user_text("turn off my computer")
        except Exception as e:  # pragma: no cover - failure path
            errors.append(e)

    threads = [threading.Thread(target=hammer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []


# ── Integration: computer_settings spends the arm before confirming ─────────

@pytest.fixture()
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(incidents, "INCIDENTS_PATH",
                        tmp_path / "incidents.jsonl")
    monkeypatch.setattr(cs, "_PYAUTOGUI", True)
    shown = []
    confirm.bind(show=lambda t, d: shown.append((t, d)),
                 hide=lambda: None,
                 log=lambda m: None)
    calls = []
    monkeypatch.setattr(cs.subprocess, "run",
                        lambda *a, **k: calls.append(a[0] if a else []))
    yield shown, calls
    if confirm.pending_title():
        confirm.resolve(False)
    confirm.bind(None, None, None)


def _wait_for(predicate, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return False


def _expected_argv(action):
    if cs._OS == "Windows":
        return ["shutdown", "/r" if action == "restart" else "/s", "/t", "10"]
    if cs._OS == "Darwin":
        verb = "restart" if action == "restart" else "shut down"
        return ["osascript", "-e",
                f'tell application "System Events" to {verb}']
    return ["systemctl", "reboot" if action == "restart" else "poweroff"]


def test_restart_without_arm_is_refused(gate):
    shown, calls = gate
    result = cs.computer_settings({"action": "restart"})
    assert "status=failed" in result
    assert "I only restart the computer when you ask me to directly." in result
    assert shown == []                       # no confirmation was requested
    assert confirm.pending_title() == ""
    assert calls == []                       # no subprocess ran


def test_shutdown_without_arm_is_refused(gate):
    shown, calls = gate
    result = cs.computer_settings({"action": "shutdown"})
    assert "status=failed" in result
    assert "I only shut down the computer when you ask me to directly." in result
    assert shown == []
    assert calls == []


def test_armed_restart_confirms_then_runs(gate):
    shown, calls = gate
    assert power_intent.note_user_text("restart my computer") == "restart"
    result = cs.computer_settings({"action": "restart"})
    assert "[CONFIRMATION_PENDING]" in result
    assert [t for t, _ in shown] == ["Restart this computer"]
    assert calls == []                       # nothing before the human says yes
    confirm.resolve(True)
    assert _wait_for(lambda: len(calls) == 1)
    assert calls[0] == _expected_argv("restart")


def test_armed_shutdown_confirms_then_runs(gate):
    shown, calls = gate
    assert power_intent.note_user_text("shut down my computer") == "shutdown"
    result = cs.computer_settings({"action": "shutdown"})
    assert "[CONFIRMATION_PENDING]" in result
    assert [t for t, _ in shown] == ["Shut this computer down"]
    confirm.resolve(True)
    assert _wait_for(lambda: len(calls) == 1)
    assert calls[0] == _expected_argv("shutdown")


def test_arm_is_spent_by_a_cancelled_attempt(gate):
    shown, calls = gate
    power_intent.note_user_text("restart my computer")
    result = cs.computer_settings({"action": "restart"})
    assert "[CONFIRMATION_PENDING]" in result
    confirm.resolve(False)                   # the user cancels the banner
    assert power_intent.armed("restart") is False
    again = cs.computer_settings({"action": "restart"})
    assert "status=failed" in again          # a fresh command is required
    assert len(shown) == 1
    assert calls == []


def test_expired_arm_is_refused(gate):
    shown, calls = gate
    power_intent.arm("restart", now=time.monotonic() - 121)
    result = cs.computer_settings({"action": "restart"})
    assert "status=failed" in result
    assert shown == []
    assert calls == []


def test_restart_arm_does_not_authorise_shutdown(gate):
    shown, calls = gate
    power_intent.note_user_text("restart my computer")
    result = cs.computer_settings({"action": "shutdown"})
    assert "status=failed" in result
    assert shown == []
    assert calls == []


def test_toggle_wifi_still_uses_confirmation_alone(gate):
    shown, calls = gate
    result = cs.computer_settings({"action": "toggle_wifi"})
    assert "[CONFIRMATION_PENDING]" in result
    assert [t for t, _ in shown] == ["Switch WiFi off or on"]
    confirm.resolve(False)


# ── Source pins: the seams and the enforcement point stay where they are ────

def test_main_arms_only_at_the_three_user_text_seams():
    src = (ROOT / "main.py").read_text(encoding="utf-8")
    assert src.count("power_intent.note_user_text(") == 3


def test_computer_settings_consumes_before_confirming():
    src = (ROOT / "actions" / "computer_settings.py").read_text(encoding="utf-8")
    consume_at = src.index("power_intent.consume(action)")
    request_at = src.index("return confirm.request(")
    assert consume_at < request_at
