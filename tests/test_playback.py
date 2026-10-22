"""Playback isolation (2026-10-22) — the speaker's dedicated writer.

The failure being pinned shut is the one Alek reported live: "If I
use ARIA a lot, her speaking gets choppy and glitchy." The play loop
wrote through the event loop's default executor — the same pool that
carries all tool execution — so heavy tool use starved the device
buffer. core/playback.py's PlaybackWriter owns the stream on its own
thread instead.

These tests cover what is honestly testable headlessly, with fake
streams: batches land in order, a failing stream is re-opened once
and writing continues on the new stream, a slow write also triggers
the heal, flush drops unwritten batches, and close is clean. Whether
the choppiness is actually gone is a witness item for Alek's laptop —
no fake stream can prove that.
"""
import threading
import time

from core.playback import PlaybackWriter


class FakeStream:
    def __init__(self, fail_on_write=None, write_delay=0.0):
        self.writes = []
        self.fail_on_write = fail_on_write   # 1-based write number
        self.write_delay = write_delay
        self.latency = 0.05
        self.stopped = False
        self.closed = False
        self._n = 0

    def write(self, data):
        self._n += 1
        if self.fail_on_write is not None and self._n == self.fail_on_write:
            raise RuntimeError("device went away")
        if self.write_delay:
            time.sleep(self.write_delay)
        self.writes.append(bytes(data))

    def stop(self):
        self.stopped = True

    def close(self):
        self.closed = True


def _factory(streams, **kw):
    def open_stream(device):
        st = FakeStream(**kw)
        streams.append(st)
        return st
    return open_stream


def _wait_for(pred, timeout=5.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def test_batches_written_in_order_and_close_is_clean():
    streams = []
    w = PlaybackWriter(_factory(streams))
    w.start()
    for i in range(5):
        w.submit(f"batch-{i}".encode())
    assert _wait_for(lambda: len(streams[0].writes) == 5)
    w.close()
    assert streams[0].writes == [f"batch-{i}".encode() for i in range(5)]
    assert streams[0].stopped and streams[0].closed
    assert not w.alive
    assert w.error is None
    assert w.latency == 0.05     # read off the stream at open


def test_open_failure_on_chosen_device_falls_back_to_default():
    calls, fell_back, streams = [], [], []

    def open_stream(device):
        calls.append(device)
        if device == 7:
            raise RuntimeError("exclusive mode")
        st = FakeStream()
        streams.append(st)
        return st

    w = PlaybackWriter(open_stream, device=7,
                       on_fallback=fell_back.append)
    w.start()
    assert calls == [7, None]                    # chosen, then default
    assert len(fell_back) == 1                   # caller got to warn
    w.submit(b"x")
    assert _wait_for(lambda: streams[0].writes == [b"x"])
    w.close()


def test_open_failure_with_no_device_raises_from_start():
    def open_stream(device):
        raise RuntimeError("no audio at all")

    w = PlaybackWriter(open_stream, device=None)
    try:
        w.start()
    except RuntimeError as e:
        assert "no audio at all" in str(e)
    else:
        raise AssertionError("start() should have raised")
    assert not w.alive


def test_failing_stream_is_reopened_once_and_writing_continues():
    streams = []

    def open_stream(device):
        # Only the FIRST stream fails (on its second write).
        st = FakeStream(fail_on_write=2 if not streams else None)
        streams.append(st)
        return st

    w = PlaybackWriter(open_stream)
    w.start()
    w.submit(b"one")
    w.submit(b"two")      # this write raises on stream #1
    w.submit(b"three")    # must land on the re-opened stream #2
    assert _wait_for(lambda: len(streams) == 2
                     and streams[1].writes == [b"three"])
    assert streams[0].writes == [b"one"]         # failed batch dropped
    assert streams[0].closed                     # old stream closed
    assert w.error is None                       # healed, not dead
    w.close()


def test_slow_write_is_treated_as_degraded_and_reopened():
    streams = []

    def open_stream(device):
        st = FakeStream(write_delay=0.25 if not streams else 0.0)
        streams.append(st)
        return st

    w = PlaybackWriter(open_stream, slow_write_s=0.05)
    w.start()
    w.submit(b"slow")
    w.submit(b"next")
    assert _wait_for(lambda: len(streams) == 2
                     and streams[1].writes == [b"next"])
    w.close()


def test_flush_drops_unwritten_batches_but_not_the_in_flight_one():
    gate = threading.Event()
    streams = []

    class GatedStream(FakeStream):
        def write(self, data):
            gate.wait(5)          # first write blocks until released
            super().write(data)

    def open_stream(device):
        st = GatedStream()
        streams.append(st)
        return st

    w = PlaybackWriter(open_stream)
    w.start()
    w.submit(b"in-flight")
    # Give the writer a moment to pick the batch up and block in write.
    assert _wait_for(lambda: w._q.empty())
    w.submit(b"queued-1")
    w.submit(b"queued-2")
    w.flush()                                  # barge-in
    gate.set()
    time.sleep(0.3)
    assert streams[0].writes == [b"in-flight"]  # queued ones never wrote
    w.close()


def test_close_discards_pending_and_exits():
    streams = []
    w = PlaybackWriter(_factory(streams))
    w.start()
    w.submit(b"a")
    assert _wait_for(lambda: streams[0].writes == [b"a"])
    w.close()
    assert not w.alive
    w.submit(b"late")          # after close: a no-op, never a crash
    assert streams[0].writes == [b"a"]
