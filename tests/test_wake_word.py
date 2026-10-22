"""Wake-word connect state (core/wake_word.py initial_awake).

Alek's ruling: "She should be awake upon program starting." The connect
block in main.py used to force her ASLEEP on every session connect in
wake-word mode, which also suppressed the startup briefing (its gate
requires _awake) for the whole launch. initial_awake is the pure decision
behind the fix:

  * wake word off — always awake;
  * first connect of the process — awake, even in wake-word mode;
  * a reconnect — preserve the current state, so an auto-sleep (or a
    conversation in progress) survives a session blip.

These tests pin the full truth table so the ruling can't quietly rot.
"""
import pytest

from core.wake_word import initial_awake


@pytest.mark.parametrize("first_session,current_awake", [
    (True, True), (True, False), (False, True), (False, False),
])
def test_wake_word_off_is_always_awake(first_session, current_awake):
    assert initial_awake(False, first_session, current_awake) is True


@pytest.mark.parametrize("current_awake", [True, False])
def test_first_connect_comes_up_awake_even_in_wake_mode(current_awake):
    assert initial_awake(True, True, current_awake) is True


def test_reconnect_preserves_awake():
    assert initial_awake(True, False, True) is True


def test_reconnect_preserves_asleep():
    # She auto-slept, then the session blipped — she must not wake up
    # (and start listening / talking) just because the transport hiccuped.
    assert initial_awake(True, False, False) is False
