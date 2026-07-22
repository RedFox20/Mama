"""Pins AsyncLogWriter: queued writes reach the stream in order, ANSI stripped, amortized idle-flush,
close() drains."""
import time
from mama.utils.log_writer import AsyncLogWriter


class _Cap:
    def __init__(self): self.data = []; self.flushes = 0; self.closed = False
    def write(self, s): self.data.append(s)
    def flush(self): self.flushes += 1
    def close(self): self.closed = True


def test_drains_in_order_strips_ansi_and_closes():
    cap = _Cap()
    w = AsyncLogWriter(cap)
    w.write('\x1b[31mred error\x1b[0m\n'); w.write('plain\n')
    w.close()   # enqueues the sentinel, joins the drain thread, flushes+closes the stream
    assert ''.join(cap.data) == 'red error\nplain\n'   # colours stripped, order preserved
    assert cap.closed


def test_write_never_raises_on_a_broken_stream():
    class _Broken:
        def write(self, s): raise OSError('disk full')
        def flush(self): pass
        def close(self): pass
    w = AsyncLogWriter(_Broken())
    w.write('x\n'); w.close()   # the drain swallows the OSError; the build must not crash


def test_flushes_on_idle_without_waiting_for_close():
    cap = _Cap()
    w = AsyncLogWriter(cap, flush_interval=0.02)
    w.write('confirmed sequential output\n')       # not close()d yet
    end = time.monotonic() + 2.0
    while cap.flushes == 0 and time.monotonic() < end: time.sleep(0.01)
    assert cap.flushes >= 1                         # amortized: flushed on the idle lull, not only at close
    assert 'confirmed sequential output' in ''.join(cap.data)
    w.close()
