"""Pins the parallel scheduler: dep ordering, cycle detection, configure/build governors, fail-fast."""
import threading, time
import pytest
from mama.build_scheduler import Job, Scheduler, build_dep_jobs, CONFIGURE, BUILD


def _wait_until(pred, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred(): return True
        time.sleep(0.005)
    return False


class Probe:
    """Counts concurrent job bodies and holds them at a gate until released."""
    def __init__(self):
        self.lock = threading.Lock(); self.cur = 0; self.max = 0
        self.gate = threading.Event()
    def body(self):
        with self.lock:
            self.cur += 1; self.max = max(self.max, self.cur)
        self.gate.wait(2.0)
        with self.lock: self.cur -= 1


def _run_bg(sched, jobs):
    out = {}
    t = threading.Thread(target=lambda: out.__setitem__('r', sched.run(jobs)))
    t.start()
    return t, out


def _sched(**kw):
    kw.setdefault('max_configure', 8); kw.setdefault('core_budget', 8)
    kw.setdefault('poll_interval', 0.02)
    return Scheduler(**kw)


def test_runs_all_jobs():
    ran = []
    jobs = [Job(i, BUILD, (lambda i=i: ran.append(i)), weight=1) for i in range(5)]
    assert _sched().run(jobs) is None
    assert sorted(ran) == [0, 1, 2, 3, 4]


def test_cycle_detection_raises():
    a = Job('a', BUILD, lambda: None); b = Job('b', BUILD, lambda: None)
    a.deps.add(b); b.deps.add(a)
    with pytest.raises(RuntimeError, match='Cyclical'):
        _sched().run([a, b])


def test_configure_governor_caps_concurrency():
    p = Probe()
    jobs = [Job(i, CONFIGURE, p.body) for i in range(5)]
    sched = _sched(max_configure=2)
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 2)
    time.sleep(0.05)              # give any erroneous 3rd a chance to start
    assert p.cur == 2            # never exceeds the cap
    p.gate.set(); t.join(2.0)
    assert p.max == 2


def test_build_governor_high_load_runs_one_at_a_time():
    p = Probe()
    jobs = [Job(i, BUILD, p.body, weight=1) for i in range(4)]
    sched = _sched(cpu_sampler=lambda: 100.0, load_threshold=85.0)  # always "busy"
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 1)
    time.sleep(0.05)
    assert p.cur == 1            # always-allow-one, but load blocks a second
    p.gate.set(); t.join(2.0)
    assert p.max == 1


def test_build_governor_low_load_runs_many():
    p = Probe()
    jobs = [Job(i, BUILD, p.body, weight=1) for i in range(4)]
    sched = _sched(cpu_sampler=lambda: 0.0, core_budget=8)
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 4)
    p.gate.set(); t.join(2.0)
    assert p.max == 4


def test_build_governor_respects_core_budget():
    p = Probe()
    jobs = [Job(i, BUILD, p.body, weight=4) for i in range(4)]  # 4 cores each
    sched = _sched(cpu_sampler=lambda: 0.0, core_budget=8, overprovision=1.0)
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 2)  # 4+4 = budget; a third (12) won't fit
    time.sleep(0.05)
    assert p.cur == 2
    p.gate.set(); t.join(2.0)
    assert p.max == 2


def test_resolve_weight_handles_int_and_callable():
    assert Scheduler._resolve_weight(Job('a', BUILD, lambda: None, weight=lambda: 4)) == 4
    assert Scheduler._resolve_weight(Job('a', BUILD, lambda: None, weight=3)) == 3
    assert Scheduler._resolve_weight(Job('a', BUILD, lambda: None, weight=0)) == 1  # clamped


def test_fail_fast_returns_failed_job_and_blocks_dependents():
    def boom(): raise RuntimeError('kaboom')
    a = Job('a', BUILD, boom)
    b = Job('b', BUILD, lambda: None, deps=[a])  # depends on the failing job
    c = Job('c', BUILD, lambda: None)            # independent
    failed = _sched().run([a, b, c])
    assert failed is a and isinstance(a.error, RuntimeError)
    assert not b.started and not b.done   # never released after a failed
    assert c.done                         # independent job still completed


class _Dep:
    def __init__(self, name, children=()):
        self.name = name; self._children = list(children)
    def get_children(self): return self._children


def test_build_dep_jobs_orders_parent_configure_after_child_build():
    log, lock = [], threading.Lock()
    def rec(kind, dep):
        with lock: log.append((kind, dep.name))
    child = _Dep('child'); parent = _Dep('parent', [child])
    jobs = build_dep_jobs([child, parent],
                          configure_fn=lambda d: rec('cfg', d), build_fn=lambda d: rec('bld', d))
    assert _sched().run(jobs) is None
    assert log.index(('bld', 'child')) < log.index(('cfg', 'parent'))  # child built before parent configures
    assert log.index(('cfg', 'parent')) < log.index(('bld', 'parent'))  # own configure before own build


def test_build_dep_jobs_lazy_weight_is_resolved_per_dep():
    d = _Dep('x')
    jobs = build_dep_jobs([d], configure_fn=lambda d: None, build_fn=lambda d: None,
                          weight_fn=lambda d: (lambda: 7))
    build = next(j for j in jobs if j.kind == BUILD)
    assert Scheduler._resolve_weight(build) == 7
