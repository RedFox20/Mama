"""Pins Stage 2 build-insights: timetrace tree rebuild, frontend/backend/link aggregation, header
costs, target scoping, and the vcperf session start/stop wiring."""
import json
import pytest
from types import SimpleNamespace
from mama import build_insights as bi
from testutils import strip_ansi

def _b(name, ts, args=None): return {'ph': 'B', 'name': name, 'ts': ts, **({'args': args} if args else {})}
def _e(ts): return {'ph': 'E', 'ts': ts}
def _x(name, ts, dur): return {'ph': 'X', 'name': name, 'ts': ts, 'dur': dur}

# Real vcperf shape: CL Invocation -> FrontEndPass -> C1DLL -> <source> (+ nested includes); BackEndPass ->
# C2DLL -> codegen leaves. CL0 (a.cpp -> C:\proj): frontend 100us (incl big.h 60us), backend 60us (someFunc).
# CL1 (b.cpp -> C:\other): frontend 60us, re-includes big.h 20us, no backend. Link 0 -> C:\proj, 50us.
def _fe(src, ts, dur, inc=None):  # one FrontEndPass: C1DLL -> source, optional nested include (name, ts, dur)
    body = [_b('C1DLL', ts), _b(src, ts)] + ([_x(inc[0], inc[1], inc[2])] if inc else []) + [_e(ts + dur), _e(ts + dur)]
    return [_b('FrontEndPass', ts), *body, _e(ts + dur)]
def _be(sym, ts, dur, sym_dur):  # one BackEndPass: C2DLL -> one codegen leaf
    return [_b('BackEndPass', ts), _b('C2DLL', ts), _x(sym, ts, sym_dur), _e(ts + dur), _e(ts + dur)]

_TRACE = {'traceEvents': [
    _b('CL Invocation 0', 0, {'File Input': r'C:\proj\src\a.cpp', 'File Output': r'C:\proj\build\a.obj'}),
      *_fe(r'C:\proj\src\a.cpp', 0, 100, inc=(r'C:\proj\inc\big.h', 10, 60)),
      *_be('someFunc', 100, 60, 40),
    _e(160),
    _b('CL Invocation 1', 200, {'File Input': r'C:\other\src\b.cpp', 'File Output': r'C:\other\build\b.obj'}),
      *_fe(r'C:\other\src\b.cpp', 200, 60, inc=(r'C:\proj\inc\big.h', 210, 20)),
    _e(260),
    _b('Link Invocation 0', 260, {'File Output': r'C:\proj\build\app.exe', 'File Input': r'C:\proj\build\a.obj'}),
    _e(310),
]}

US = 1e-6


def test_build_tree_reconstructs_passes_and_durations():
    roots = bi._build_tree(_TRACE['traceEvents'])
    assert [r.name for r in roots] == ['CL Invocation 0', 'CL Invocation 1', 'Link Invocation 0']
    assert roots[0].dur == 160
    assert [c.name for c in roots[0].children] == ['FrontEndPass', 'BackEndPass']
    assert roots[0].children[0].dur == 100   # FrontEndPass B/E span


def test_root_totals_sum_passes_not_invocation_wall():
    s = bi.parse_timetrace(_TRACE)
    assert s.n_tu == 2                                              # one per FrontEndPass, not per cl.exe
    assert s.frontend_s == (100 + 60) * US and s.backend_s == 60 * US
    assert s.compile_s == s.frontend_s + s.backend_s and s.link_s == 50 * US  # compile is aggregate, never clamped


def test_header_costs_count_includes_not_tu_sources():
    files = dict((b, (sec, n)) for b, sec, n in bi.parse_timetrace(_TRACE).files)
    assert files['big.h'] == (80 * US, 2)   # included by both TUs -> summed, counted twice
    assert 'a.cpp' not in files and 'b.cpp' not in files   # a TU's own source is not a header


def test_codegen_symbols_exclude_structural_and_paths():
    syms = [name for name, _ in bi.parse_timetrace(_TRACE).symbols]
    assert syms == ['someFunc']   # the passes (FrontEndPass/C1DLL/...) and file paths are not codegen symbols


def test_scope_filters_to_one_packages_tus():
    s = bi.parse_timetrace(_TRACE, scope_paths=[r'C:\proj'])
    assert s.n_tu == 1 and s.link_s == 50 * US        # a.cpp + its link, not b.cpp under C:\other
    assert [t[0] for t in s.tus] == ['a.cpp']
    assert dict((b, n) for b, _, n in s.files)['big.h'] == 1   # only a.cpp's parse of it


def test_is_path_separates_files_from_symbols():
    assert bi._is_path(r'C:\x\y.h') and bi._is_path('/usr/include/vector')
    assert not bi._is_path('std::vector<int>::push_back') and not bi._is_path('someFunc')


def test_empty_trace_reports_no_activity(capsys):
    s = bi.parse_timetrace({'traceEvents': []})
    assert s.empty
    bi.print_buildtimes_deep(s, 'root')
    assert 'no compiler activity captured' in strip_ansi(capsys.readouterr().out)


def test_deep_report_renders_all_sections(capsys):
    bi.print_buildtimes_deep(bi.parse_timetrace(_TRACE), 'root')
    out = strip_ansi(capsys.readouterr().out)
    for token in ['Build Insights (root)', 'frontend', 'backend', 'link', 'translation units', 'a.cpp',
                  'costliest headers', 'big.h']:
        assert token in out


def test_demangle_decodes_msvc_and_passes_through_plain():
    assert bi._demangle('main') == 'main'                              # not mangled -> unchanged
    assert bi._demangle('mocs_compilation.cpp') == 'mocs_compilation.cpp'
    if bi.System.windows:
        assert bi._demangle('?_buildMap@QGCPalette@@CAXXZ') == 'QGCPalette::_buildMap'  # dbghelp NAME_ONLY


def test_short_truncates_long_symbols_to_one_line():
    assert bi._short('x' * 200).endswith('...') and len(bi._short('x' * 200)) == bi._SYM_WIDTH
    assert bi._short('rfl::parsing::to_single_error_message') == 'rfl::parsing::to_single_error_message'


def test_timetrace_path_is_in_the_build_dir(tmp_path):
    assert bi.timetrace_path(str(tmp_path / 'pkg' / 'windows')).endswith('/pkg/windows/mama_timetrace.json')


def test_deep_report_demangles_codegen_symbols(capsys):
    if not bi.System.windows: return  # demangling is a no-op off Windows (dbghelp is Windows-only)
    tr = {'traceEvents': [
        _b('CL Invocation 0', 0, {'File Input': r'C:\p\a.cpp', 'File Output': r'C:\p\b\a.obj'}),
          *_fe(r'C:\p\a.cpp', 0, 1_000_000),
          *_be('?_buildMap@QGCPalette@@CAXXZ', 1_000_000, 5_000_000, 5_000_000),
        _e(6_000_000)]}
    bi.print_buildtimes_deep(bi.parse_timetrace(tr), 'root')
    out = strip_ansi(capsys.readouterr().out)
    assert 'QGCPalette::_buildMap' in out and '?_buildMap' not in out   # raw mangled name never shown


def test_is_header_excludes_source_files():
    assert bi._is_header('/usr/include/c++/vector') and bi._is_header('foo.h')   # STL (no ext) + .h
    assert not bi._is_header('/proj/a.cpp') and not bi._is_header('b.cc')        # the TU's own source


def test_parse_clang_traces_aggregates_across_tus(tmp_path):
    ev = lambda n, d, detail=None: {'ph': 'X', 'name': n, 'dur': d, **({'args': {'detail': detail}} if detail else {})}
    def w(name, evs): (tmp_path / name).write_text(json.dumps({'traceEvents': evs}))
    w('a.cpp.json', [ev('Frontend', 800000), ev('Backend', 200000), ev('Source', 500000, '/u/vector'),
                     ev('Source', 300000, '/p/a.cpp'), ev('InstantiateClass', 150000, 'std::vector<Foo>')])
    w('b.cpp.json', [ev('Frontend', 600000), ev('Backend', 100000), ev('Source', 400000, '/u/vector'),
                     ev('OptFunction', 90000, 'rpp::recv()')])
    st = bi.parse_clang_traces([str(tmp_path / 'a.cpp.json'), str(tmp_path / 'b.cpp.json')], wall_s=1.0)
    assert st.n_tu == 2 and st.link_s == 0.0 and st.wall_s == 1.0
    assert st.frontend_s == pytest.approx(1.4) and st.backend_s == pytest.approx(0.3)
    assert [t[0] for t in st.tus] == ['a.cpp', 'b.cpp']            # slowest first, .json + source basename stripped
    files = dict((b, (s, n)) for b, s, n in st.files)
    assert files['vector'] == (pytest.approx(0.9), 2) and 'a.cpp' not in files  # header summed; the .cpp isn't a header
    assert dict(st.symbols)['std::vector<Foo>'] == pytest.approx(0.15)          # clang details already readable


def test_parse_clang_traces_skips_unreadable_and_non_trace_json(tmp_path):
    (tmp_path / 'bad.json').write_text('{ not json')
    (tmp_path / 'compile_commands.json').write_text('[{"file": "a.cpp"}]')   # a list, not a trace dict
    files = [str(tmp_path / n) for n in ('bad.json', 'compile_commands.json', 'missing.json')]
    assert bi.parse_clang_traces(files).empty


def test_collect_clang_traces_filters_by_mtime(tmp_path):
    import os
    old = tmp_path / 'old.json'; old.write_text('{}'); os.utime(old, (1, 1))   # ancient
    new = tmp_path / 'sub' / 'new.json'; new.parent.mkdir(); new.write_text('{}')  # recursive + fresh
    got = bi.collect_clang_traces(str(tmp_path), since=1000)
    assert str(new) in got and str(old) not in got


def test_find_vcperf_prefers_env_override(monkeypatch, tmp_path):
    exe = tmp_path / 'vcperf.exe'; exe.write_text('')
    monkeypatch.setenv('VCPERF', str(exe))
    assert bi.find_vcperf(SimpleNamespace()) == str(exe)


def test_find_vcperf_returns_empty_when_absent(monkeypatch):
    monkeypatch.delenv('VCPERF', raising=False)
    monkeypatch.setattr(bi.util, 'find_executable_from_system', lambda name: '')
    def _raise(): raise EnvironmentError('no VS')
    cfg = SimpleNamespace(get_msvc_bin64=_raise, get_visualstudio_path=_raise)
    assert bi.find_vcperf(cfg) == ''


def test_session_issues_start_noadmin_nocpusampling_and_stop_timetrace(monkeypatch, tmp_path):
    cmds, out = [], tmp_path / 'tt.json'
    def _run(args, **k): cmds.append(args); out.write_text('{}'); return 0  # /stop writes the trace
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(_run))
    with bi.VcPerfSession('vcperf.exe', str(out)) as s:
        assert s.ok
    assert s.ok   # judged by the written file, not the exit code
    flat = [' '.join(c) for c in cmds]
    assert any('/start /noadmin /nocpusampling /level3 mama_buildtimes' in c for c in flat)
    assert any(f'/stop mama_buildtimes /timetrace {out}' in c for c in flat)


def test_session_missing_trace_file_degrades_to_noop(monkeypatch, tmp_path):
    # /start succeeds, /stop exits nonzero and writes nothing -> we trust the absent file, not the code.
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(lambda args, **k: 0 if '/start' in args else 5))
    with bi.VcPerfSession('vcperf.exe', str(tmp_path / 'nope.json')) as s:
        assert s.ok   # /start reported success
    assert not s.ok   # /stop produced no file -> no-op


def test_start_auto_runs_grantusercontrol_then_retries(monkeypatch, tmp_path):
    calls, out = [], tmp_path / 'tt.json'
    def _run(args, io_func=None, **k):
        calls.append(args); joined = ' '.join(args)
        if 'grantusercontrol' in joined: return 0   # elevated grant accepted
        if '/start' in args:
            if sum('/start' in c for c in calls) == 1:   # first start: not yet granted
                if io_func: io_func(None, 'requires /grantusercontrol with admin')
                return 1
            return 0                                     # retry after grant succeeds
        out.write_text('{}'); return 0                   # /stop writes the trace
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(_run))
    monkeypatch.setattr(bi.System, 'windows', True)
    with bi.VcPerfSession('vcperf.exe', str(out)) as s:
        assert s.ok
    joined = [' '.join(c) for c in calls]
    assert any('grantusercontrol' in c for c in joined) and sum('/start' in c for c in joined) == 2


def test_grantusercontrol_hint_is_concise_when_grant_declined(monkeypatch, capsys):
    def _run(args, io_func=None, **k):
        if io_func: io_func(None, 'requires to run vcperf with the `/grantusercontrol` flag with admin')
        return 1   # every command (incl. the declined elevated grant) fails
    monkeypatch.setattr(bi.SubProcess, 'run', staticmethod(_run))
    monkeypatch.setattr(bi.System, 'windows', True)
    with bi.VcPerfSession('vcperf.exe', 'out.json') as s:
        pass
    out = strip_ansi(capsys.readouterr().out)
    assert not s.ok and '/grantusercontrol' in out and 'Failed to start trace' not in out  # the blurb is collapsed
