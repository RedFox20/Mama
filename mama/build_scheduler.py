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
import threading, concurrent.futures
from typing import Callable, Iterable, List, Optional

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
    def __init__(self, *, max_configure: int, core_budget: int, load_threshold: float = 85.0,
                 overprovision: float = 1.25, cpu_sampler: Callable[[], float] = None,
                 poll_interval: float = 1.0, max_workers: int = 256):
        self._max_configure = max(1, max_configure)
        self._core_budget = max(1, core_budget)
        self._load_threshold = load_threshold
        self._overprovision = overprovision
        self._cpu_sampler = cpu_sampler or (lambda: 0.0)
        self._poll_interval = poll_interval
        self._max_workers = max_workers
        self._cond = threading.Condition()
        self._n_config = 0       # running CONFIGURE jobs
        self._reserved = 0       # reserved cores of running BUILD jobs
        self._n_running = 0      # total running jobs
        self._error: Optional[Job] = None  # first failed job

    def run(self, jobs: List[Job]) -> Optional[Job]:
        """Execute all jobs honoring deps + governors. Returns the failed job, or None."""
        jobs = list(jobs)
        _check_acyclic(jobs)
        pending = list(jobs)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self._max_workers) as ex:
            with self._cond:
                while pending or self._n_running:
                    if self._error and not self._n_running:
                        break  # failed and drained
                    progressed = False
                    if not self._error:
                        for job in [j for j in pending if self._deps_done(j)]:
                            if self._can_launch(job):
                                pending.remove(job)
                                self._launch(job, ex)
                                progressed = True
                    if not progressed:
                        # nothing launchable now: wait for a completion or to re-sample CPU
                        self._cond.wait(timeout=self._poll_interval)
        return self._error

    # -- governors ---------------------------------------------------------

    def _deps_done(self, job: Job) -> bool:
        return all(d.done for d in job.deps)

    @staticmethod
    def _resolve_weight(job: Job) -> int:
        w = job.weight() if callable(job.weight) else job.weight
        return max(1, int(w))

    def _can_launch(self, job: Job) -> bool:
        if self._n_running >= self._max_workers:
            return False
        if job.kind == CONFIGURE:
            return self._n_config < self._max_configure
        # BUILD: always allow at least one; otherwise gate on CPU load + core budget.
        if self._reserved == 0:
            return True
        if self._cpu_sampler() >= self._load_threshold:
            return False
        return self._reserved + self._resolve_weight(job) <= self._core_budget * self._overprovision

    def _launch(self, job: Job, ex: concurrent.futures.ThreadPoolExecutor):
        job.started = True
        self._n_running += 1
        if job.kind == CONFIGURE:
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
            if job.kind == CONFIGURE: self._n_config -= 1
            else:                     self._reserved -= job._rweight
            if job.error is not None and self._error is None:
                self._error = job  # first failure wins; stops further launches
            self._cond.notify_all()
