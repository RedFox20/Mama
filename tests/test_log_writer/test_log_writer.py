"""Pins AsyncLogWriter: queued writes reach the stream in order, ANSI stripped, close() drains."""
from mama.utils.log_writer import AsyncLogWriter


class _Cap:
    def __init__(self): self.data = []; self.closed = False
    def write(self, s): self.data.append(s)
    def flush(self): pass
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
