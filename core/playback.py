"""
core/playback.py — the speaker gets its own thread, and nothing else
is allowed to make it wait.

THE FAILURE THIS FIXES (Alek, live, 2026-10-22)

    "If I use ARIA a lot, her speaking gets choppy and glitchy. It's
    inaudible after a point."

    _play_audio used to write each batch with
    `await asyncio.to_thread(stream.write, ...)` — the event loop's
    DEFAULT executor. That same pool carries essentially all tool
    execution plus dozens of other to_thread calls (scheduler ticks,
    monitors, memory saves, camera captures). The heavier a session's
    tool use, the longer each audio write queues behind unrelated
    work; a late write starves the device buffer, the device underruns,
    and the crackle compounds with use — exactly the reported shape.
    The stream was also opened once per session and never healed, so
    any device-side degradation lasted until a restart.

THE SHAPE OF THE FIX

    PlaybackWriter owns the RawOutputStream end-to-end on ONE
    dedicated thread: the stream is opened there, every write happens
    there, and it is closed there. The play coroutine hands it PCM
    batches through a bounded thread-safe queue and never touches the
    stream — or any executor — itself. Analysis (visemes, echo guard)
    runs in the coroutine AFTER the batch is submitted, so it can add
    latency to the mouth but never to the speaker.

    Self-healing: a write that raises, or one that takes longer than
    `slow_write_s` for a ≤200 ms batch, marks the stream degraded; the
    writer closes it and re-opens (same device, same fallback-to-
    default rule as the session's first open) before the next batch.
    The failed batch is dropped, not replayed — replaying stale speech
    late is worse than a skipped slice. If the re-open also fails, the
    writer records the error and exits; the play loop notices via
    `error`/`alive` and ends the way a failed write used to end it.

    Interrupt latency is preserved two ways: batches stay capped at
    ~200 ms by the caller (unchanged), and flush() drops every
    queued-but-unwritten batch, so a barge-in leaves at most the one
    in-flight batch sounding — the same bound the old design had.

    The queue is a deep jitter buffer (MAX_QUEUED batches, ~5 min of
    speech at the 200 ms cap) and submit() NEVER evicts queued speech.
    The first version of this module bounded the queue at 10 batches
    and evicted the OLDEST unwritten batch on overflow — a design
    error, proven by Alek's 2026-10-22 video of a startup briefing
    shredded into ~200 ms fragments with gaps from the first second:
    Gemini Live delivers speech in bursts faster than realtime, the
    10-batch queue saturated seconds into any long utterance, and
    from there roughly every other batch was discarded unwritten.
    The old design's blocking write was the flow control, and this
    buffer is what replaces it: speech queues up and plays out whole,
    however fast it arrives. The bound survives only as a
    pathological guard — if it is ever hit, submit() drops the
    INCOMING batch and counts it on `dropped`, rather than silently
    discarding speech already accepted.
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Callable, Optional

MAX_QUEUED = 1500       # batches; ~5 min of speech at the 200 ms cap.
                         # A pathological guard only — see the header.
_SLOW_WRITE_S = 2.0      # a ≤200 ms batch taking longer = degraded stream


class PlaybackWriter:
    """Owns one output stream on one dedicated thread.

        open_stream(device) -> stream   factory; returns a STARTED
                                        stream; may raise. Injected so
                                        this class never imports an
                                        audio library (and tests can
                                        hand it fakes).
        device                          the resolved output device,
                                        or None for the system default.
        on_fallback(exc)                called when the chosen device
                                        fails to open and the default
                                        is tried instead — ON THE
                                        WRITER THREAD, so it may only
                                        print, never touch the GUI.
                                        The exception is also recorded
                                        on `fallback_exc` before the
                                        call, for the caller's thread.
        slow_write_s                    write-duration bound for the
                                        degradation check (tests shrink
                                        it; production keeps 2 s).

    start() opens the stream on the writer thread and blocks until the
    open has succeeded or failed: on failure it raises the same error
    the inline open would have raised (device + default both refused).
    """

    def __init__(self,
                 open_stream: Callable,
                 device=None,
                 on_fallback: Optional[Callable] = None,
                 slow_write_s: float = _SLOW_WRITE_S):
        self._open_stream = open_stream
        self._device = device
        self._on_fallback = on_fallback
        self._slow_write_s = float(slow_write_s)
        self._q: "queue.Queue[Optional[bytes]]" = queue.Queue(
            maxsize=MAX_QUEUED)
        self._thread: Optional[threading.Thread] = None
        self._stream = None
        self._opened = threading.Event()
        self._open_error: Optional[BaseException] = None
        self._stop = threading.Event()
        # The stream's reported output latency (seconds), refreshed on
        # every (re)open; the play loop reads it once at session start
        # to size the echo tail, as the inline open was read before.
        self.latency = 0.0
        # Set when the writer has died for good (open/re-open failed).
        # The play loop polls it and ends playback, mirroring how a
        # failed stream.write used to break the loop.
        self.error: Optional[BaseException] = None
        # Batches dropped because the jitter buffer hit its
        # pathological guard bound. Public on purpose: speech loss is
        # counted where the caller can see it, never silent.
        self.dropped = 0
        # The chosen device's open exception when the default was used
        # instead. Recorded before on_fallback runs because on_fallback
        # executes on the writer thread — a callback touching the GUI
        # from here is a thread-safety bug (main.py's speaker fallback
        # used to call ui.write_log from this thread). The caller
        # reads this after start() and does GUI work on its own thread.
        self.fallback_exc: Optional[BaseException] = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="playback-writer", daemon=True)
        self._thread.start()
        # Wait for the open so a refused device fails the caller the
        # way the inline open did — before any audio is queued.
        self._opened.wait(timeout=30)
        if self._open_error is not None:
            err, self._open_error = self._open_error, None
            raise err

    def submit(self, data: bytes) -> None:
        """Hand one batch to the writer. Never blocks the caller and
        never evicts speech already queued: the buffer is deep enough
        (~5 min) that a full queue is pathological, and in that case
        the INCOMING batch is dropped and counted on `dropped` —
        accepted speech is never silently discarded (see header)."""
        if self._stop.is_set():
            return
        try:
            self._q.put_nowait(data)
        except queue.Full:
            self.dropped += 1             # guard tripped: the loss is
                                          # counted, never silent

    def flush(self) -> None:
        """Drop every queued-but-unwritten batch (barge-in). The batch
        currently inside stream.write is untouched — it is ≤200 ms,
        the interrupt bound the batching cap has always promised."""
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                return

    def close(self) -> None:
        """Stop the writer and close the stream, on the writer thread.
        Unwritten batches are discarded, as session teardown always
        discarded them. Bounded join: a wedged write must not hang
        shutdown — the thread is a daemon and the stream close is
        best-effort in that case."""
        self._stop.set()
        try:
            self._q.put_nowait(None)      # wake the loop so it can exit
        except queue.Full:
            pass
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=3.0)

    # -- the writer thread -----------------------------------------------------

    def _open_with_fallback(self):
        """Open on the chosen device; on failure, warn via on_fallback
        and open the default — the session open's rule, reused for
        every heal. Raises only when there is nothing left to try."""
        try:
            return self._open_stream(self._device)
        except Exception as e:
            if self._device is None:
                raise
            # Record BEFORE invoking the callback: on_fallback runs on
            # this writer thread, so the caller needs the exception on
            # `fallback_exc` to do any GUI-side warning on its own
            # thread once start() returns.
            self.fallback_exc = e
            if self._on_fallback is not None:
                try:
                    self._on_fallback(e)
                except Exception:
                    pass
            return self._open_stream(None)

    def _open_fresh(self) -> bool:
        """(Re)open the stream and refresh latency. False = the writer
        is dead; self.error says why."""
        try:
            stream = self._open_with_fallback()
        except Exception as e:
            self.error = e
            print(f"[JARVIS] ❌ Playback: output stream failed to "
                  f"open: {e}")
            return False
        self._stream = stream
        try:
            lat = float(getattr(stream, "latency", 0.0) or 0.0)
            if 0.0 < lat < 1.0:
                self.latency = lat
        except Exception:
            pass
        return True

    def _close_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is None:
            return
        for fn in ("stop", "close"):
            try:
                getattr(stream, fn)()
            except Exception:
                pass

    def _run(self) -> None:
        if not self._open_fresh():
            self._open_error = self.error
            self._opened.set()
            return
        self._opened.set()
        try:
            while not self._stop.is_set():
                try:
                    item = self._q.get(timeout=0.25)
                except queue.Empty:
                    continue
                if item is None or self._stop.is_set():
                    break
                degraded = False
                why = ""
                t0 = time.time()
                try:
                    self._stream.write(item)
                except Exception as e:
                    why = (f"Playback write failed: {e} — "
                           f"re-opening the output stream")
                    degraded = True
                else:
                    took = time.time() - t0
                    if took > self._slow_write_s:
                        why = (f"Playback write took {took:.1f}s — "
                               f"re-opening the output stream")
                        degraded = True
                if degraded:
                    if self._stop.is_set():
                        # Teardown is closing the stream under the
                        # writer, so a failed write here is the close
                        # racing us, not a sick device — this is the
                        # "Stream is stopped [PaErrorCode -9983]" line
                        # in Alek's 2026-10-22 console. Exit quietly:
                        # no heal attempt, no prints.
                        break
                    print(f"[JARVIS] ⚠️  {why}")
                    self._close_stream()
                    if not self._open_fresh():
                        return           # error recorded; thread dies
                    print("[JARVIS] 🔊 Playback stream re-opened")
        finally:
            self._close_stream()
