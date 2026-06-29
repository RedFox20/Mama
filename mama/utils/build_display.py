"""Unified live display for parallel configure/build jobs.

TTY: a live region of one line per ACTIVELY-running task, capped to terminal height, redrawn in
place (superconsole style). A dep flows through phases (load -> configure -> build) on ONE task that
stays put across them; when its whole workflow finishes it commits a single summary line above the
region, with a per-phase timing breakdown (`P 3.7s  C 363ms  B 415ms`) when more than one phase did
real work. Non-TTY: that same one merged summary line per dep, + a full output dump when verbose.
Every task keeps its raw colour-preserving output for failure replay. Injected seams (out / isatty /
term_size / clock) -> unit-testable with no real terminal/threads/subprocesses."""

from __future__ import annotations
import re, time, threading
from . import proc_cpu
from .system import Color, get_colored_text
from ..util import get_time_str


_CURSOR_UP = '\x1b[1A'
_ERASE_EOL = '\x1b[K'  # erase to end of line (colorama enables it on Windows)
_ERASE_EOL_LF = _ERASE_EOL + '\n'  # clear-to-EOL then newline: one written task/permanent line
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')  # SGR colour codes, for width-correct previews

_ICON = {'run': '*', 'ok': '+', 'fail': 'x'}
_ICON_COLOR = {'run': Color.BLUE, 'ok': Color.GREEN, 'fail': Color.RED}
# Single-letter tag per phase for the multi-phase breakdown: G for any Git load (check/clone/pull),
# L local source, A artifactory, C configure, B build.
_PHASE_LETTER = {'check': 'G', 'clone': 'G', 'pulling': 'G', 'local': 'L', 'artifactory': 'A',
                 'configure': 'C', 'build': 'B'}


class Task:
    """One dep across its whole workflow. `kind`/`detail`/`start` track the CURRENT phase; `phases`
    accumulates (duration, kind, detail) for each completed phase that did real work, so the final
    summary can show the breakdown. `lines` accumulates across phases for failure replay."""
    def __init__(self, id, kind: str, name: str, start: float, detail: str = ''):
        self.id = id
        self.kind = kind            # current phase: 'check' | 'configure' | 'build' | ...
        self.detail = detail        # e.g. 'J16' = cores this build uses, shown after the kind
        self.name = name
        self.start = start          # current phase start
        self.end = None
        self.state = 'run'          # 'run' | 'ok' | 'fail'
        self.cpu = 0.0              # live subprocess-tree CPU% (Linux-style: 8 busy cores ~ 800%)
        self.lines: list[str] = []  # full raw output, colours intact (for replay)
        self.current = ''           # last non-empty line, shown live
        self.phases: list = []      # completed (duration, kind, detail), interesting phases only

    def begin(self, kind: str, start: float, detail: str = ''):
        """Resume this task on a new phase (keeps phases/lines, resets the live preview + timer)."""
        self.kind = kind; self.detail = detail; self.start = start
        self.end = None; self.state = 'run'; self.current = ''

    def feed(self, line: str):
        self.lines.append(line)
        s = line.strip()
        if s: self.current = s

    def elapsed(self, now: float) -> float:
        return (self.end if self.end is not None else now) - self.start


class BuildDisplay:
    def __init__(self, out, isatty: bool, term_size, clock, verbose=False, color=True,
                 min_interval=0.1, margin=1, reveal_delay=0.1, cpu_sampler=None, sample_interval=1.5):
        self._out = out
        self._isatty = isatty
        self._term_size = term_size  # () -> (cols, rows)
        self._clock = clock          # () -> float
        self._verbose = verbose
        self._color = color
        self._min_interval = min_interval
        self._margin = margin
        self._reveal = reveal_delay  # hide tasks that start+finish faster than this (instant no-ops)
        self._tasks: dict[object, Task] = {}
        self._active: list[object] = []  # ids in start order
        self._pending: list[str] = []    # permanent lines to flush above the region
        self._pending_hint = None        # (name, reason) of the single next blocked task, shown live
        self._drawn = 0                  # active region lines drawn last frame
        self._last_render = 0.0
        self._lock = threading.RLock()        # guards task/region state (held only briefly)
        self._render_lock = threading.Lock()  # serializes terminal writes; non-forced renders skip if busy
        self._cpu_sampler = cpu_sampler  # (set[int]) -> float total tree CPU%; None -> auto (psutil)
        self._cpu_auto = cpu_sampler is None
        self._pids: dict[object, set] = {}  # tid -> live child pids, for CPU sampling
        self._sampler = None             # daemon thread, lazily started on first attach_pid
        self._stop = threading.Event()
        self._sample_interval = sample_interval

    @property
    def isatty(self) -> bool:
        return self._isatty

    # -- task lifecycle ----------------------------------------------------

    def start_task(self, id, kind: str, name: str, detail: str = '') -> Task:
        # Create on the first phase, else RESUME the existing dep task on a new phase (so check ->
        # configure -> build stay one line). Either way INVISIBLE until it outlives reveal_delay, so
        # an instant no-op (~0.0s cached dep) never clutters output.
        with self._lock:
            t = self._tasks.get(id)
            if t is None: t = self._tasks[id] = Task(id, kind, name, self._clock(), detail)
            else:         t.begin(kind, self._clock(), detail)
            if id not in self._active: self._active.append(id)
        if self._isatty: self.render()  # render OUTSIDE the state lock (terminal I/O must not block feeders)
        return t

    def relabel(self, id, kind: str):
        """Change a task's kind after the fact (a load task only knows it cloned/pulled/checked once done)."""
        with self._lock:
            t = self._tasks.get(id)
            if t is not None: t.kind = kind

    def set_pending(self, hint):
        """Show the single next blocked task `(name, reason)` below the live region, or clear it (None).
        Renders on change so the line updates even when nothing else draws - the stalled-scheduler case
        the user most wants to see."""
        with self._lock:
            if hint == self._pending_hint: return
            self._pending_hint = hint
        if self._isatty: self.render()

    def feed(self, id, line: str):
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            t.feed(line)
        if self._isatty: self.render()  # state lock released first: a slow draw can't stall the subprocess reader

    def finish_task(self, id, ok: bool, final: bool = True):
        # End the current phase. A non-final success stays DORMANT (no summary yet); the dep's last
        # phase (final=True) or any failure commits ONE merged summary for the whole dep.
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            t.end = self._clock()
            t.state = 'ok' if ok else 'fail'
            if id in self._active: self._active.remove(id)
            dur = t.elapsed(t.end)
            if dur >= self._reveal or not ok:  # skip instant phases; always keep a failure
                t.phases.append((dur, t.kind, t.detail))
            done = final or not ok                        # workflow over -> emit; else dormant, resume later
            show = done and (not ok or bool(t.phases))    # all-instant success -> hide (cached no-op)
            if not self._isatty:
                if show:
                    self._writeln(self._summary_line(t))
                    if self._verbose or not ok:
                        for line in t.lines: self._writeln(line)
                return
            if show: self._pending.append(self._summary_line(t))
        self.render(force=True)  # commit the summary + redraw the shrunken region, off the state lock

    # -- permanent output (above the live region) --------------------------

    def print_above(self, text: str):
        """Emit a line that survives above the live region (status messages)."""
        with self._lock:
            if not self._isatty:
                self._writeln(text); return
            self._pending.append(text)
        self.render(force=True)

    def replay(self, id):
        """Dump a task's full captured output permanently (colours intact)."""
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            self._clear_region()
            for line in t.lines: self._writeln(line)

    # -- rendering ---------------------------------------------------------

    def render(self, force=False):
        """Draw the live frame. A forced render waits for the terminal; a normal one SKIPS if another
        thread is already drawing (it'll be covered by that draw / the next tick), so feeders never
        block. State is snapshotted under the short state lock; the terminal write happens off it."""
        if not self._isatty:
            return
        if force: self._render_lock.acquire()
        elif not self._render_lock.acquire(blocking=False): return
        try:
            with self._lock:
                now = self._clock()
                if not force and (now - self._last_render) < self._min_interval:
                    return
                self._last_render = now
                pending, self._pending = self._pending, []
                region = self._region_lines(now)
                prev_drawn, self._drawn = self._drawn, len(region)
            frame = (_CURSOR_UP + '\r' + _ERASE_EOL) * prev_drawn
            frame += ''.join(line + _ERASE_EOL_LF for line in pending + region)
            self._out.write(frame)
            self._flush()
        finally:
            self._render_lock.release()

    def close(self):
        """Finalize: stop the CPU sampler, flush any pending permanent lines, drop the live region."""
        self._stop.set()
        if self._sampler is not None: self._sampler.join(timeout=1.0)  # join off-lock: sampler takes it
        with self._lock:
            if self._isatty:
                self._clear_region()
                for line in self._pending: self._writeln(line)
                self._pending.clear()
                self._drawn = 0
                self._flush()

    # -- internals ---------------------------------------------------------

    def _clear_region(self):
        # Cursor sits below the region (after trailing newlines); walk up,
        # erasing each line, to land at the region's top-left.
        if self._drawn:
            self._out.write((_CURSOR_UP + '\r' + _ERASE_EOL) * self._drawn)
            self._drawn = 0

    def _region_lines(self, now: float) -> list[str]:
        cols, rows = self._term_size()
        cap = max(1, rows - self._margin)
        ids = [i for i in self._active if self._tasks[i].elapsed(now) >= self._reveal]  # past reveal delay
        lines = [self._task_line(self._tasks[i], now, cols) for i in ids]
        if self._pending_hint:  # the single next blocked task + why, so a stall is visible at a glance
            lines.append(self._pending_line(self._pending_hint[0], self._pending_hint[1], cols))
        if len(lines) > cap:
            lines[cap - 1:] = [self._truncate(f'  ... (+{len(lines) - (cap - 1)} more)', cols)]
        return lines

    def _pending_line(self, name: str, reason: str, cols: int) -> str:
        icon = self._colored('~', Color.BLUE)
        return self._truncate(f'{icon} {"pending":<24}{name:<22} {reason}', cols)

    @staticmethod
    def _kind_field(kind: str, detail: str, cpu: float = 0.0) -> str:
        s = f'{kind} {detail}' if detail else kind   # 'build J12' / 'build J8 ' / 'configure'
        if cpu >= 1.0: s += ' cpu:' + f'{cpu:.0f}%'.ljust(5)  # fixed-width slot: 'cpu:132% ' / 'cpu:2790%'
        return s

    @staticmethod
    def _letter(kind: str) -> str:
        return _PHASE_LETTER.get(kind, (kind[:1] or '?').upper())

    def _time_field(self, t: Task, now: float) -> str:
        phases = t.phases + ([(t.elapsed(now), t.kind, t.detail)] if t.state == 'run' else [])
        if len(phases) == 1:
            return get_time_str(phases[0][0])  # single phase: bare time (no letter), the classic look
        return '  '.join(f'{self._letter(k)} {get_time_str(d)}' for d, k, _ in phases)

    def _task_line(self, t: Task, now: float, cols: int) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        preview = _ANSI_RE.sub('', t.current)  # strip colours so width math is correct
        head = f'{icon} {self._kind_field(t.kind, t.detail, t.cpu):<24}{t.name:<22} {self._time_field(t, now)}  '
        return self._truncate(head + preview, cols)

    def _summary_line(self, t: Task) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        _, kind, detail = t.phases[-1] if t.phases else (0, t.kind, t.detail)  # kind = last phase that did work
        return f'{icon} {self._kind_field(kind, detail):<24}{t.name:<22} {self._time_field(t, t.end)}'

    # -- live CPU sampling -------------------------------------------------

    def attach_pid(self, tid, pid: int):
        """Register a running child pid so the sampler can attribute its process-tree CPU to `tid`.
        Only build tasks are sampled - a CPU% on a configure/clone/update step is noise and wasted work."""
        with self._lock:
            t = self._tasks.get(tid)
            if t is None or t.kind != 'build': return
            self._pids.setdefault(tid, set()).add(pid)
        self._ensure_sampler()

    def detach_pid(self, tid, pid: int):
        with self._lock:
            pids = self._pids.get(tid)
            if not pids: return
            pids.discard(pid)
            if not pids:
                del self._pids[tid]
                t = self._tasks.get(tid)
                if t is not None: t.cpu = 0.0  # subprocess gone -> stop showing stale CPU

    def _ensure_sampler(self):
        if self._sampler is not None or not self._isatty: return
        with self._lock:
            if self._sampler is not None: return
            if self._cpu_auto:
                self._cpu_sampler = proc_cpu.make_sampler(); self._cpu_auto = False
            if self._cpu_sampler is None: return  # psutil unavailable -> feature off
            self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
            self._sampler.start()

    def _next_wait(self, sample_cost: float) -> float:
        # back off when a sample is expensive (busy host, huge process table) so CPU sampling can never
        # exceed ~10% of wall-time - a hard cap against starving the build threads (cost*9 -> 1-in-10).
        return max(self._sample_interval, sample_cost * 9)

    def _sample_loop(self):
        wait = self._sample_interval
        while not self._stop.wait(wait):
            t0 = self._clock()
            try: self._sample_once()
            except Exception: pass  # CPU readout is best-effort, never break the display
            wait = self._next_wait(self._clock() - t0)
            self.render()  # reflect updated CPU numbers (throttled by min_interval)

    def _sample_once(self):
        with self._lock:
            snapshot = {tid: set(pids) for tid, pids in self._pids.items() if pids}
        if not snapshot: return
        cpus = self._cpu_sampler(snapshot)  # ONE process scan for ALL build trees -> {tid: cpu%}; off-lock
        with self._lock:
            for tid, cpu in cpus.items():
                if tid not in self._pids: continue  # detached mid-scan: don't resurrect a dead task's CPU
                t = self._tasks.get(tid)
                if t is not None: t.cpu = cpu

    def _truncate(self, text: str, cols: int) -> str:
        # Cap to cols-1 to avoid wrapping that would break the cursor math. If it
        # fits, keep colours; if not, truncate the plain text (drops the icon colour).
        limit = max(1, cols - 1)
        plain = _ANSI_RE.sub('', text)
        return text if len(plain) <= limit else plain[:limit]

    def _colored(self, text: str, color) -> str:
        return get_colored_text(text, color) if self._color else text

    def _writeln(self, text: str):
        self._out.write(text + _ERASE_EOL_LF if self._isatty else text + '\n')

    def _flush(self):
        flush = getattr(self._out, 'flush', None)
        if flush: flush()
