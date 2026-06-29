"""Parallel DAG scheduler for configure/build jobs.

Runs a graph of `Job`s respecting dependency edges, with two governors:
- CONFIGURE jobs are cheap (compiler probes) -> bounded by a simple count.
- BUILD jobs each spawn `cmake --build -jN`, so they're gated by a CPU-load sample
  plus a core budget: always allow at least one build, then launch more only while
  load is below threshold and the reserved-core sum leaves room.

Fail-fast: on the first job error, stop releasing new jobs, let in-flight jobs finish,
then return the failed job. Generic over `Job.run`, so it unit-tests with fake jobs;
the cmake wiring lives in the caller (build_target/dependency_chain)."""

from __future__ import annotations
import time, threading, concurrent.futures, contextlib
from typing import Callable, Iterable, List, Optional

LOAD = 'load'        # clone + parse mamafile + discover deps (network/IO bound)
CONFIGURE = 'configure'
BUILD = 'build'


class Job:
    def __init__(self, key, kind: str, run: Callable[[], None],
                 deps: Iterable['Job'] = (), weight=1, node=None):
        self.key = key
        self.kind = kind
        self.run = run
        self.deps = set(deps)
        self.weight = weight   # int, or a zero-arg callable resolved at launch (lazy TU probe)
        self.node = node
        self.started = False
        self.done = False
        self.error: Optional[BaseException] = None
        self._rweight = 1      # weight resolved at launch, reused at completion

    def __repr__(self): return f'Job({self.kind} {self.key})'


def _check_acyclic(jobs: List[Job]):
    """DFS cycle check; raises RuntimeError naming a node in the cycle."""
    WHITE, GREY, BLACK = 0, 1, 2
    color = {j: WHITE for j in jobs}
    def visit(j: Job):
        color[j] = GREY
        for d in j.deps:
            if d not in color: continue  # dep outside this batch: already satisfied
            if color[d] == GREY: raise RuntimeError(f'Cyclical dependency at {d}')
            if color[d] == WHITE: visit(d)
        color[j] = BLACK
    for j in jobs:
        if color[j] == WHITE: visit(j)


def build_dep_jobs(deps, configure_fn, build_fn, weight_fn=None, children_fn=None) -> List[Job]:
    """Wire a configure + build job per dep. A dep's configure waits on every in-set
    child's build (its dependencies.cmake embeds child build outputs); its build waits
    on its own configure. `weight_fn(dep)` may return an int or a zero-arg callable for a
    lazy TU probe. `children_fn(dep)` defaults to `dep.get_children()`."""
    children_fn = children_fn or (lambda d: d.get_children())
    weight_fn = weight_fn or (lambda d: 1)
    dep_set = set(deps)
    cfg = {d: Job((d, 'c'), CONFIGURE, (lambda x=d: configure_fn(x)), node=d) for d in deps}
    bld = {d: Job((d, 'b'), BUILD, (lambda x=d: build_fn(x)), weight=weight_fn(d), node=d) for d in deps}
    for d in deps:
        bld[d].deps.add(cfg[d])
        for child in children_fn(d):
            if child in dep_set: cfg[d].deps.add(bld[child])
    return list(cfg.values()) + list(bld.values())


class Scheduler:
    def __init__(self, *, max_configure: int, core_budget: int, max_load: int = 20,
                 load_threshold: float = 85.0, overprovision: float = 2.0, cpu_sampler: Callable[[], float] = None,
                 poll_interval: float = 1.0, max_workers: int = 256, debug_log: Callable[[str], None] = None):
        self._max_configure = max(1, max_configure)
        self._core_budget = max(1, core_budget)
        self._max_load = max(1, max_load)
        self._load_threshold = load_threshold
        self._overprovision = overprovision
        self._cpu_sampler = cpu_sampler or (lambda: 0.0)
        self._poll_interval = poll_interval
        self._max_workers = max_workers
        self._debug_log = debug_log   # optional (str)->None sink for per-second scheduler state
        self._cond = threading.Condition()
        self._pending: List[Job] = []  # jobs not yet launched (grows dynamically via grow())
        self._running: set = set()    # running jobs, for debug status
        self._n_load = 0         # running LOAD jobs
        self._n_config = 0       # running CONFIGURE jobs
        self._n_slot = 0         # active build_slot() barriers (custom build()s compiling)
        self._reserved = 0       # reserved cores of running BUILD jobs + acquired build_slots
        self._n_running = 0      # total running jobs
        self._cpu_now = 0.0      # system CPU%, sampled once per scheduler pass (reused by gate + debug)
        self._last_debug = 0.0
        self._error: Optional[Job] = None  # first failed job

    def grow(self, build_fn: Callable[[], List[Job]]):
        """Atomically extend the graph at runtime: `build_fn()` (run under the scheduler lock so it
        can safely mutate caller registries and add edges to existing jobs) returns new jobs to add.
        Used by a LOAD job that just discovered a dep's children. The add happens before the LOAD
        job is marked done, so a parent's CONFIGURE never launches before its children's edges exist."""
        with self._cond:
            self._pending.extend(build_fn())
            self._cond.notify_all()

    def run(self, jobs: List[Job]) -> Optional[Job]:
        """Execute all jobs honoring deps + governors. Returns the failed job, or None. The graph may
        grow during the run via grow(); the loop ends only when nothing is pending AND nothing runs."""
        _check_acyclic(list(jobs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            with self._cond:
                self._pending = list(jobs)
                while self._pending or self._n_running:
                    if self._error and not self._n_running:
                        break  # failed and drained
                    self._cpu_now = self._cpu_sampler()  # one sample per pass: reused by every gate check + debug
                    progressed = False
                    blocked: List[Job] = []
                    if not self._error:
                        for job in [j for j in self._pending if self._deps_done(j)]:
                            if self._can_launch(job):
                                self._pending.remove(job)
                                self._launch(job, ex)
                                progressed = True
                            else:
                                blocked.append(job)  # deps ready but a governor held it back
                    self._maybe_debug(blocked)
                    if not progressed:
                        if self._n_running == 0 and self._pending and not self._error:
                            # nothing running and nothing launchable: the remaining jobs have unmet
                            # deps that no running job can satisfy -> a dependency cycle. Don't hang.
                            self._error = self._pending[0]
                            self._error.error = RuntimeError(f'Cyclical dependency at {self._error}')
                            break
                        # otherwise wait for a completion, a grow(), or a CPU re-sample
                        self._cond.wait(timeout=self._poll_interval)
        return self._error

    # -- governors ---------------------------------------------------------

    def _deps_done(self, job: Job) -> bool:
        return all(d.done for d in job.deps)

    @staticmethod
    def _resolve_weight(job: Job) -> int:
        w = job.weight() if callable(job.weight) else job.weight
        return max(0, int(w))  # 0 = unsizable build: reserves no budget, never blocks others

    def _can_launch(self, job: Job) -> bool:
        if self._n_running >= self._max_workers:
            return False
        if job.kind == LOAD:
            return self._n_load < self._max_load   # network/IO bound: simple count cap
        if job.kind == CONFIGURE:
            return self._n_config < self._max_configure
        return self._build_admits(self._resolve_weight(job))  # BUILD

    def _build_admits(self, weight: int) -> bool:
        """Budget gate shared by BUILD-job launch and build_slot() barriers: launch freely while
        reserved cores stay within budget REGARDLESS of momentary CPU (one compile saturates the
        sampler - gating on that stalled every build while cores sat idle, the krattgcs bug); the
        CPU sampler only gates EXTRA over-provisioning beyond the budget. reserved==0 always admits
        so at least one build always proceeds (and a barrier is always released - no deadlock)."""
        if self._reserved == 0:
            return True
        need = self._reserved + weight
        if need <= self._core_budget:
            return True
        if self._cpu_now >= self._load_threshold:
            return False
        return need <= self._core_budget * self._overprovision

    @contextlib.contextmanager
    def build_slot(self, weight: int):
        """Acquire `weight` budget cores for a compile running INSIDE a job - a custom build()'s
        cmake_build(), reached transparently via system.build_barrier(). The worker thread suspends
        here (coroutine-style) until the budget admits it, reserves, runs the compile, then releases
        on exit. always-admit-when-idle guarantees the barrier is released and can't deadlock."""
        weight = max(0, int(weight))
        with self._cond:
            while not self._build_admits(weight):
                self._cond.wait(timeout=self._poll_interval)
            self._reserved += weight
            self._n_slot += 1
            self._cond.notify_all()
        try:
            yield
        finally:
            with self._cond:
                self._reserved -= weight
                self._n_slot -= 1
                self._cond.notify_all()

    def _maybe_debug(self, blocked: List[Job]):
        """Once per second, emit reserved/budget, the running builds + their weights, the builds held
        back by a governor + their weights, and the sampled CPU - so parallelism can be observed."""
        if self._debug_log is None: return
        now = time.monotonic()
        if now - self._last_debug < 1.0: return
        self._last_debug = now
        name = lambda j: getattr(j.node, 'name', None) or j.key
        running = [j for j in self._running if j.kind == BUILD]
        run_s = ' '.join(f'{name(j)}({j._rweight})' for j in running) or '-'
        blk_s = ' '.join(f'{name(j)}({self._resolve_weight(j)})' for j in blocked if j.kind == BUILD) or '-'
        slots = f' slots={self._n_slot}' if self._n_slot else ''
        self._debug_log(f'[sched] cpu={self._cpu_now:>3.0f}% reserved={self._reserved}/'
                        f'{self._core_budget * self._overprovision:.0f}{slots} building[{len(running)}]: {run_s} '
                        f'| blocked: {blk_s}')

    def _launch(self, job: Job, ex: concurrent.futures.ThreadPoolExecutor):
        job.started = True
        self._n_running += 1
        self._running.add(job)
        if job.kind == LOAD:
            self._n_load += 1
        elif job.kind == CONFIGURE:
            self._n_config += 1
        else:
            job._rweight = self._resolve_weight(job)
            self._reserved += job._rweight
        ex.submit(self._exec, job)

    def _exec(self, job: Job):
        try:
            job.run()
        except BaseException as e:  # noqa: BLE001 - surfaced to the caller via job.error
            job.error = e
        with self._cond:
            job.done = True
            self._n_running -= 1
            self._running.discard(job)
            if job.kind == LOAD:        self._n_load -= 1
            elif job.kind == CONFIGURE: self._n_config -= 1
            else:                       self._reserved -= job._rweight
            if job.error is not None and self._error is None:
                self._error = job  # first failure wins; stops further launches
            self._cond.notify_all()
