"""Pins proc_cpu: tree CPU% delta math, that it reads only each build's own tree (not every system
process, never the whole table), and the Windows kernel32 path's correctness + speed."""
import contextlib, os, sys, time
import pytest
from mama.utils import proc_cpu as pc


def test_make_sampler_returns_callable_or_none():
    s = pc.make_sampler()
    assert s is None or callable(s)


def test_accumulate_cpu_delta_first_sight_and_prune():
    state = {}
    assert pc.accumulate_cpu(state, 100.0, {'t': {1: (12.0, 96.0)}}) == {'t': 300.0}  # first sight: 12s/4s life
    assert pc.accumulate_cpu(state, 101.0, {'t': {1: (13.0, 96.0)}}) == {'t': 100.0}  # +1 cpu-s over 1s wall
    assert pc.accumulate_cpu(state, 102.0, {'t': {}}) == {'t': 0.0} and state == {}   # gone -> pruned


class _FakeProc:
    def __init__(self, pid, calls): self.pid = pid; self._calls = calls
    def cpu_times(self): self._calls.append(self.pid); return (12.0, 0.0)
    def create_time(self): return 96.0
    def children(self, recursive=True): return [_FakeProc(2, self._calls)] if self.pid == 1 else []
    def oneshot(self): return contextlib.nullcontext()


def test_psutil_sampler_sums_tree_and_touches_only_that_tree(monkeypatch):
    # root pid 1 -> child pid 2, each first-seen alive 4s @ 12 cpu-sec -> 300% each -> 600%; AND it must
    # read cpu only for the build's own 2 procs, never enumerate the whole system
    monkeypatch.setattr(pc.time, 'time', lambda: 100.0)
    calls = []
    class Ps:
        Error = Exception
        def Process(self, pid): return _FakeProc(pid, calls)
        def process_iter(self, *a): raise AssertionError('must not enumerate all processes')
    assert pc.PsutilTreeCpu(Ps())({'t': {1}}) == {'t': 600.0}
    assert sorted(calls) == [1, 2]   # only the tree's two pids touched


@pytest.mark.skipif(sys.platform != 'win32', reason='kernel32 sampler is Windows-only')
def test_win_sampler_reads_real_process():
    s = pc.WinTreeCpu(); me = os.getpid()
    assert me in s._ppid_map()                  # one toolhelp snapshot sees this process
    cpu, create = s._proc_times(me)
    assert cpu >= 0.0 and create > 0.0          # GetProcessTimes returns sane cpu seconds + creation time
    assert s({'t': {me}})['t'] >= 0.0           # a full sample returns a CPU% for the tree


@pytest.mark.skipif(sys.platform != 'win32', reason='kernel32 sampler is Windows-only')
def test_win_sampler_is_fast():
    # one toolhelp snapshot + GetProcessTimes per tree pid. Generous bound (real cost ~15ms) that still
    # catches the read-every-process regression, which sampled in seconds.
    s = pc.WinTreeCpu(); me = os.getpid(); s({'t': {me}})  # warm up (first-sight state)
    t0 = time.perf_counter()
    for _ in range(5): s({'t': {me}})
    avg_ms = (time.perf_counter() - t0) / 5 * 1000
    assert avg_ms < 250, f'sampler too slow: {avg_ms:.1f} ms/sample'
