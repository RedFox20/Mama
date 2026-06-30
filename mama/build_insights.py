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


def timetrace_path() -> str:
    return normalized_path(os.path.join(tempfile.gettempdir(), 'mama_timetrace.json'))


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


def _walk(node, depth_in_file: int, files: dict, symbols: dict) -> float:
    """Returns the node's frontend (parse) contribution while filling `files` (each INCLUDED header by
    basename -> [inclusive_us, count]) and `symbols` (codegen leaves -> us). Frontend credits only the
    OUTERMOST file span (depth 0, its dur already covers the includes); that source file is the TU itself,
    not a header, so only nested files (depth > 0) feed `files` - that's the cross-TU PCH signal."""
    fe = 0.0
    for ch in node.children:
        if _is_path(ch.name):
            if depth_in_file == 0:
                fe += ch.dur
            else:
                agg = files.setdefault(_base(ch.name), [0.0, 0]); agg[0] += ch.dur; agg[1] += 1
            _walk(ch, depth_in_file + 1, files, symbols)
        else:
            if not ch.children and ch.dur > 0 and not _structural(ch.name):
                symbols[ch.name] = symbols.get(ch.name, 0.0) + ch.dur
            fe += _walk(ch, depth_in_file, files, symbols)
    return fe


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

    compile_us = link_us = frontend_us = 0.0
    n_tu = 0
    tus, files, symbols = [], {}, {}
    starts, stops = [], []
    for node in _build_tree(data.get('traceEvents', [])):
        ins, outs = _arg_values(node.args, 'File Input'), _arg_values(node.args, 'File Output')
        if not in_scope(ins + outs): continue
        if node.name.startswith('CL Invocation'):
            n_tu += 1; compile_us += node.dur
            starts.append(node.start); stops.append(node.start + node.dur)
            tus.append((_base(ins[0]) if ins else node.name, node.dur))
            frontend_us += _walk(node, 0, files, symbols)
        elif node.name.startswith('Link Invocation'):
            link_us += node.dur
            starts.append(node.start); stops.append(node.start + node.dur)
    backend_us = max(0.0, compile_us - frontend_us)
    wall_us = (max(stops) - min(starts)) if starts else 0.0
    us = 1e-6
    rank = lambda seq: sorted(seq, key=lambda t: t[1], reverse=True)
    tus_s = rank((l, d*us) for l, d in tus)
    files_s = rank((b, v[0]*us, v[1]) for b, v in files.items())
    syms_s = rank((s, d*us) for s, d in symbols.items())
    return TraceStats(compile_us*us, link_us*us, frontend_us*us, backend_us*us, wall_us*us, n_tu,
                      tus_s, files_s, syms_s)


# --- rendering ----------------------------------------------------------------------------------------
_TOP = 8  # rows per ranked section

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
            console(f'      {get_time_str(s):>8}  {name}')
