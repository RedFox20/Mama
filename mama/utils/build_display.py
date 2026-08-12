"""Live display for parallel configure/build jobs: one redrawn-in-place line per running task, and one committed
summary line per dep with a per-phase timing breakdown. Injected seams (out / isatty / term_size / clock) ease tests."""

from __future__ import annotations
import re, time, threading
from . import proc_cpu
from .system import Color, get_colored_text
from ..utils.progress import get_time_str, is_progress_line


_CURSOR_UP = '\x1b[1A'
_ERASE_EOL = '\x1b[K'  # erase to end of line (colorama enables it on Windows)
_ERASE_EOL_LF = _ERASE_EOL + '\n'  # clear-to-EOL then newline: one written task/permanent line
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*m')  # SGR color codes, for width-correct previews
_ESC_RE = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]|\x1b.')  # any escape sequence, color codes included
_CTRL_RE = re.compile(r'[\x00-\x1a\x1c-\x1f\x7f]')  # every C0 control except ESC, which _ESC_RE handles
# Matches MSVC 'error C2065:', GCC/Clang 'warning:' / 'error:', and CMake's colon-less 'CMake Error at <file>'.
# The \b guards keep -Werror, std::error_code and '0 errors' from matching.
_DIAG_RE = re.compile(r'\bcmake\s+(?P<cm>error|warning)\b|\b(?P<sev>error|warning)\b\s*(?:[A-Za-z]+[0-9]+)?\s*:',
                      re.IGNORECASE)


_CMAKE_HEAD = re.compile(r'^cmake\s+(error|warning)\b', re.IGNORECASE)
_MAX_BODY = 8  # continuation lines kept per cmake block, so one chatty warning cannot bury the rest

# Keep the context BEFORE a diagnostic (the call site) and the numbered snippet AFTER it (the broken
# expression), or a diagnostic reads as one line that points deep inside a header nobody edited.
# An `In function` header that opens an inlining chain carries no file prefix, so the prefix is optional here.
_DIAG_ROLE = (r'In (instantiation of|substitution of|(static |member |lambda )*function|constructor|destructor)\b'
              r'|At global scope:')
_DIAG_CONTEXT = re.compile(r'^(In file included from |\s+(inlined )?from \S'
                           rf'|(.*:\s+)?({_DIAG_ROLE})'
                           r'|.*:\s+(recursively )?required (from|by)\b'
                           r'|.*:\s*note:\s+in instantiation of\b)')
_DIAG_SNIPPET = re.compile(r'^\s*(\d+\s*\||\||[\^~])')  # `  566 | expr` and `      |     ~~~^~~~`
_DIAG_NOTE = re.compile(r'.*:\s*note:\s')
_MAX_CONTEXT = 5  # an instantiation chain runs dozens deep, and the innermost frames are the useful ones
_MAX_SNIPPET = 3  # the numbered source line plus its caret line, and one spare for a multi-line caret
_MAX_NOTES = 2    # clang reports the instantiation site in the notes AFTER the error, not above it


def _diag_context(lines, i):
    """The instantiation chain immediately above the diagnostic at `i`, outermost frame first."""
    out = []
    j = i - 1
    while j >= 0 and len(out) < _MAX_CONTEXT:
        text = _ANSI_RE.sub('', lines[j]).rstrip()
        if not _DIAG_CONTEXT.match(text): break
        out.append(text if text[:1].isspace() else text.strip())  # keep an include chain's alignment
        j -= 1
    return list(reversed(out))


def _diag_snippet(lines, i):
    """The source snippet under a diagnostic, whitespace kept so the caret column stays right. Returns (snippet, next_i)."""
    out = []
    while i < len(lines) and len(out) < _MAX_SNIPPET:
        text = _ANSI_RE.sub('', lines[i]).rstrip()
        if not _DIAG_SNIPPET.match(text): break
        out.append(text)
        i += 1
    return out, i


def _diag_trailer(lines, i):
    """The source snippet under a diagnostic, then the `note:` lines with their own snippets. Returns (out, next_i).
    Clang reports the instantiation site in those notes, not above the error, so they must survive."""
    out, i = _diag_snippet(lines, i)
    for _ in range(_MAX_NOTES):
        if i >= len(lines): break
        text = _ANSI_RE.sub('', lines[i]).rstrip()
        if not _DIAG_NOTE.match(text): break
        out.append(text.strip())
        snippet, i = _diag_snippet(lines, i + 1)
        out += snippet
    return out, i


def _cmake_body(lines, i):
    """A cmake diagnostic is a header plus an INDENTED body that ends on a blank pair. Returns (body, next_i).
    Walks to the END of the block, so the scanner never re-reports body lines, but keeps only _MAX_BODY lines."""
    body, j, blanks, dropped = [], i + 1, 0, 0
    while j < len(lines):
        raw = _ANSI_RE.sub('', lines[j]).rstrip()
        if not raw.strip():
            blanks += 1
            if blanks >= 2: break   # cmake terminates the block with two line breaks
            j += 1; continue
        if not raw[:1].isspace(): break  # unindented again: back to ordinary build output
        blanks = 0
        if len(body) < _MAX_BODY: body.append(raw.strip())
        else:                     dropped += 1
        j += 1
    if dropped: body.append(f'... (+{dropped} more lines)')  # capped, but never silently
    return body, j


def scan_diagnostics(lines, limit=8):
    """Extract compiler diagnostics for the post-build summary (a parallel build replays output only on failure).
    Returns (diags, n_err, n_warn) with diags = [(severity, ansi-stripped text)], de-duplicated, errors
    before warnings. A cmake block keeps its body as embedded newlines.
    lines: the task's raw captured output lines
    limit: max diagnostics returned"""
    seen = set(); errs = []; warns = []
    i = 0
    while i < len(lines):
        text = _ANSI_RE.sub('', lines[i]).strip()
        m = _DIAG_RE.search(text)
        if not m:
            i += 1; continue
        if _CMAKE_HEAD.match(text):
            body, i = _cmake_body(lines, i)
            if body: text = text + '\n' + '\n'.join(body)
        else:
            context = _diag_context(lines, i)
            trailer, i = _diag_trailer(lines, i + 1)
            if context or trailer: text = '\n'.join(context + [text] + trailer)
        if text in seen: continue
        seen.add(text)
        severity = (m.group('cm') or m.group('sev')).lower()
        (errs if severity == 'error' else warns).append(text)
    diags = [('error', t) for t in errs] + [('warning', t) for t in warns]
    return diags[:limit], len(errs), len(warns)

_ICON = {'run': '*', 'ok': '+', 'fail': 'x'}
_ICON_COLOR = {'run': Color.BLUE, 'ok': Color.GREEN, 'fail': Color.RED}
_START_ICON = '>'  # a non-terminal run opens each phase with this line, because nothing redraws it into view
# Short tag per phase for the timing breakdown (lowercase stands out between the times):
# git any git load, loc local source, art artifactory, cfg configure, bld build.
_PHASE_TAG = {'check': 'git', 'clone': 'git', 'pulling': 'git', 'local': 'loc', 'artifactory': 'art',
              'configure': 'cfg', 'build': 'bld'}


def _fmt_dur(d: float) -> str:
    """One phase's duration, right-aligned to a fixed width so the timing columns line up across rows.
    Sub-0.1s shows 2 decimals (`0.03s`), 0.1s and up uses the shared get_time_str (`0.5s`, `2m 44s`)."""
    s = f'{d:.2f}s' if d < 0.1 else get_time_str(d)
    if s == '0.00s': s = '0.0s'   # an instant phase: 0.0s reads better than an over-precise 0.00s
    return s.rjust(6)


class Task:
    """One dep across its whole workflow. `kind`/`detail`/`start` track the CURRENT phase. `phases` holds
    each completed phase that did real work, and `lines` accumulates across phases for failure replay."""
    def __init__(self, id, kind: str, name: str, start: float, detail: str = ''):
        self.id = id
        self.kind = kind            # current phase: 'check' | 'configure' | 'build' | ...
        self.detail = detail        # e.g. 'J16' = cores this build uses, shown after the kind
        self.name = name
        self.start = start          # current phase start
        self.end = None
        self.state = 'run'          # 'run' | 'ok' | 'fail'
        self.cpu = 0.0              # live subprocess-tree CPU% (Linux-style: 8 busy cores ~ 800%)
        self.lines: list[str] = []  # full raw output, colors intact (for replay)
        self.phase_start = 0        # index in `lines` where the CURRENT phase's output begins
        self.current = ''           # last non-empty line, shown live
        self.phases: list = []      # completed (duration, kind, detail), interesting phases only
        self.note = ''              # dep-level fact for the summary line, eg the artifactory archive

    def begin(self, kind: str, start: float, detail: str = ''):
        """Resume this task on a new phase (keeps phases/lines, resets the live preview + timer)."""
        self.kind = kind; self.detail = detail; self.start = start
        self.end = None; self.state = 'run'; self.current = ''
        self.phase_start = len(self.lines)  # replay shows THIS phase, not the whole dep's history

    def feed(self, line: str):
        # Collapse a run of progress updates to just the latest line, so a captured clone or download
        # does not flood the buffer (and thus the log + failure replay) with hundreds of updates.
        if self.lines and is_progress_line(line) and is_progress_line(self.lines[-1]):
            self.lines[-1] = line
        else:
            self.lines.append(line)
        s = line.strip()
        if s: self.current = s

    def elapsed(self, now: float) -> float:
        return (self.end if self.end is not None else now) - self.start


class BuildDisplay:
    def __init__(self, out, isatty: bool, term_size, clock, verbose=False, color=True,
                 min_interval=0.1, margin=1, reveal_delay=0.1, cpu_sampler=None, sample_interval=1.5, log=None,
                 platform=''):
        self._out = out
        self._log = log  # optional AsyncLogWriter: full per-target output + permanent lines -> mamabuild.log
        self._isatty = isatty
        self._term_size = term_size  # () -> (cols, rows)
        self._clock = clock          # () -> float
        self._verbose = verbose
        self._color = color
        self._platform = f'[{platform}] ' if platform else ''  # separates parallel builds of different platforms
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
        self._render_lock = threading.Lock()  # serializes terminal writes, non-forced renders skip if busy
        self._cpu_sampler = cpu_sampler  # (set[int]) -> float total tree CPU%. None -> auto (psutil)
        self._cpu_auto = cpu_sampler is None
        self._pids: dict[object, set] = {}  # tid -> live child pids, for CPU sampling
        self._sampler = None             # daemon thread, lazily started on first attach_pid
        self._stop = threading.Event()
        self._closed = False         # after close() the region is gone: never draw over the final output
        self._sample_interval = sample_interval

    @property
    def isatty(self) -> bool:
        return self._isatty

    # -- task lifecycle ----------------------------------------------------

    def start_task(self, id, kind: str, name: str, detail: str = '') -> Task:
        # Create on the first phase, else RESUME the existing dep task, so check -> configure -> build stay
        # one line. Either way INVISIBLE until it outlives reveal_delay, so an instant no-op never clutters.
        with self._lock:
            t = self._tasks.get(id)
            if t is None: t = self._tasks[id] = Task(id, kind, name, self._clock(), detail)
            else:         t.begin(kind, self._clock(), detail)
            if id not in self._active: self._active.append(id)
        # OUTSIDE the state lock: terminal I/O must not block the feeders
        if self._isatty: self.render()
        else: self._writeln(f'{self._colored(_START_ICON, Color.BLUE)} {self._kind_field(kind, detail):<24}{name}')
        return t

    def relabel(self, id, kind: str):
        """Change a task's kind after the fact (a load task only knows it cloned/pulled/checked once done)."""
        with self._lock:
            t = self._tasks.get(id)
            if t is not None: t.kind = kind

    def set_note(self, id, text: str):
        """Attach a dep-level fact to the summary line, so a reader sees where the exports came from.
        An empty text keeps whatever the task already holds."""
        if not text: return
        with self._lock:
            t = self._tasks.get(id)
            if t is not None: t.note = text

    def set_pending(self, hint):
        """Show the single next blocked task `(name, reason)` below the live region, or clear it (None).
        Renders on change, so a stalled scheduler stays visible even when nothing else draws."""
        with self._lock:
            if hint == self._pending_hint: return
            self._pending_hint = hint
        if self._isatty: self.render()

    def feed(self, id, line: str):
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            t.feed(line)
        if self._isatty: self.render()  # state lock released first: a slow draw cannot stall the subprocess reader

    def finish_task(self, id, ok: bool, final: bool = True):
        # End the current phase. A non-final success stays DORMANT, the dep's last phase or any failure
        # commits ONE merged summary. The task records every phase, so the table shows all steps.
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            t.end = self._clock()
            t.state = 'ok' if ok else 'fail'
            if id in self._active: self._active.remove(id)
            t.phases.append((t.elapsed(t.end), t.kind, t.detail))
            done = final or not ok                        # workflow over -> emit, else dormant until resume
            if done: self._log_task(t)  # full per-target output -> log, even for deps hidden from the live region
            if not self._isatty:
                # a start line opened every phase of this dep, so each one closes, instant or not
                if done:
                    self._writeln(self._summary_line(t))
                    if self._verbose or not ok:
                        for line in t.lines: self._writeln(line)
                return
            # a live region hides a dep that succeeded with every phase instant: a cached no-op is noise
            if done and (not ok or any(d >= self._reveal for d, _, _ in t.phases)):
                self._pending.append(self._summary_line(t))
        self.render(force=True)  # commit the summary + redraw the shrunken region, off the state lock

    # -- permanent output (above the live region) --------------------------

    def print_above(self, text: str):
        """Emit a line that survives above the live region (status messages)."""
        if self._log is not None: self._log.write(text + '\n')
        with self._lock:
            if not self._isatty or self._closed:  # no live region (or it is gone): write it straight out
                self._writeln(text); return
            self._pending.append(text)
        self.render(force=True)

    def _log_task(self, t: Task):
        """Write a target's whole captured buffer to the build log as ONE contiguous block, never intermixed
        across parallel targets. The log then has the full output the live region only previews."""
        if self._log is None or not t.lines: return
        self._log.write(f'\n{"=" * 100}\n{self._summary_line(t)}\n{"-" * 100}\n')
        self._log.write('\n'.join(t.lines) + '\n')

    def replay(self, id):
        """Dump the FAILING phase's captured output permanently (colors intact). Not the whole dep buffer:
        an earlier phase replays as stale noise. The full history is still in mamabuild.log."""
        with self._lock:
            t = self._tasks.get(id)
            if t is None: return
            self._clear_region()
            for line in t.lines[t.phase_start:]: self._writeln(line)

    def diagnostics(self, id, limit=8):
        """Compiler warnings/errors captured for a finished dep task, for the post-build summary."""
        t = self._tasks.get(id)
        return scan_diagnostics(t.lines, limit) if t else ([], 0, 0)

    def _flush_unfinished(self):
        """Dump what a phase buffered but never committed. A crash, a signal or a killed compiler ends a
        phase with no finish, and those last lines name the cause."""
        for tid in list(self._active):
            t = self._tasks.get(tid)
            if t is None or not t.lines: continue
            self._log_task(t)
            head = self._kind_field(t.kind, t.detail)
            self._writeln(f'{self._colored(_ICON["fail"], Color.RED)} {head:<24}{t.name:<22} stopped, last output:')
            for line in t.lines[t.phase_start:]: self._writeln(line)

    # -- rendering ---------------------------------------------------------

    def render(self, force=False):
        """Draw the live frame. A forced render waits for the terminal, a normal one SKIPS if another thread
        already draws, so feeders never block. The state snapshot goes under the short state lock, the
        terminal write off it."""
        if not self._isatty or self._closed:
            return
        if force: self._render_lock.acquire()
        elif not self._render_lock.acquire(blocking=False): return
        try:
            with self._lock:
                if self._closed: return  # closed while we waited for the terminal
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
        """Finalize: stop the CPU sampler, flush any pending permanent lines, drop the live region.
        Idempotent, so a signal handler and the run itself may both call it."""
        if self._closed: return
        self._stop.set()
        if self._sampler is not None: self._sampler.join(timeout=1.0)  # join off-lock: sampler takes it
        # take the render lock too: a sampler render that outlives the join would redraw the region UNDER the final output
        with self._render_lock, self._lock:
            self._closed = True
            if self._isatty:
                self._clear_region()
                for line in self._pending: self._writeln(line)
                self._pending.clear()
                self._drawn = 0
            self._flush_unfinished()
            self._flush()
        # the log belongs to the run, not to this display: a later phase opens its own display and
        # writes to the same log. log_writer closes it when the process exits.

    # -- internals ---------------------------------------------------------

    def _clear_region(self):
        # the cursor sits below the region: walk up, erasing each line, to land at the region's top-left
        if self._drawn:
            self._out.write((_CURSOR_UP + '\r' + _ERASE_EOL) * self._drawn)
            self._drawn = 0

    def _region_lines(self, now: float) -> list[str]:
        cols, rows = self._term_size()
        cap = max(1, rows - self._margin)
        ids = [i for i in self._active if self._tasks[i].elapsed(now) >= self._reveal]  # past reveal delay
        lines = [self._task_line(self._tasks[i], now, cols) for i in ids]
        if self._pending_hint:  # the single next blocked task + why, so a stall is visible
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
    def _tag(kind: str) -> str:
        return _PHASE_TAG.get(kind, (kind[:3] or '?').lower())

    def _time_field(self, t: Task, now: float) -> str:
        # tag every phase, even a lone build -> 'bld 4.0s', so the timing column stays consistent
        phases = t.phases + ([(t.elapsed(now), t.kind, t.detail)] if t.state == 'run' else [])
        return self._platform + '  '.join(f'{self._tag(k)} {_fmt_dur(d)}' for d, k, _ in phases)

    def _task_line(self, t: Task, now: float, cols: int) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        preview = _ANSI_RE.sub('', t.current)  # strip colors so width math is correct
        head = f'{icon} {self._kind_field(t.kind, t.detail, t.cpu):<24}{t.name:<22} {self._time_field(t, now)}  '
        return self._truncate((head + preview).rstrip(), cols)  # rstrip: no trailing pad when there is no preview yet

    def _summary_line(self, t: Task) -> str:
        icon = self._colored(_ICON[t.state], _ICON_COLOR[t.state])
        _, kind, detail = t.phases[-1] if t.phases else (0, t.kind, t.detail)  # kind = last phase that did work
        line = f'{icon} {self._kind_field(kind, detail):<24}{t.name:<22} {self._time_field(t, t.end)}'
        return f'{line}  {t.note}' if t.note else line

    # -- live CPU sampling -------------------------------------------------

    def attach_pid(self, tid, pid: int):
        """Register a running child pid so the sampler can attribute its process-tree CPU to `tid`.
        The sampler covers only build tasks: a CPU% on a configure/clone/update step is noise."""
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
        # wait longer when a sample is expensive, so sampling never exceeds ~10% of wall-time (cost*9 -> 1-in-10)
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
        cpus = self._cpu_sampler(snapshot)  # ONE process scan for ALL build trees -> {tid: cpu%}, off-lock
        with self._lock:
            for tid, cpu in cpus.items():
                if tid not in self._pids: continue  # detached mid-scan: do not resurrect a dead task's CPU
                t = self._tasks.get(tid)
                if t is not None: t.cpu = cpu

    def _truncate(self, text: str, cols: int) -> str:
        # Cap to cols-1: a wrapped line breaks the cursor math. A fitting line keeps its colors, an over-long
        # one truncates as plain text. Neutralize control characters and non-color escapes first: one stray
        # \n, cursor-move escape or multi-column tab shifts the cursor and strands finished task lines.
        text = _CTRL_RE.sub(' ', text)
        if '\x1b' in text: text = _ESC_RE.sub(lambda m: m[0] if m[0].endswith('m') else '', text)
        limit = max(1, cols - 1)
        plain = _ANSI_RE.sub('', text)
        return text if len(plain) <= limit else plain[:limit]

    def _colored(self, text: str, color) -> str:
        return get_colored_text(text, color) if self._color else text

    def _writeln(self, text: str):
        self._out.write(text + _ERASE_EOL_LF if self._isatty else text + '\n')
        if not self._isatty: self._flush()  # a killed CI process must not lose a block-buffered line

    def _flush(self):
        flush = getattr(self._out, 'flush', None)
        if flush: flush()
