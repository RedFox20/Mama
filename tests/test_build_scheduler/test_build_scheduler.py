"""Pins the parallel scheduler: dep ordering, cycle detection, configure/build governors, fail-fast."""
import threading, time
from types import SimpleNamespace
import pytest
from mama.build_scheduler import Job, Scheduler, build_dep_jobs, BuildInterrupted, LOAD, CONFIGURE, BUILD


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


def test_build_governor_high_load_blocks_overprovision():
    p = Probe()
    jobs = [Job(i, BUILD, p.body, weight=4) for i in range(4)]  # each fills half the budget
    sched = _sched(cpu_sampler=lambda: 100.0, core_budget=4, overprovision=2.0)  # busy
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 1)   # one fills the budget; busy CPU blocks over-provisioning
    time.sleep(0.05)
    assert p.cur == 1
    p.gate.set(); t.join(2.0)
    assert p.max == 1


def test_build_governor_low_load_overprovisions_past_budget():
    p = Probe()
    jobs = [Job(i, BUILD, p.body, weight=4) for i in range(4)]
    sched = _sched(cpu_sampler=lambda: 0.0, core_budget=4, overprovision=2.0)  # idle -> overprovision
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 2)   # 4+4 = budget*2; a third (12) exceeds even that
    time.sleep(0.05)
    assert p.cur == 2
    p.gate.set(); t.join(2.0)
    assert p.max == 2


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


def test_ungated_build_bypasses_cpu_and_budget_gate():
    # The root build is ungated: it launches even when the budget is full and CPU is pegged (it runs
    # alone after all deps), gated only by its own deps being done.
    sched = _sched(core_budget=8, overprovision=1.0, cpu_sampler=lambda: 100.0)
    sched._reserved = 8; sched._cpu_now = 100.0  # budget exhausted, CPU maxed
    assert sched._can_launch(Job('leaf', BUILD, lambda: None, weight=8)) is False    # normal build held back
    assert sched._can_launch(Job('root', BUILD, lambda: None, weight=8, ungated=True)) is True


def test_pending_hint_reports_blocked_build_then_waiting_dep():
    sched = _sched(core_budget=8, overprovision=1.0)
    sched._reserved = 8; sched._cpu_now = 100.0  # budget full + CPU pegged
    blocked = Job('b', BUILD, lambda: None, weight=8, node=SimpleNamespace(name='geo'))
    name, reason = sched._pending_hint([blocked])
    assert name == 'geo' and 'cpu' in reason          # a governor-held build -> CPU reason
    undone = Job('d', BUILD, lambda: None, node=SimpleNamespace(name='ReCpp'))   # not done
    waiter = Job('w', BUILD, lambda: None, deps=[undone], node=SimpleNamespace(name='app'))
    sched._pending = [waiter]
    assert sched._pending_hint([]) == ('app', 'waiting for ReCpp')  # nothing gated -> waiting on a dep
    sched._pending = []
    assert sched._pending_hint([]) is None            # nothing waiting -> no hint


def test_build_dep_jobs_marks_root_build_ungated():
    leaf = _Dep('leaf'); root = _Dep('root'); root.is_root = True
    jobs = build_dep_jobs([leaf, root], configure_fn=lambda d: None, build_fn=lambda d: None)
    builds = {j.node: j for j in jobs if j.kind == BUILD}
    assert builds[root].ungated and not builds[leaf].ungated


def test_many_small_leaf_builds_launch_in_parallel_under_busy_cpu():
    # krattgcs has ~20 small leaf deps; with the old CPU gate they ran one-at-a-time. Small TU
    # weights must fill the core budget concurrently even while the sampler reads saturated.
    p = Probe()
    jobs = [Job(i, BUILD, p.body, weight=2) for i in range(12)]
    sched = _sched(cpu_sampler=lambda: 99.0, core_budget=16)  # 16/2 = 8 fit at once
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 8)
    p.gate.set(); t.join(2.0)
    assert p.max == 8


def test_debug_log_reports_running_and_blocked_weights():
    logs, p = [], Probe()
    jobs = [Job(f'b{i}', BUILD, p.body, weight=4, node=SimpleNamespace(name=f'b{i}')) for i in range(3)]
    sched = _sched(core_budget=4, overprovision=2.0, cpu_sampler=lambda: 100.0, debug_log=logs.append)  # busy
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: any('blocked:' in l for l in logs))
    p.gate.set(); t.join(2.0)
    line = next(l for l in logs if 'building[1]' in l)
    assert 'reserved=4/8' in line and 'cpu=100%' in line   # budget 4 * overprovision 2.0
    assert '(4)' in line.split('blocked:')[1]              # blocked builds report their weight


def test_build_slot_barrier_blocks_until_budget_frees():
    sched = _sched(core_budget=8, overprovision=1.0, cpu_sampler=lambda: 100.0)
    p = Probe()
    hog = Job('hog', BUILD, p.body, weight=8)
    acquired = []
    def runner():
        with sched.build_slot(8):   # needs the whole budget; blocked while hog holds it
            acquired.append(time.monotonic())
    t, _ = _run_bg(sched, [hog, Job('runner', BUILD, runner, weight=0)])
    assert _wait_until(lambda: p.cur == 1)   # hog running, holds budget=8
    time.sleep(0.05)
    assert not acquired                       # the slot is blocked behind the hog
    p.gate.set(); t.join(2.0)
    assert acquired                           # released once the hog freed the budget


def test_resolve_weight_handles_int_and_callable():
    assert Scheduler._resolve_weight(Job('a', BUILD, lambda: None, weight=lambda: 4)) == 4
    assert Scheduler._resolve_weight(Job('a', BUILD, lambda: None, weight=3)) == 3
    assert Scheduler._resolve_weight(Job('a', BUILD, lambda: None, weight=0)) == 0  # unsizable -> no reserve


def test_fail_fast_returns_failed_job_and_blocks_dependents():
    def boom(): raise RuntimeError('kaboom')
    a = Job('a', BUILD, boom)
    b = Job('b', BUILD, lambda: None, deps=[a])  # depends on the failing job
    c = Job('c', BUILD, lambda: None)            # independent
    failed = _sched().run([a, b, c])
    assert failed is a and isinstance(a.error, RuntimeError)
    assert not b.started and not b.done   # never released after a failed
    assert c.done                         # independent job still completed


def test_normal_failure_does_not_fire_abort_hook():
    # Deliberate asymmetry: the child-killer (abort_hook) fires ONLY on Ctrl+C. A plain job failure
    # stops new launches but lets in-flight compiles finish, so the hook must stay silent.
    hook = []
    def boom(): raise RuntimeError('kaboom')
    failed = _sched(abort_hook=lambda: hook.append(1)).run([Job('a', BUILD, boom)])
    assert failed is not None and not hook


def test_load_governor_caps_concurrency():
    p = Probe()
    jobs = [Job(i, LOAD, p.body) for i in range(5)]
    sched = _sched(max_load=2)
    t, _ = _run_bg(sched, jobs)
    assert _wait_until(lambda: p.cur == 2)
    time.sleep(0.05)
    assert p.cur == 2            # capped at max_load
    p.gate.set(); t.join(2.0)
    assert p.max == 2


def test_dynamic_grow_runs_child_jobs_after_parent_load():
    log, lock = [], threading.Lock()
    def rec(x):
        with lock: log.append(x)
    sched = _sched()
    parent_cfg = Job(('p', 'c'), CONFIGURE, lambda: rec('cfg-p'))
    parent_bld = Job(('p', 'b'), BUILD, lambda: rec('bld-p'), deps={parent_cfg})
    def parent_load():
        rec('load-p')
        def grow():
            cl = Job(('c', 'L'), LOAD, lambda: rec('load-c'))
            cc = Job(('c', 'c'), CONFIGURE, lambda: rec('cfg-c'), deps={cl})
            cb = Job(('c', 'b'), BUILD, lambda: rec('bld-c'), deps={cc})
            parent_cfg.deps.add(cb)   # parent configure must now wait for the discovered child's build
            return [cl, cc, cb]
        sched.grow(grow)
    pl = Job(('p', 'L'), LOAD, parent_load)
    parent_cfg.deps.add(pl)
    assert sched.run([pl, parent_cfg, parent_bld]) is None
    assert set(log) == {'load-p', 'load-c', 'cfg-c', 'bld-c', 'cfg-p', 'bld-p'}
    assert log.index('load-c') > log.index('load-p')   # child discovered during parent load
    assert log.index('cfg-p') > log.index('bld-c')     # parent configure waited for the child build
    assert log.index('bld-p') > log.index('cfg-p')


def test_unsatisfiable_dep_is_reported_not_hung():
    a = Job('a', BUILD, lambda: None)
    a.deps.add(Job('missing', BUILD, lambda: None))   # dep never added to the run set
    failed = _sched(poll_interval=0.02).run([a])
    assert failed is a   # deadlock guard returns it instead of looping forever


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


def test_build_dep_jobs_resolves_weight_lazily_per_dep():
    d = _Dep('x')
    jobs = build_dep_jobs([d], configure_fn=lambda d: None, build_fn=lambda d: None, weight_fn=lambda d: 7)
    build = next(j for j in jobs if j.kind == BUILD)
    assert Scheduler._resolve_weight(build) == 7


def test_keyboard_interrupt_aborts_build_kills_children_and_returns_interrupted():
    # Ctrl+C lands in the scheduler loop: abort_hook fires (kills children), the in-flight job is
    # released, and run() returns a synthetic KeyboardInterrupt job so the caller fails the build.
    released, hook, n = threading.Event(), [], {'i': 0}
    def sampler():
        n['i'] += 1
        if n['i'] >= 2: raise KeyboardInterrupt   # 2nd pass = user hits Ctrl+C
        return 0.0
    sched = _sched(cpu_sampler=sampler, abort_hook=lambda: (hook.append(1), released.set()))
    failed = sched.run([Job('blocker', BUILD, lambda: released.wait(2.0), weight=0)])
    assert hook == [1] and sched._aborted
    assert failed is not None and isinstance(failed.error, KeyboardInterrupt)
    assert released.is_set()   # the worker was unblocked -> the pool drained


def test_build_slot_bails_when_build_already_failing():
    # A custom build()'s barrier must not start a new compile once the build is aborting/failing.
    sched = _sched()
    sched._error = Job('boom', BUILD, lambda: None)
    with pytest.raises(BuildInterrupted):
        with sched.build_slot(4): pass
