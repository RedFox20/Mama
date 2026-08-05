"""Pins AsyncLogWriter: queued writes reach the stream in order, ANSI stripped, amortized idle-flush,
close() drains. Also pins that one run opens one log."""
import time
import pytest
import mama.utils.log_writer as lw
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
    assert ''.join(cap.data) == 'red error\nplain\n'   # colors stripped, order preserved
    assert cap.closed


def test_write_never_raises_on_a_broken_stream():
    class _Broken:
        def write(self, s): raise OSError('disk full')
        def flush(self): pass
        def close(self): pass
    w = AsyncLogWriter(_Broken())
    w.write('x\n'); w.close()   # the drain swallows the OSError; the build must not crash


@pytest.fixture
def no_run_log(monkeypatch):
    """The module holds the ONE log of a run, so each test starts and ends without one."""
    monkeypatch.setattr(lw, '_build_log', None)
    yield
    lw.close_build_log()


def test_the_run_log_lands_in_the_workspace_the_root_named(tmp_path, no_run_log):
    log = lw.open_run_log(str(tmp_path), 'mypackages')
    assert lw.get_build_log() is log       # a display reads it back, it never opens one of its own
    log.write('first phase\n')
    lw.close_build_log()
    assert (tmp_path / 'mypackages' / 'mamabuild.log').read_text() == 'first phase\n'


def test_a_later_phase_reuses_the_one_log_instead_of_truncating_it(tmp_path, no_run_log):
    first = lw.open_run_log(str(tmp_path), 'packages')
    first.write('load phase\n')
    assert lw.open_run_log(str(tmp_path), 'packages') is first
    lw.close_build_log()
    assert (tmp_path / 'packages' / 'mamabuild.log').read_text() == 'load phase\n'


def test_no_workspace_root_means_no_log(no_run_log):
    assert lw.open_run_log(None, 'packages') is None and lw.get_build_log() is None


def test_flushes_on_idle_without_waiting_for_close():
    cap = _Cap()
    w = AsyncLogWriter(cap, flush_interval=0.02)
    w.write('confirmed sequential output\n')       # not close()d yet
    end = time.monotonic() + 2.0
    while cap.flushes == 0 and time.monotonic() < end: time.sleep(0.01)
    assert cap.flushes >= 1                         # amortized: flushed on the idle lull, not only at close
    assert 'confirmed sequential output' in ''.join(cap.data)
    w.close()
