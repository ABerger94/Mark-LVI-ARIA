"""core/presence.py — Presence v1.

Everything runs on fake signals and fake clocks: no Windows calls,
no sleeps, no threads. The tests pin the spec's contract — the
away/return transitions with their thresholds and vetoes, the
greet rule (30+ min away, never inside quiet hours), callbacks
firing exactly once per transition, the fail-open registry, and a
disabled monitor being completely inert.
"""
import pytest

from core import presence


class FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


class FakeWorld:
    """The mutable world the fake signals read."""

    def __init__(self):
        self.idle = 0.0
        self.locked = False
        self.media = False
        self.busy = False
        self.hour = 14


@pytest.fixture(autouse=True)
def _registry_clean():
    presence.set_monitor(None)
    yield
    presence.set_monitor(None)


@pytest.fixture
def rig():
    world = FakeWorld()
    clock = FakeClock()
    events = {"away": [], "return": []}
    monitor = presence.PresenceMonitor(
        idle_signal=lambda: world.idle,
        locked_signal=lambda: world.locked,
        media_signal=lambda: world.media,
        busy_probe=lambda: world.busy,
        monotonic=clock,
        hour_fn=lambda: world.hour,
        idle_after_s=600,
        greet_after_s=1800,
        quiet_hours=(23, 7),
        enabled=True,
        on_away=lambda reason: events["away"].append(reason),
        on_return=lambda away_s, greet: events["return"].append(
            (away_s, greet)),
    )
    return world, clock, events, monitor


def test_idle_reaching_threshold_goes_away(rig):
    world, clock, events, monitor = rig
    world.idle = 599
    monitor.tick()
    assert monitor.present
    world.idle = 600
    monitor.tick()
    assert not monitor.present
    assert events["away"] == ["idle"]


def test_activity_before_threshold_stays_present(rig):
    world, clock, events, monitor = rig
    world.idle = 590
    monitor.tick()
    world.idle = 5            # he typed something: the clock reset
    clock.advance(300)
    monitor.tick()
    assert monitor.present
    assert events["away"] == []


def test_media_veto_blocks_away(rig):
    world, clock, events, monitor = rig
    world.idle = 5000
    world.media = True        # a film is playing: he may be watching
    monitor.tick()
    assert monitor.present
    assert events["away"] == []


def test_busy_veto_blocks_away(rig):
    world, clock, events, monitor = rig
    world.idle = 5000
    world.busy = True         # she is speaking / mid-task
    monitor.tick()
    assert monitor.present
    assert events["away"] == []


def test_lock_goes_away_immediately(rig):
    world, clock, events, monitor = rig
    world.idle = 3            # nowhere near the idle threshold
    world.locked = True
    monitor.tick()
    assert not monitor.present
    assert events["away"] == ["lock"]


def test_away_fires_on_away_exactly_once(rig):
    world, clock, events, monitor = rig
    world.locked = True
    for _ in range(5):
        clock.advance(60)
        monitor.tick()
    assert events["away"] == ["lock"]
    assert events["return"] == []


def test_unlock_after_10_minutes_returns_without_greeting(rig):
    world, clock, events, monitor = rig
    world.locked = True
    monitor.tick()
    assert not monitor.present
    clock.advance(600)        # 10 min away: under the greet bar
    world.locked = False
    monitor.tick()
    assert monitor.present
    assert events["return"] == [(600.0, False)]


def test_return_after_45_minutes_greets(rig):
    world, clock, events, monitor = rig
    world.idle = 600
    monitor.tick()
    assert not monitor.present
    clock.advance(2700)       # 45 min away
    world.idle = 0            # fresh input: he sat back down
    monitor.tick()
    assert monitor.present
    assert events["return"] == [(2700.0, True)]


def test_return_inside_quiet_hours_never_greets(rig):
    world, clock, events, monitor = rig
    world.hour = 23           # 23:xx is inside quiet hours (23->7)
    world.idle = 600
    monitor.tick()
    clock.advance(2700)       # long enough to greet, but it's late
    world.idle = 0
    monitor.tick()
    assert monitor.present
    assert events["return"] == [(2700.0, False)]


def test_is_present_defaults_true_with_no_monitor():
    assert presence.get_monitor() is None
    assert presence.is_present() is True


def test_registry_reflects_the_monitor(rig):
    world, clock, events, monitor = rig
    presence.set_monitor(monitor)
    assert presence.is_present() is True
    world.locked = True
    monitor.tick()
    assert presence.is_present() is False


def test_disabled_monitor_is_inert():
    world = FakeWorld()
    clock = FakeClock()
    fired = []
    monitor = presence.PresenceMonitor(
        idle_signal=lambda: 99999,
        locked_signal=lambda: True,
        monotonic=clock,
        hour_fn=lambda: 14,
        enabled=False,
        on_away=lambda reason: fired.append(reason),
        on_return=lambda away_s, greet: fired.append("return"),
    )
    for _ in range(3):
        clock.advance(3600)
        monitor.tick()
    assert monitor.present
    assert fired == []


def test_broken_signals_fail_open():
    def boom():
        raise RuntimeError("probe exploded")

    monitor = presence.PresenceMonitor(
        idle_signal=boom, locked_signal=boom, media_signal=boom,
        busy_probe=boom, monotonic=FakeClock(), hour_fn=lambda: 14)
    monitor.tick()            # must not raise, must not go away
    assert monitor.present


def test_idle_away_does_not_flap_on_the_unlocked_level(rig):
    """Regression, live witness 2026-10-24: locked() reads False on
    EVERY tick of an unlocked machine, and the v1 AWAY branch read
    that level as 'he unlocked it' — so an idle absence ended on
    the very next tick, re-fired once the idle clock was still
    high, and the log filled with away/back pairs (away 0m). An
    idle absence must persist while the idle clock stays high,
    with on_return never firing."""
    world, clock, events, monitor = rig
    world.idle = 600
    monitor.tick()
    assert not monitor.present
    for _ in range(6):
        clock.advance(5)
        monitor.tick()
        assert not monitor.present
    assert events["away"] == ["idle"]
    assert events["return"] == []


def test_idle_away_ends_on_fresh_input_without_any_lock(rig):
    world, clock, events, monitor = rig
    world.idle = 900
    monitor.tick()
    assert not monitor.present
    clock.advance(120)
    monitor.tick()               # idle still high: stays away
    assert not monitor.present
    world.idle = 2               # a keystroke: the clock reset
    monitor.tick()
    assert monitor.present
    assert events["return"] == [(120.0, False)]


def test_idle_away_then_lock_then_unlock_returns(rig):
    world, clock, events, monitor = rig
    world.idle = 5000
    monitor.tick()
    assert not monitor.present
    world.locked = True          # he locks it on his way out
    monitor.tick()
    assert not monitor.present
    clock.advance(60)
    world.locked = False         # and unlocks it when he gets back
    monitor.tick()
    assert monitor.present
    assert len(events["return"]) == 1


def test_session_activity_needs_an_open_session_and_recent_speech():
    """The helper main's busy probe delegates to. Every one of
    these fails against the old rule — 'session open' alone —
    which counted an empty room as a conversation forever."""
    h = presence.session_counts_as_activity
    assert h(True, 30, 600) is True        # spoke half a minute ago
    assert h(True, 599, 600) is True       # still inside the window
    assert h(True, 600, 600) is False      # boundary: evidence expired
    assert h(True, 3600, 600) is False     # open session, hour of silence
    assert h(False, 0, 600) is False       # no session, however fresh
    assert h(False, 30, 600) is False
    assert h(True, None, 600) is False     # junk numbers are not activity
    assert h(True, 30, None) is False


def test_open_session_with_stale_speech_no_longer_vetoes_away():
    """Regression, live witness 2026-10-24: he walked away for
    over ten minutes with the Live session open and ARIA kept
    talking — the busy probe's bare session veto meant AWAY could
    never fire. The probe below is wired exactly as main wires it
    now: the helper over (session open, seconds since his last
    speech, the presence idle window)."""
    world = FakeWorld()
    clock = FakeClock()
    events = {"away": [], "return": []}
    session = {"open": True, "since_speech": 30.0}
    monitor = presence.PresenceMonitor(
        idle_signal=lambda: world.idle,
        locked_signal=lambda: world.locked,
        media_signal=lambda: world.media,
        busy_probe=lambda: presence.session_counts_as_activity(
            session["open"], session["since_speech"], 600),
        monotonic=clock,
        hour_fn=lambda: world.hour,
        idle_after_s=600,
        greet_after_s=1800,
        quiet_hours=(23, 7),
        enabled=True,
        on_away=lambda reason: events["away"].append(reason),
        on_return=lambda away_s, greet: events["return"].append(
            (away_s, greet)),
    )
    world.idle = 5000          # the Windows idle clock is deep…
    monitor.tick()
    assert monitor.present     # …but he spoke 30s ago: veto holds
    assert events["away"] == []
    session["since_speech"] = 3600.0   # an hour of silence since
    monitor.tick()
    assert not monitor.present
    assert events["away"] == ["idle"]


def test_main_wires_the_recency_helper_into_presence_busy():
    import inspect

    import main
    src = inspect.getsource(main)
    body = src[src.index("def _presence_busy"):
               src.index("def _presence_on_away")]
    assert "session_counts_as_activity" in body
    assert "_last_user_speech" in body
    assert "_presence_idle_s" in body
    # The window itself comes from the presence settings on start.
    start = src[src.index("def _start_presence"):
                src.index("async def _run_presence_watch")]
    assert 'self._presence_idle_s = settings["presence_idle_seconds"]' \
        in start


def test_greeting_for_is_time_aware_and_rotates():
    morning = {presence.greeting_for(9, 2000, d) for d in range(3)}
    assert len(morning) == 3                       # rotates by day
    assert all("morning" in g.lower() for g in morning)
    afternoon = presence.greeting_for(14, 2000, 0)
    assert "morning" not in afternoon.lower()
    long_away = presence.greeting_for(14, 4 * 3600, 0)
    assert "gone a while" in long_away or "long time" in long_away \
        or "proper break" in long_away


# ── Unprompted-speech gate (2026-10-24 live witness) ────────────────────
# The state machine marked Alek away correctly and he still heard her:
# the proactive check-in engine watches his speech clock, so his
# absence is exactly what makes it fire — and it never consulted
# presence. These tests pin the gate on every background voice loop.

class _StubMonitor:
    def __init__(self, present):
        self.present = present


def _bare_live():
    import main
    return main.JarvisLive.__new__(main.JarvisLive)


def test_allows_speech_follows_the_registered_monitor():
    live = _bare_live()
    try:
        presence.set_monitor(_StubMonitor(True))
        assert live._presence_allows_speech() is True
        presence.set_monitor(_StubMonitor(False))
        assert live._presence_allows_speech() is False
    finally:
        presence.set_monitor(None)


def test_allows_speech_fail_open_without_a_monitor():
    live = _bare_live()
    presence.set_monitor(None)
    assert live._presence_allows_speech() is True


def test_every_background_voice_loop_consults_the_gate():
    import inspect

    import main
    for fn in ("_run_proactive_mode", "_run_system_monitor",
               "_run_background_monitor"):
        body = inspect.getsource(getattr(main.JarvisLive, fn))
        assert "_presence_allows_speech" in body, fn
