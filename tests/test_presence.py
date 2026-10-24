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


def test_greeting_for_is_time_aware_and_rotates():
    morning = {presence.greeting_for(9, 2000, d) for d in range(3)}
    assert len(morning) == 3                       # rotates by day
    assert all("morning" in g.lower() for g in morning)
    afternoon = presence.greeting_for(14, 2000, 0)
    assert "morning" not in afternoon.lower()
    long_away = presence.greeting_for(14, 4 * 3600, 0)
    assert "gone a while" in long_away or "long time" in long_away \
        or "proper break" in long_away
