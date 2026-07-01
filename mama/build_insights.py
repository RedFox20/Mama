"""C++ Build Insights (Stage 2 of `buildtimes`): wrap an MSVC build in a vcperf /timetrace session, then
parse the Chrome-trace JSON to show where compile time goes - frontend parse vs backend codegen, slowest
TUs, costliest headers. vcperf is a pure observer of whatever the build command compiled, so `build
buildtimes` profiles the iterative build and `rebuild buildtimes` the full one. Non-MSVC builds skip this."""
import os, json, tempfile
import mama.util as util
from .util import get_time_str, normalized_path
from .utils.system import System, console, warning, Color, get_colored_text
from .utils.sub_process import SubProcess

_SESSION = 'mama_buildtimes'  # one global ETW session name around the whole parallel build
_TIMEOUT = 600  # vcperf /stop relogs the whole trace; give it room on large builds
_VS_VCPERF_SUBPATH = 'Common7/IDE/CommonExtensions/Platform/CppBuildInsights/vcperf.exe'  # bundled standalone
# /noadmin: capture without elevation (one-time `vcperf /grantusercontrol` from an elevated prompt is the
# prerequisite); /nocpusampling: skip the admin-only kernel sampling trace - timetrace needs only MSVC events.
_START = ('/start', '/noadmin', '/nocpusampling')
# Structural activity names (passes/threads/codegen wrappers) that carry no path/symbol of interest.
_STRUCTURAL = ('thread', 'pass', 'frontend', 'backend', 'code generation', 'codegeneration',
               'whole program', 'wholeprogram', 'optref', 'opticf', 'optlbr', 'ltcg', 'invocation')


def find_vcperf(config) -> str:
    """Locate vcperf.exe: VCPERF env, then PATH, then the MSVC toolset bin and the bundled CppBuildInsights
    folder (which ships vcperf's DLLs alongside). Returns '' if not found."""
    env = os.getenv('VCPERF')
    if env and os.path.isfile(env): return env
    found = util.find_executable_from_system('vcperf')
    if found: return found
    candidates = []
    try: candidates.append(f'{config.get_msvc_bin64()}vcperf.exe')
    except Exception: pass
    try: candidates.append(os.path.join(config.get_visualstudio_path(), _VS_VCPERF_SUBPATH))
    except Exception: pass
    return next((c for c in candidates if c and os.path.isfile(c)), '')


def timetrace_path(build_dir: str) -> str:
    """Where vcperf writes the trace: the root project's platform build dir, so other tools can find it at
    a stable per-project location (packages/<project>/<platform>/mama_timetrace.json), not a temp file."""
    return normalized_path(os.path.join(build_dir, 'mama_timetrace.json'))


def _start_reason(output: str) -> str:
    """One concise, actionable line from vcperf's verbose /start failure blurb."""
    low = output.lower()
    if 'grantusercontrol' in low:
        return 'vcperf needs one-time setup - run `vcperf /grantusercontrol` from an elevated prompt'
    if 'preventing vcperf' in low or 'failed to start' in low:
        return 'another ETW trace is already running (try `vcperf /stop mama_buildtimes` or elevated `xperf -stop`)'
    return 'vcperf could not start a trace'


class VcPerfSession:
    """Context manager: `/start /noadmin /nocpusampling` on enter, `/stop /timetrace <json>` on exit (always,
    so an ETW session is never left open even if the build raised). Success is judged by whether the JSON was
    written, not vcperf's exit code (which is unreliable - it errors on an event-less session yet still writes
    a valid empty trace). A failed start degrades to a no-op with a one-line hint."""
    def __init__(self, vcperf: str, json_out: str, level='/level3'):
        self.vcperf = vcperf; self.json_out = json_out; self.level = level; self.ok = False

    def _run(self, args) -> tuple:
        out = []
        try:
            st = SubProcess.run(args, io_func=lambda p, ln: out.append(ln), timeout=_TIMEOUT)
        except Exception as e:
            return 1, str(e)
        return st, '\n'.join(out)

    def _clear_stale(self):
        """Best-effort discard of a session left open by a crashed run, so /start can succeed. /stopnoanalyze
        needs an output path; we write a throwaway ETL and delete it."""
        etl = normalized_path(os.path.join(tempfile.gettempdir(), 'mama_stale.etl'))
        self._run([self.vcperf, '/stopnoanalyze', _SESSION, etl])
        try: os.remove(etl)
        except OSError: pass

    def _start(self) -> tuple:
        return self._run([self.vcperf, *_START, self.level, _SESSION])

    def _grant_user_control(self) -> bool:
        """One-time elevated `vcperf /grantusercontrol` (one UAC prompt) so /noadmin needs no elevation on
        any later run. Windows-only; returns True if the elevated command completed."""
        if not System.windows: return False
        console('buildtimes: granting vcperf user-mode trace control (one-time; accept the elevation prompt)')
        cmd = ['powershell', '-NoProfile', '-Command',
               f"Start-Process -FilePath '{self.vcperf}' -ArgumentList '/grantusercontrol' -Verb RunAs -Wait"]
        try: return SubProcess.run(cmd, io_func=lambda p, ln: None, timeout=120) == 0
        except Exception: return False

    def __enter__(self):
        st, out = self._start()
        if st != 0 and 'grantusercontrol' in out.lower() and self._grant_user_control():
            st, out = self._start()  # /noadmin now permitted - retry
        if st != 0:  # otherwise maybe a stale session from a crashed run - clear it and retry once
            self._clear_stale()
            st, out = self._start()
        self.ok = st == 0
        if not self.ok: warning(f'buildtimes: {_start_reason(out)}; skipping Build Insights')
        return self

    def __exit__(self, *exc):
        if self.ok:
            os.makedirs(os.path.dirname(self.json_out) or '.', exist_ok=True)  # vcperf won't create the dir
            try: os.remove(self.json_out)  # so a leftover trace can't masquerade as this run's
            except OSError: pass
            self._run([self.vcperf, '/stop', _SESSION, '/timetrace', self.json_out])
            self.ok = os.path.exists(self.json_out)  # trust the file, not vcperf's exit code
            if not self.ok: warning('buildtimes: vcperf produced no trace, skipping Build Insights')
        return False  # never suppress a build exception


# --- timetrace JSON parsing ---------------------------------------------------------------------------
# Format (vcperf TimeTraceGenerator): {"traceEvents":[...]} where ts/dur are microseconds. Childless
# entries are complete events (ph "X", carry dur); entries with children are begin/end pairs (ph "B"/"E").
# Names: "CL Invocation N" / "Link Invocation N" (top-level, with File Input/Output args); a FrontEndFile
# carries its path as the name; a Function/template carries its symbol. So a path-separator in the name
# distinguishes a parsed file from a codegen symbol - no dependence on internal pass names.

class _Node:
    __slots__ = ('name', 'args', 'start', 'dur', 'children')
    def __init__(self, name, args, start):
        self.name = name; self.args = args; self.start = start; self.dur = 0.0; self.children = []


def _build_tree(events) -> list:
    roots, stack = [], []
    for ev in events:
        ph = ev.get('ph')
        if ph == 'E':
            if stack: n = stack.pop(); n.dur = ev.get('ts', 0) - n.start
            continue
        n = _Node(ev.get('name', ''), ev.get('args'), ev.get('ts', 0))
        if ph == 'X': n.dur = ev.get('dur', 0)
        (stack[-1].children if stack else roots).append(n)
        if ph == 'B': stack.append(n)
    return roots


def _is_path(name: str) -> bool:
    return '\\' in name or '/' in name  # MSVC symbol names carry no path separators

def _base(name: str) -> str:
    return name.replace('\\', '/').rsplit('/', 1)[-1]

def _structural(name: str) -> bool:
    low = name.lower()
    return any(s in low for s in _STRUCTURAL)

def _arg_values(args, key: str) -> list:
    return [v for k, v in args.items() if k.startswith(key)] if args else []


_FRONTEND_PASS, _BACKEND_PASS = 'FrontEndPass', 'BackEndPass'

def _find_passes(node, fe: list, be: list):
    """Collect the per-TU FrontEndPass/BackEndPass activities under a CL invocation (descending through
    any wrapper nodes). With /MP one invocation runs MANY of each in parallel; summing their durations
    gives aggregate CPU - which is what makes frontend/backend comparable (the invocation's own dur is
    wall time, so aggregate parse could exceed it and clamp backend to zero - the bug this fixes)."""
    for ch in node.children:
        if ch.name == _FRONTEND_PASS: fe.append(ch)
        elif ch.name == _BACKEND_PASS: be.append(ch)
        else: _find_passes(ch, fe, be)


def _collect(node, depth: int, files: dict, symbols: dict, in_backend: bool) -> str:
    """DFS of one compiler pass: aggregate each INCLUDED header (a FrontEndFile at depth>0) into `files`
    and, under a backend pass, each codegen leaf into `symbols`. Returns the outermost source path (the
    TU's own .cpp at depth 0) for labeling - it's not a header, so it never feeds `files`."""
    src = None
    for ch in node.children:
        if _is_path(ch.name):
            if depth > 0:
                agg = files.setdefault(_base(ch.name), [0.0, 0]); agg[0] += ch.dur; agg[1] += 1
            elif src is None:
                src = ch.name
            _collect(ch, depth + 1, files, symbols, in_backend)
        else:
            if in_backend and not ch.children and ch.dur > 0 and not _structural(ch.name):
                symbols[ch.name] = symbols.get(ch.name, 0.0) + ch.dur
            r = _collect(ch, depth, files, symbols, in_backend)
            if src is None: src = r
    return src


def _norm(p: str) -> str:
    return p.replace('\\', '/').lower()


class TraceStats:
    """Aggregated build-insights numbers, all durations in seconds. Lists are sorted slowest-first."""
    def __init__(self, compile_s, link_s, frontend_s, backend_s, wall_s, n_tu, tus, files, symbols):
        self.compile_s = compile_s; self.link_s = link_s
        self.frontend_s = frontend_s; self.backend_s = backend_s
        self.wall_s = wall_s; self.n_tu = n_tu
        self.tus = tus          # [(label, seconds)]
        self.files = files      # [(basename, seconds, include_count)]
        self.symbols = symbols  # [(symbol, seconds)]

    @property
    def empty(self) -> bool:
        return self.n_tu == 0 and self.link_s <= 0


def parse_timetrace(data: dict, scope_paths=None) -> TraceStats:
    """Aggregate a loaded timetrace dict. `scope_paths` (dir prefixes) limits to one package's TUs by
    File Input/Output path; None aggregates the whole build (root stats)."""
    prefixes = [_norm(p) for p in scope_paths] if scope_paths else None
    def in_scope(paths):
        if not prefixes: return True
        return any(any(_norm(p).startswith(pre) for pre in prefixes) for p in paths)

    frontend_us = backend_us = link_us = 0.0
    n_tu = 0
    tus, files, symbols = [], {}, {}
    starts, stops = [], []
    for node in _build_tree(data.get('traceEvents', [])):
        ins, outs = _arg_values(node.args, 'File Input'), _arg_values(node.args, 'File Output')
        if not in_scope(ins + outs): continue
        if node.name.startswith('CL Invocation'):
            starts.append(node.start); stops.append(node.start + node.dur)
            fe_passes, be_passes = [], []
            _find_passes(node, fe_passes, be_passes)
            for fp in fe_passes:  # one per TU (the .cpp's frontend), even under /MP
                frontend_us += fp.dur; n_tu += 1
                src = _collect(fp, 0, files, symbols, in_backend=False)
                tus.append((_base(src) if src else 'TU', fp.dur))
            for bp in be_passes:
                backend_us += bp.dur
                _collect(bp, 0, files, symbols, in_backend=True)
        elif node.name.startswith('Link Invocation'):
            link_us += node.dur
            starts.append(node.start); stops.append(node.start + node.dur)
    wall_us = (max(stops) - min(starts)) if starts else 0.0
    return _build_stats(frontend_us, backend_us, link_us, wall_us, n_tu, tus, files, symbols)


def _build_stats(frontend_us, backend_us, link_us, wall_us, n_tu, tus, files, symbols) -> TraceStats:
    """Convert accumulated microseconds to a sorted TraceStats (compile = frontend + backend, aggregate)."""
    us = 1e-6
    rank = lambda seq: sorted(seq, key=lambda t: t[1], reverse=True)
    return TraceStats((frontend_us + backend_us) * us, link_us * us, frontend_us * us, backend_us * us,
                      wall_us * us, n_tu, rank((l, d * us) for l, d in tus),
                      rank((b, v[0] * us, v[1]) for b, v in files.items()), rank((s, d * us) for s, d in symbols.items()))


# --- Clang -ftime-trace (Linux/Clang deep dive) -------------------------------------------------------
# One flat-event JSON per TU (events ph:"X", each with args.detail). Frontend/Backend are the phase
# totals; Source events carry a parsed file path; Instantiate*/OptFunction carry an ALREADY-demangled
# symbol. Linking isn't traced (link stays 0), and per-TU timelines are independent, so wall comes from
# the build clock, not the JSONs.
_CLANG_CODEGEN = ('InstantiateClass', 'InstantiateFunction', 'OptFunction')
_SRC_EXTS = ('.c', '.cc', '.cpp', '.cxx', '.c++', '.m', '.mm')

def _is_header(path: str) -> bool:
    return not _base(path).lower().endswith(_SRC_EXTS)  # the TU's own .cpp isn't a header; STL headers have no ext


def parse_clang_traces(paths, wall_s=0.0) -> TraceStats:
    """Aggregate Clang -ftime-trace JSONs (one per TU) into the same TraceStats vcperf produces, so the
    Linux deep report renders identically. `wall_s` is the measured build wall time (the JSONs can't give it)."""
    frontend_us = backend_us = 0.0
    n_tu = 0
    tus, files, symbols = [], {}, {}
    for path in paths:
        try:
            with open(path, encoding='utf-8') as f: data = json.load(f)
            events = data.get('traceEvents', []) if isinstance(data, dict) else []  # skip compile_commands.json etc
        except (OSError, ValueError):
            continue
        fe = be = 0.0
        for ev in events:
            if ev.get('ph') != 'X': continue
            name, dur = ev.get('name', ''), ev.get('dur', 0)
            detail = (ev.get('args') or {}).get('detail', '')
            if name == 'Frontend': fe += dur
            elif name == 'Backend': be += dur
            elif name == 'Source' and detail and _is_header(detail):
                agg = files.setdefault(_base(detail), [0.0, 0]); agg[0] += dur; agg[1] += 1
            elif name in _CLANG_CODEGEN and detail:
                symbols[detail] = symbols.get(detail, 0.0) + dur
        if fe or be:
            n_tu += 1; frontend_us += fe; backend_us += be
            tus.append((_base(path)[:-5] if path.endswith('.json') else _base(path), fe + be))
    return _build_stats(frontend_us, backend_us, 0.0, wall_s / 1e-6, n_tu, tus, files, symbols)


def collect_clang_traces(build_dir: str, since: float = 0.0) -> list:
    """The `*.json` time-traces clang wrote into `build_dir`, modified at/after `since` (wall seconds) so
    only THIS build's TUs are analyzed. Content-filtering (skip compile_commands.json etc) is left to the
    parser. Sorted newest-first is irrelevant; the parser aggregates all."""
    import glob
    out = []
    for p in glob.glob(os.path.join(build_dir, '**', '*.json'), recursive=True):
        try:
            if os.path.getmtime(p) >= since: out.append(p)
        except OSError:
            pass
    return out


# --- rendering ----------------------------------------------------------------------------------------
_TOP = 8           # rows per ranked section
_SYM_WIDTH = 100   # truncate a demangled symbol so a codegen row stays one line
_UNDNAME_NAME_ONLY = 0x1000  # dbghelp flag: drop return type / calling convention / params -> scope::name<...>
_undname = None    # memoized (ctypes, UnDecorateSymbolName) or False; resolved once (the API is process-constant)


def _demangle(name: str) -> str:
    """An MSVC-mangled symbol (`?...`) -> readable `scope::name<...>` via dbghelp's UnDecorateSymbolName
    (NAME_ONLY drops the return type/params). No-op for a non-mangled name or off Windows."""
    global _undname
    if not name.startswith('?') or not System.windows:
        return name
    if _undname is None:
        try:
            import ctypes
            fn = ctypes.WinDLL('dbghelp.dll').UnDecorateSymbolName
            fn.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint, ctypes.c_uint]; fn.restype = ctypes.c_uint
            _undname = (ctypes, fn)
        except Exception:
            _undname = False
    if not _undname: return name
    ctypes, fn = _undname
    buf = ctypes.create_string_buffer(8192)
    n = fn(name.encode('utf-8', 'replace'), buf, len(buf), _UNDNAME_NAME_ONLY)
    return buf.value.decode('utf-8', 'replace') if n else name


def _short(s: str, width=_SYM_WIDTH) -> str:
    return s if len(s) <= width else s[:width - 3] + '...'


def _seg(label, color, s, whole=None) -> str:
    pct = f' ({100*s/whole:4.1f}%)' if whole else ''  # share-of-compile, omitted for link
    return f'{get_colored_text(label, color)} {get_time_str(s):>8}{pct}'


def print_buildtimes_deep(stats: TraceStats, scope_label: str):
    """Stage 2 report: frontend/backend/link split + slowest TUs + costliest headers (+ codegen symbols)."""
    console(f'\n  Build Insights ({scope_label})', color=Color.BLUE)
    if stats.empty:
        console('    no compiler activity captured (nothing recompiled - try `rebuild buildtimes`)')
        return

    par = f'{stats.compile_s/stats.wall_s:.1f}x' if stats.wall_s > 0 else '-'
    segs = [_seg('frontend', Color.MAGENTA, stats.frontend_s, stats.compile_s),
            _seg('backend', Color.GREEN, stats.backend_s, stats.compile_s),
            _seg('link', Color.BLUE, stats.link_s)]
    console(f'    {"   ".join(segs)}')
    console(f'    {stats.n_tu} TU(s), {get_time_str(stats.compile_s)} compile over '
            f'{get_time_str(stats.wall_s)} wall ({par} parallel)')

    if stats.tus:
        console('    slowest translation units:', color=Color.MAGENTA)
        for name, s in stats.tus[:_TOP]:
            console(f'      {get_time_str(s):>8}  {name}')
    if stats.files:
        console('    costliest headers (total parse, include count):', color=Color.MAGENTA)
        for name, s, count in stats.files[:_TOP]:
            console(f'      {get_time_str(s):>8}  x{count:<4} {name}')
    if stats.symbols:
        console('    costliest codegen:', color=Color.GREEN)
        for name, s in stats.symbols[:_TOP]:
            console(f'      {get_time_str(s):>8}  {_short(_demangle(name))}')
