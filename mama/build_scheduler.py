"""Parallel DAG scheduler for configure/build jobs: runs a graph of `Job`s honoring dep edges.
Governors: CONFIGURE jobs (cheap probes) are count-bounded; BUILD jobs (each spawns `cmake
--build -jN`) are gated by a core budget + CPU-load sample. Fail-fast: first error stops new
launches, drains in-flight, returns the failed job. Generic over `Job.run` (the cmake wiring
lives in the caller), so it unit-tests with fake jobs."""

from __future__ import annotations
import time, threading, concurrent.futures, contextlib
from types import SimpleNamespace
from typing import Callable, Iterable, List, Optional

LOAD = 'load'        # clone + parse mamafile + discover deps (network/IO bound)
CONFIGURE = 'configure'
BUILD = 'build'


class BuildInterrupted(RuntimeError):
    """Raised in a build_slot barrier when the build is already failing/aborting, so a custom build()
    waiting for budget bails instead of starting a new compile."""


class Job:
    def __init__(self, key, kind: str, run: Callable[[], None],
                 deps: Iterable['Job'] = (), weight=1, node=None, ungated=False):
        self.key = key
        self.kind = kind
        self.run = run
        self.deps = set(deps)
        self.weight = weight   # int, or a zero-arg callable resolved at launch (lazy TU probe)
        self.node = node
        self.ungated = ungated # BUILD that bypasses the CPU/budget gate (the root: runs alone after its deps)
        self.started = False
        self.done = False
        self.error: Optional[BaseException] = None
        self._rweight = 1      # weight resolved at launch, reused at completion

    def __repr__(self): return f'Job({self.kind} {self.key})'


def _make_abort_job() -> Job:
    """Synthetic job run() returns on Ctrl+C so the caller reports an interrupted (failed) build."""
    job = Job(('<interrupted>',), BUILD, run=(lambda: None), node=SimpleNamespace(name='<interrupted>'))
    job.error = KeyboardInterrupt('build interrupted by Ctrl+C')
    job.done = True
    return job


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
    """Wire a configure + build job per dep: a dep's configure waits on every in-set child's build
    (its dependencies.cmake embeds child outputs), its build on its own configure. `weight_fn(dep)`
    gives the build's core weight, resolved lazily at launch. `children_fn` defaults to get_children."""
    children_fn = children_fn or (lambda d: d.get_children())
    weight_fn = weight_fn or (lambda d: 1)
    dep_set = set(deps)
    cfg = {d: Job((d, 'c'), CONFIGURE, (lambda x=d: configure_fn(x)), node=d) for d in deps}
    bld = {d: Job((d, 'b'), BUILD, (lambda x=d: build_fn(x)), weight=(lambda x=d: weight_fn(x)),
                  node=d, ungated=getattr(d, 'is_root', False)) for d in deps}
    for d in deps:
        bld[d].deps.add(cfg[d])
        for child in children_fn(d):
            if child in dep_set: cfg[d].deps.add(bld[child])
    return list(cfg.values()) + list(bld.values())


class Scheduler:
    def __init__(self, *, max_configure: int, core_budget: int, max_load: int = 20,
                 load_threshold: float = 85.0, overprovision: float = 2.0, cpu_sampler: Callable[[], float] = None,
                 poll_interval: float = 1.0, max_workers: int = 256, debug_log: Callable[[str], None] = None,
                 abort_hook: Callable[[], None] = None, pending_log: Callable[[Optional[tuple]], None] = None):
        self._max_configure = max(1, max_configure)
        self._core_budget = max(1, core_budget)
        self._max_load = max(1, max_load)
        self._load_threshold = load_threshold
        self._overprovision = overprovision
        self._cpu_sampler = cpu_sampler or (lambda: 0.0)
        self._poll_interval = poll_interval
        self._max_workers = max_workers
        self._debug_log = debug_log   # optional (str)->None sink for per-second scheduler state
        self._abort_hook = abort_hook # optional ()->None called on Ctrl+C to kill in-flight child processes
        self._pending_log = pending_log # optional ((name,reason)|None)->None: the single next blocked task
        self._aborted = False
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
        """Atomically extend the graph at runtime: `build_fn()` runs under the scheduler lock (so it
        can safely mutate caller registries + add edges) and returns new jobs. Used by a LOAD job
        that discovered children; the add happens before LOAD is marked done, so a parent's
        CONFIGURE never launches before its children's edges exist."""
        with self._cond:
            self._pending.extend(build_fn())
            self._cond.notify_all()

    def run(self, jobs: List[Job]) -> Optional[Job]:
        """Execute all jobs honoring deps + governors; returns the failed job or None. The graph may
        grow via grow(); the loop ends only when nothing is pending AND nothing runs."""
        _check_acyclic(list(jobs))
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            try:
                self._run_loop(ex, jobs)
            except KeyboardInterrupt:
                self._abort()  # Ctrl+C: kill children + wake barriers so the pool drains fast
        return self._error

    def _run_loop(self, ex, jobs: List[Job]):
        """The scheduler pass loop. A KeyboardInterrupt here propagates to run()'s abort handler."""
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
                if self._pending_log is not None: self._pending_log(self._pending_hint(blocked))
                if not progressed:
                    if self._n_running == 0 and self._pending and not self._error:
                        # nothing running and nothing launchable: the remaining jobs have unmet
                        # deps that no running job can satisfy -> a dependency cycle. Don't hang.
                        self._error = self._pending[0]
                        self._error.error = RuntimeError(f'Cyclical dependency at {self._error}')
                        break
                    # otherwise wait for a completion, a grow(), or a CPU re-sample
                    self._cond.wait(timeout=self._poll_interval)

    def _abort(self):
        """Ctrl+C: stop launching, set a synthetic interrupted error, wake barrier waiters, then kill
        in-flight children (outside the lock - kill() blocks up to ~1s each) so the pool drains fast."""
        with self._cond:
            self._aborted = True
            if self._error is None: self._error = _make_abort_job()
            self._cond.notify_all()
        if self._abort_hook: self._abort_hook()

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
        if job.ungated:  # BUILD the root: launch the moment its deps are done, no CPU/budget gate
            return True
        return self._build_admits(self._resolve_weight(job))  # BUILD

    def _build_admits(self, weight: int) -> bool:
        """Budget gate shared by BUILD launch + build_slot() barriers: admit freely while reserved
        cores stay within budget REGARDLESS of momentary CPU (one compile saturates the sampler;
        gating on that stalled every build - the krattgcs bug). CPU only gates over-provisioning
        beyond budget. reserved==0 always admits, so one build always proceeds and no barrier deadlocks."""
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
        """Acquire `weight` budget cores for a compile running INSIDE a job (a custom build()'s
        cmake_build(), reached via system.build_barrier()): the worker thread suspends until the
        budget admits, reserves, runs, then releases on exit. always-admit-when-idle = no deadlock."""
        weight = max(0, int(weight))
        with self._cond:
            while True:
                if self._error is not None: raise BuildInterrupted()  # build failing/aborting: don't start a new compile
                if self._build_admits(weight): break
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

    @staticmethod
    def _name(job: Job):
        return getattr(job.node, 'name', None) or str(job.key)

    def _pending_hint(self, blocked: List[Job]):
        """The single next task waiting to launch + why, for the live display: a governor-held job
        (deps ready, gated by CPU/budget/slots) if any, else a job still waiting on a dep. None if
        nothing's waiting (the scheduler is keeping up)."""
        if blocked:
            return (self._name(blocked[0]), self._block_reason(blocked[0]))
        for job in self._pending:
            undone = [d for d in job.deps if not d.done]
            if undone: return (self._name(job), f'waiting for {self._name(undone[0])}')
        return None

    def _block_reason(self, job: Job) -> str:
        if job.kind == LOAD:      return f'clone slots full ({self._n_load}/{self._max_load})'
        if job.kind == CONFIGURE: return f'configure slots full ({self._n_config}/{self._max_configure})'
        w = self._resolve_weight(job)  # BUILD: held by _build_admits (CPU gate or budget exhausted)
        if self._cpu_now >= self._load_threshold: return f'cpu {self._cpu_now:.0f}% >= {self._load_threshold:.0f}%'
        return f'budget {self._reserved}+{w} > {self._core_budget * self._overprovision:.0f}'

    def _maybe_debug(self, blocked: List[Job]):
        """Once per second, emit reserved/budget, running + governor-blocked builds (with weights),
        and the sampled CPU - so parallelism can be observed."""
        if self._debug_log is None: return
        now = time.monotonic()
        if now - self._last_debug < 1.0: return
        self._last_debug = now
        name = self._name
        running = [j for j in self._running if j.kind == BUILD]
        run_s = ' '.join(f'{name(j)}({j._rweight})' for j in running) or '-'
        blk_s = ' '.join(f'{name(j)}({self._resolve_weight(j)})' for j in blocked if j.kind == BUILD) or '-'
        slots = f' slots={self._n_slot}' if self._n_slot else ''
        self._debug_log(f'[sched] cpu={self._cpu_now:>3.0f}% reserved={self._reserved}/'
                        f'{self._core_budget * self._overprovision:.0f}{slots} building[{len(running)}]: {run_s} '
                        f'| blocked: {blk_s}')

    def _account(self, job: Job, sign: int):
        """Adjust the per-kind running counters (and reserved cores for BUILD) by +1/-1 in one place."""
        self._n_running += sign
        if job.kind == LOAD:        self._n_load += sign
        elif job.kind == CONFIGURE: self._n_config += sign
        else:                       self._reserved += sign * job._rweight

    def _launch(self, job: Job, ex: concurrent.futures.ThreadPoolExecutor):
        job.started = True
        self._running.add(job)
        if job.kind == BUILD: job._rweight = self._resolve_weight(job)  # fixed at launch, reused at completion
        self._account(job, +1)
        ex.submit(self._exec, job)

    def _exec(self, job: Job):
        try:
            job.run()
        except BaseException as e:  # noqa: BLE001 - surfaced to the caller via job.error
            job.error = e
        with self._cond:
            job.done = True
            self._running.discard(job)
            self._account(job, -1)
            if job.error is not None and self._error is None:
                self._error = job  # first failure wins; stops further launches
            self._cond.notify_all()
