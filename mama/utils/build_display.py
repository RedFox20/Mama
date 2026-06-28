"""Unified live display for parallel configure/build jobs.

TTY: a live region of one line per running task, capped to terminal height, redrawn
in place (Buck2/Bazel "superconsole" style). Finished tasks commit a permanent summary
line above the region. Non-TTY: plain start/end lines, with a full output dump per task
when verbose. Every task keeps its full raw (colour-preserving) output for failure replay.

Pure logic with injected seams (out / isatty / term_size / clock) so it unit-tests with
no real terminal, threads, or subprocesses."""

from __future__ import annotations
import re, threading
from .system import Color, get_colored_text


_CURSOR_UP = '\x1b[1A'
_ERASE_EOL = '\x1b[K'  # erase to end of line (colorama enables it on Windows)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')  # SGR colour codes, for width-correct previews

_ICON = {'run': '*', 'ok': '+', 'fail': 'x'}
_ICON_COLOR = {'run': Color.BLUE, 'ok': Color.GREEN, 'fail': Color.RED}


def _fmt_secs(s: float) -> str:
    return f'{s:5.1f}s'


class Task:
    def __init__(self, id, kind: str, name: str, start: float):
        self.id = id
        self.kind = kind            # 'configure' | 'build' | ...
        self.name = name
        self.start = start
        self.end = None
        self.state = 'run'          # 'run' | 'ok' | 'fail'
        self.lines: list[str] = []  # full raw output, colours intact (for replay)
        self.current = ''           # last non-empty line, shown live

    def feed(self, line: str):
        self.lines.append(line)
        s = line.strip()
        if s: self.current = s

    def elapsed(self, now: float) -> float:
        return (self.end if self.end is not None else now) - self.start


class BuildDisplay:
    def __init__(self, out, isatty: bool, term_size, clock,
                 verbose=False, color=True, min_interval=0.1, margin=1, reveal_delay=0.15):
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

    @property
    def isatty(self) -> bool:
        return self._isatty

    # -- task lifecycle ----------------------------------------------------

    def start_task(self, id, kind: str, name: str) -> Task:
        # A task is recorded but stays INVISIBLE until it has run longer than reveal_delay, so an
        # instant no-op (cached/nothing-to-build dep, ~0.0s) never clutters the output. No start
        # line off-TTY either - we only emit a summary at finish, and only if it was slow enough.
        with self._lock:
            t = Task(id, kind, name, self._clock())
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
            hide = ok and not self._verbose and t.elapsed(t.end) < self._reveal  # instant success
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
        """Finalize: flush any pending permanent lines, drop the live region."""
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

    def _task_line(self, t: Task, now: float, cols: int) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        preview = _ANSI_RE.sub('', t.current)  # strip colours so width math is correct
        head = f'{icon} {t.kind:<9} {t.name:<22} {_fmt_secs(t.elapsed(now))}  '
        return self._truncate(head + preview, cols)

    def _summary_line(self, t: Task) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        return f'{icon} {t.kind:<9} {t.name:<22} {_fmt_secs(t.elapsed(t.end))}'

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
