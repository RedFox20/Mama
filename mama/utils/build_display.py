"""Unified live display for parallel configure/build jobs.

TTY: a live region of one line per running task, capped to terminal height, redrawn in place
(superconsole style); finished tasks commit a permanent summary line above it. Non-TTY: plain
summary lines + a full output dump per task when verbose. Every task keeps its raw colour-preserving
output for failure replay. Injected seams (out / isatty / term_size / clock) -> unit-testable with
no real terminal/threads/subprocesses."""

from __future__ import annotations
import re, time, threading
from .system import Color, get_colored_text


_CURSOR_UP = '\x1b[1A'
_ERASE_EOL = '\x1b[K'  # erase to end of line (colorama enables it on Windows)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')  # SGR colour codes, for width-correct previews

_ICON = {'run': '*', 'ok': '+', 'fail': 'x'}
_ICON_COLOR = {'run': Color.BLUE, 'ok': Color.GREEN, 'fail': Color.RED}


def _fmt_secs(s: float) -> str:
    return f'{s:5.1f}s'


class Task:
    def __init__(self, id, kind: str, name: str, start: float, detail: str = ''):
        self.id = id
        self.kind = kind            # 'configure' | 'build' | ...
        self.detail = detail        # e.g. '[16]' = cores this build uses, shown after the kind
        self.name = name
        self.start = start
        self.end = None
        self.state = 'run'          # 'run' | 'ok' | 'fail'
        self.cpu = 0.0              # live subprocess-tree CPU% (Linux-style: 8 busy cores ~ 800%)
        self.lines: list[str] = []  # full raw output, colours intact (for replay)
        self.current = ''           # last non-empty line, shown live

    def feed(self, line: str):
        self.lines.append(line)
        s = line.strip()
        if s: self.current = s

    def elapsed(self, now: float) -> float:
        return (self.end if self.end is not None else now) - self.start


class BuildDisplay:
    def __init__(self, out, isatty: bool, term_size, clock, verbose=False, color=True,
                 min_interval=0.1, margin=1, reveal_delay=0.1, cpu_sampler=None, sample_interval=0.7):
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
        self._drawn = 0                  # active region lines drawn last frame
        self._last_render = 0.0
        self._lock = threading.RLock()
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
        # Recorded but INVISIBLE until it outlives reveal_delay, so an instant no-op (~0.0s cached
        # dep) never clutters output. Off-TTY emits only a finish summary, and only if slow enough.
        with self._lock:
            t = Task(id, kind, name, self._clock(), detail)
            self._tasks[id] = t
            self._active.append(id)
            if self._isatty: self.render()
            return t

    def feed(self, id, line: str):
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            t.feed(line)
            if self._isatty: self.render()

    def finish_task(self, id, ok: bool):
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            t.end = self._clock()
            t.state = 'ok' if ok else 'fail'
            if id in self._active: self._active.remove(id)
            hide = ok and t.elapsed(t.end) < self._reveal  # instant no-op (cached/<0.1s): prune even in verbose
            if self._isatty:
                if not hide: self._pending.append(self._summary_line(t))
                self.render(force=True)
            elif not hide:
                self._writeln(self._summary_line(t))
                if self._verbose or not ok:
                    for line in t.lines: self._writeln(line)

    # -- permanent output (above the live region) --------------------------

    def print_above(self, text: str):
        """Emit a line that survives above the live region (status messages)."""
        with self._lock:
            if self._isatty:
                self._pending.append(text)
                self.render(force=True)
            else:
                self._writeln(text)

    def replay(self, id):
        """Dump a task's full captured output permanently (colours intact)."""
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            self._clear_region()
            for line in t.lines: self._writeln(line)

    # -- rendering ---------------------------------------------------------

    def render(self, force=False):
        with self._lock:
            if not self._isatty:
                return
            now = self._clock()
            if not force and (now - self._last_render) < self._min_interval:
                return
            self._last_render = now
            self._clear_region()
            for line in self._pending: self._writeln(line)
            self._pending.clear()
            region = self._region_lines(now)
            for line in region: self._writeln(line)
            self._drawn = len(region)
            self._flush()

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
        if len(ids) > cap:
            shown = [self._task_line(self._tasks[i], now, cols) for i in ids[:cap - 1]]
            shown.append(self._truncate(f'  ... (+{len(ids) - (cap - 1)} more)', cols))
            return shown
        return [self._task_line(self._tasks[i], now, cols) for i in ids]

    @staticmethod
    def _kind_field(t: Task) -> str:
        s = f'{t.kind} {t.detail}'.rstrip()        # 'build [16]' / 'configure' / 'clone'
        if t.cpu >= 1.0: s += f' [{t.cpu:.0f}%]'   # live tree CPU, e.g. 'build [16] [597%]'
        return s

    def _task_line(self, t: Task, now: float, cols: int) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        preview = _ANSI_RE.sub('', t.current)  # strip colours so width math is correct
        head = f'{icon} {self._kind_field(t):<24}{t.name:<22} {_fmt_secs(t.elapsed(now))}  '
        return self._truncate(head + preview, cols)

    def _summary_line(self, t: Task) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        return f'{icon} {self._kind_field(t):<24}{t.name:<22} {_fmt_secs(t.elapsed(t.end))}'

    # -- live CPU sampling -------------------------------------------------

    def attach_pid(self, tid, pid: int):
        """Register a running child pid so the sampler can attribute its process-tree CPU to `tid`."""
        with self._lock:
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
                self._cpu_sampler = _make_tree_cpu_sampler(); self._cpu_auto = False
            if self._cpu_sampler is None: return  # psutil unavailable -> feature off
            self._sampler = threading.Thread(target=self._sample_loop, daemon=True)
            self._sampler.start()

    def _sample_loop(self):
        while not self._stop.wait(self._sample_interval):
            try: self._sample_once()
            except Exception: pass  # CPU readout is best-effort, never break the display
            self.render()  # reflect updated CPU numbers (throttled by min_interval)

    def _sample_once(self):
        with self._lock:
            snapshot = {tid: set(pids) for tid, pids in self._pids.items() if pids}
        cpus = {tid: self._cpu_sampler(pids) for tid, pids in snapshot.items()}  # psutil work off-lock
        with self._lock:
            for tid, cpu in cpus.items():
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
        self._out.write(text + _ERASE_EOL + '\n' if self._isatty else text + '\n')

    def _flush(self):
        flush = getattr(self._out, 'flush', None)
        if flush: flush()


def _make_tree_cpu_sampler():
    try: import psutil
    except ImportError: return None
    return _PsutilTreeCpu(psutil)


class _PsutilTreeCpu:
    """Sums CPU% of a build's subprocess tree (cmake -> ninja/make/msbuild -> compilers): per-process
    cpu-time delta over wall-clock, so a tree saturating N cores reads ~N*100%."""
    def __init__(self, psutil):
        self._ps = psutil
        self._state: dict[int, tuple] = {}  # pid -> (cpu_seconds, wallclock_ts), for the delta

    def __call__(self, root_pids) -> float:
        ps, now, total, seen = self._ps, time.time(), 0.0, set()
        for rp in root_pids:
            try:
                root = ps.Process(rp)
                tree = [root] + root.children(recursive=True)
            except ps.Error:
                continue
            for proc in tree:
                seen.add(proc.pid)
                try: t = proc.cpu_times()
                except ps.Error: continue
                cur = t.user + t.system
                base_cpu, base_ts = self._state.get(proc.pid, (0.0, None))
                if base_ts is None:  # first sight: average over the process' lifetime so far
                    try: base_ts = proc.create_time()
                    except ps.Error: base_ts = now
                self._state[proc.pid] = (cur, now)
                dt = now - base_ts
                if dt > 0: total += max(0.0, (cur - base_cpu) / dt * 100.0)
        for pid in [p for p in self._state if p not in seen]: del self._state[pid]  # drop dead procs
        return total
